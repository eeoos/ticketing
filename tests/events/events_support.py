"""tests/events 공용 도구. 기준: docs/events.md (outbox → Kafka → notifier).

- 분리 DB(EVENTS_DATABASE_URL, 기본 ticketing_events)만 쓴다. 개발 DB 이름(ticketing)이면 거절.
- Kafka 는 호스트 localhost:9094(EVENTS_KAFKA_BOOTSTRAP). 토픽·그룹 이름은 실행(테스트)마다 새로 만든다(개발 booking-events·notifier 와 섞이지 않음).
- 릴레이·notifier 는 호스트 하위 프로세스(`uv run python -m backend.relay` / `backend.notifier`)로 테스트 DB·토픽·그룹을 주고 띄운다.
- API 는 tests/api 와 같은 방식으로 프로세스 안에서 TestClient 로 부른다(get_now 오버라이드). 앱은 Kafka 에 연결하지 않는다.
- Kafka 를 멈추는 테스트는 끝나면(실패해도) `docker compose start kafka` 하고 준비를 확인한다(복구 실패는 teardown error).

기다리는 시간 기준(상한, 근거):
- EVENTAL(알림이 '결국' 도착): 30초. 릴레이 폴링 0.5초 + 프로듀서·소비자 그룹 가입(해 둔 initial rebalance delay 0)에 파이썬 프로세스 시작 2~4초가 들고
  최악 10초 안쪽이다. 3배 여유.
- RECOVER(Kafka 를 다시 켠 뒤 밀린 전달): 90초. 브로커 재기동 10~20초 + 클라이언트 재연결 백오프(librdkafka 최대 10초 안팎) + 소비자 재가입.
- 'N 초 안에 안 일어난다'는 시계로 단정하지 않는다: 같은 공연(같은 키=같은 파티션) 뒤쪽 표지(sentinel) 알림이 도착한 뒤에 앞쪽의 부재를 확인한다.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parents[2]
EVENTS_DSN = os.environ.get("EVENTS_DATABASE_URL", "postgresql://ticketing:ticketing@localhost:5432/ticketing_events")
BOOTSTRAP = os.environ.get("EVENTS_KAFKA_BOOTSTRAP", "localhost:9094")
DEV_DB_NAME = "ticketing"
DEV_TOPIC, DEV_GROUP = "booking-events", "notifier"
EVENTS_REDIS_DB = 4  # tests/api 와 같은 DB 번호를 쓰되 접두사로 분리한다(ticket_support 의 DB 번호 배정 참고)
T0 = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
MOCK_PG_SECRET = os.environ.get("MOCK_PG_SECRET", "mock-pg-dev-secret")
EVENTAL, RECOVER = 30, 90


def fail(msg, e):
    return AssertionError(f"{msg}: {type(e).__name__}: {str(e)[-1500:]}")


def compose(*args, timeout=180, check=True):
    p = subprocess.run(["docker", "compose", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)} 실패(code {p.returncode}):\n{(p.stdout + p.stderr)[-1500:]}")
    return p


def wait_until(cond, timeout, what, interval=0.3):
    """cond() 가 참(또는 비어 있지 않은 값)이 될 때까지 기다린다. timeout 은 '결국 일어나는가'의 상한."""
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        last = cond()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"{timeout}초 안에 {what} 이(가) 일어나지 않았다(마지막 값: {last!r})")


def kafka_ready(timeout=5):
    from confluent_kafka.admin import AdminClient  # noqa: PLC0415
    try:
        AdminClient({"bootstrap.servers": BOOTSTRAP, "socket.timeout.ms": 3000}).list_topics(timeout=timeout)
        return True
    except Exception:  # noqa: BLE001
        return False


def ensure_kafka_up(timeout=120):
    if kafka_ready():
        return
    compose("start", "kafka", check=False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if kafka_ready():
            return
        time.sleep(1)
    raise RuntimeError(f"Kafka({BOOTSTRAP})를 {timeout}초 안에 되살리지 못했다 — 'docker compose up -d --wait kafka' 가 필요하다")


def _ensure_database():
    name = conninfo_to_dict(EVENTS_DSN)["dbname"]
    if name == DEV_DB_NAME:
        raise RuntimeError(f"EVENTS_DATABASE_URL 이 개발 DB '{DEV_DB_NAME}' 를 가리킨다. 분리 DB(기본 ticketing_events)를 써야 한다.")
    with psycopg.connect(make_conninfo(EVENTS_DSN, dbname="postgres"), autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


class Proc:
    """호스트 하위 프로세스(릴레이·notifier). 출력은 파일에 모아 실패 때 보여 준다."""

    def __init__(self, module, env, name):
        self.module, self.name = module, name
        self.log = Path(env["_LOG_DIR"]) / f"{name}.log"
        e = {k: v for k, v in env.items() if k != "_LOG_DIR"}
        self._env = e
        self.p = None

    def start(self):
        self._fh = open(self.log, "ab")
        self.p = subprocess.Popen(["uv", "run", "python", "-m", self.module], cwd=ROOT, env=self._env,
                                  stdout=self._fh, stderr=subprocess.STDOUT, start_new_session=True)
        return self

    def alive(self):
        return self.p is not None and self.p.poll() is None

    def stop(self):
        if self.p is not None and self.p.poll() is None:
            try:
                os.killpg(self.p.pid, signal.SIGTERM)
                self.p.wait(timeout=15)
            except Exception:  # noqa: BLE001
                try:
                    os.killpg(self.p.pid, signal.SIGKILL)
                    self.p.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    pass
        self._fh.close()

    def kill(self):
        """SIGKILL(커밋 사이에 죽은 것처럼)."""
        if self.alive():
            os.killpg(self.p.pid, signal.SIGKILL)
            self.p.wait(timeout=5)

    def output(self):
        try:
            return self.log.read_text(errors="replace")[-3000:]
        except OSError:
            return ""


class Events:
    """한 테스트의 API 클라이언트 + 시계 + 분리 DB + 이 실행 전용 토픽·그룹 + 하위 프로세스."""

    def __init__(self, tmp_path):
        self.now = T0
        self.topic = f"evt-{uuid.uuid4().hex[:10]}"
        self.group = f"grp-{uuid.uuid4().hex[:10]}"
        self.prefix = f"evtest-{uuid.uuid4().hex[:8]}:"
        self.log_dir = tmp_path
        self.procs: list[Proc] = []
        self._client = None
        self._kafka_stopped = False
        self._users = 0

    # ---- 준비 ----
    def _ensure(self):
        if self._client is not None:
            return
        try:
            from ticket_support import REDIS_HOST_URL, redis_for  # noqa: PLC0415
            os.environ["REDIS_URL"] = f"{REDIS_HOST_URL}/{EVENTS_REDIS_DB}"
            os.environ["REDIS_KEY_PREFIX"] = self.prefix
            os.environ["DATABASE_URL"] = EVENTS_DSN
            self._redis = redis_for(EVENTS_REDIS_DB)
            _ensure_database()
            from backend import main, migrate, seed  # noqa: PLC0415
            from fastapi.testclient import TestClient  # noqa: PLC0415
            migrate.run(EVENTS_DSN)
            seed.run(EVENTS_DSN)
            self.reset_event_tables()
            self._app, self._get_now = main.app, main.get_now
            self._app.dependency_overrides[self._get_now] = lambda: self.now
            self._make_client = lambda: TestClient(self._app, raise_server_exceptions=False)
            self._client = self._make_client()
            self._show_id = None
        except Exception as e:  # noqa: BLE001
            raise fail("events 테스트 환경을 준비하지 못했다", e) from e

    def reset_event_tables(self):
        with psycopg.connect(EVENTS_DSN, autocommit=True) as conn:
            for t in ("outbox", "notifications"):
                if conn.execute("SELECT to_regclass(%s)", (f"public.{t}",)).fetchone()[0]:
                    conn.execute(sql.SQL("TRUNCATE {} CASCADE").format(sql.Identifier(t)))

    def close(self):
        for p in self.procs:
            p.stop()
        if self._client is not None:
            self._app.dependency_overrides.pop(self._get_now, None)
            try:
                from ticket_support import delete_prefix  # noqa: PLC0415
                delete_prefix(self._redis, self.prefix)
            except Exception:  # noqa: BLE001
                pass
        errors = []
        if self._kafka_stopped:
            try:
                ensure_kafka_up()
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        try:  # 이 실행의 토픽·그룹 청소(실패해도 무시: 이름이 실행마다 달라 다른 테스트와 섞이지 않는다)
            if kafka_ready(timeout=3):
                from confluent_kafka.admin import AdminClient  # noqa: PLC0415
                a = AdminClient({"bootstrap.servers": BOOTSTRAP})
                a.delete_topics([self.topic], operation_timeout=5)
                a.delete_consumer_groups([self.group])
        except Exception:  # noqa: BLE001
            pass
        if errors:
            raise errors[0]

    # ---- 시간·사용자 ----
    def advance(self, **delta):
        self.now = self.now + timedelta(**delta)

    def user(self, label="u"):
        self._users += 1
        return f"{label}-{uuid.uuid4().hex[:8]}-{self._users}"

    # ---- HTTP ----
    def request(self, method, path, user=None, **kw):
        self._ensure()
        headers = dict(kw.pop("headers", {}))
        if user is not None:
            headers["X-User-Id"] = user
        return self._client.request(method, path, headers=headers, **kw)

    def new_client(self):
        self._ensure()
        return self._make_client()

    def show_id(self):
        self._ensure()
        if self._show_id is None:
            r = self.request("GET", "/shows")
            assert r.status_code == 200, r.text
            self._show_id = r.json()[0]["id"]
        return self._show_id

    def seat_ids(self, n=None):
        self._ensure()
        with psycopg.connect(EVENTS_DSN) as conn:
            rows = [r[0] for r in conn.execute("SELECT id FROM seats WHERE show_id=%s ORDER BY id", (self.show_id(),))]
        return rows if n is None else rows[:n]

    def seat_id(self, i=0):
        return self.seat_ids()[i]

    def _ticket(self, user, ttl=timedelta(days=1)):
        from ticket_support import grant_ticket  # noqa: PLC0415
        return grant_ticket(self._redis, self.prefix, user, self.show_id(), (self.now + ttl).timestamp() * 1000)

    def hold(self, seat_id, user):
        self._ensure()
        return self.request("POST", f"/seats/{seat_id}/hold", user=user, headers={"X-Queue-Ticket": self._ticket(user)})

    def pay(self, seat_id, user, key=None):
        return self.request("POST", "/bookings", user=user, json={"seatId": seat_id},
                            headers={"Idempotency-Key": key or f"key-{uuid.uuid4()}"})

    @staticmethod
    def sign(raw: bytes) -> str:
        import hashlib  # noqa: PLC0415
        import hmac  # noqa: PLC0415
        return hmac.new(MOCK_PG_SECRET.encode(), raw, hashlib.sha256).hexdigest()

    def callback(self, payment_id, result, client=None):
        raw = json.dumps({"paymentId": payment_id, "result": result}).encode()
        headers = {"Content-Type": "application/json", "X-Mock-Pg-Signature": self.sign(raw)}
        return (client or self._client or self.new_client()).request(
            "POST", "/payments/callback", content=raw, headers=headers)

    def hold_and_pay(self, seat_id, user):
        h = self.hold(seat_id, user)
        assert h.status_code == 201, h.text
        p = self.pay(seat_id, user)
        assert p.status_code == 202, p.text
        return p.json()

    def confirm(self, seat_id=None, user=None):
        """홀드 → 결제 요청 → SUCCEEDED 콜백(처음 CONFIRMED). (예매 본문, 사용자)를 돌려준다."""
        user = user or self.user()
        seat_id = seat_id or self.seat_id()
        b = self.hold_and_pay(seat_id, user)
        r = self.callback(b["paymentId"], "SUCCEEDED")
        assert r.status_code == 200, r.text
        assert r.json()["applied"] is True and r.json()["booking"]["status"] == "CONFIRMED", r.text
        return r.json()["booking"], user

    # ---- DB ----
    def q(self, query, params=()):
        self._ensure()
        with psycopg.connect(EVENTS_DSN) as conn:
            return conn.execute(query, params).fetchall()

    def outbox_rows(self):
        return self.q("SELECT event_id, type, booking_id, show_id, user_id, payload, created_at, sent_at, attempts, last_error "
                      "FROM outbox ORDER BY created_at, event_id")

    def outbox_count(self):
        return self.q("SELECT count(*) FROM outbox")[0][0]

    def notifications(self):
        return self.q("SELECT event_id, user_id, booking_id, type, message, created_at, read_at FROM notifications "
                      "ORDER BY created_at, id")

    def notification_count(self):
        return self.q("SELECT count(*) FROM notifications")[0][0]

    def notifications_for(self, booking_id):
        return self.q("SELECT event_id, user_id, booking_id FROM notifications WHERE booking_id=%s", (str(booking_id),))

    # ---- 하위 프로세스 ----
    def _env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in ("REDIS_URL", "REDIS_KEY_PREFIX")}
        env.update({"DATABASE_URL": EVENTS_DSN, "KAFKA_BOOTSTRAP": BOOTSTRAP, "EVENTS_TOPIC": self.topic,
                    "NOTIFIER_GROUP": self.group, "RELAY_POLL_MS": "200", "_LOG_DIR": str(self.log_dir)})
        env.update(extra)
        return env

    def _guard(self):
        assert self.topic != DEV_TOPIC and self.group != DEV_GROUP

    def start_relay(self, **extra):
        self._ensure()
        self._guard()
        p = Proc("backend.relay", self._env(**extra), f"relay{len(self.procs)}").start()
        self.procs.append(p)
        return p

    def start_notifier(self, module=os.environ.get("TEST_NOTIFIER_MODULE", "backend.notifier"), **extra):
        self._ensure()
        self._guard()
        p = Proc(module, self._env(**extra), f"notifier{len(self.procs)}").start()
        self.procs.append(p)
        return p

    def start_pipeline(self):
        return self.start_relay(), self.start_notifier()

    def wait(self, cond, timeout, what):
        """wait_until + 실패하면 하위 프로세스 출력을 함께 보여 준다."""
        try:
            return wait_until(cond, timeout, what)
        except AssertionError as e:
            raise AssertionError(f"{e}\n{self.diag()}") from None

    def diag(self):
        return "\n".join(f"--- {p.name} (alive={p.alive()}) ---\n{p.output()}" for p in self.procs)

    # ---- Kafka ----
    def stop_kafka(self):
        self._kafka_stopped = True
        compose("stop", "kafka")

    def start_kafka(self):
        compose("start", "kafka")
        ensure_kafka_up()
        self._kafka_stopped = False

    def producer(self):
        from confluent_kafka import Producer  # noqa: PLC0415
        return Producer({"bootstrap.servers": BOOTSTRAP, "enable.idempotence": True})

    def produce_raw(self, key, value, headers=None):
        """토픽에 직접 메시지를 넣고 전송 확인까지 기다린다."""
        from confluent_kafka import Producer  # noqa: PLC0415
        done = threading.Event()
        err = []

        def cb(e, _m):
            if e is not None:
                err.append(e)
            done.set()

        p = Producer({"bootstrap.servers": BOOTSTRAP, "acks": "all"})
        p.produce(self.topic, value=value, key=key, headers=headers or [], on_delivery=cb)
        p.flush(30)
        assert done.is_set() and not err, f"직접 produce 실패: {err}"

    def read_topic(self, timeout=10):
        """토픽의 모든 메시지를 (별도 그룹으로, 오프셋 커밋 없이) 처음부터 읽는다: [(key, headers dict, value bytes, partition)]."""
        from confluent_kafka import Consumer, TopicPartition  # noqa: PLC0415
        c = Consumer({"bootstrap.servers": BOOTSTRAP, "group.id": f"reader-{uuid.uuid4().hex[:8]}",
                      "enable.auto.commit": False, "auto.offset.reset": "earliest"})
        try:
            md = c.list_topics(self.topic, timeout=10)
            if self.topic not in md.topics or md.topics[self.topic].error is not None:
                return []
            parts = list(md.topics[self.topic].partitions)
            ends = {}
            for p in parts:
                lo, hi = c.get_watermark_offsets(TopicPartition(self.topic, p), timeout=10)
                ends[p] = (lo, hi)
            c.assign([TopicPartition(self.topic, p, ends[p][0]) for p in parts])
            out, deadline = [], time.monotonic() + timeout
            want = sum(hi - lo for lo, hi in ends.values())
            while len(out) < want and time.monotonic() < deadline:
                m = c.poll(0.5)
                if m is None or m.error():
                    continue
                out.append((m.key(), {k: v for k, v in (m.headers() or [])}, m.value(), m.partition()))
            return out
        finally:
            c.close()

    def confirmed_event(self):
        """실제로 확정 1건을 만들고 그 outbox 이벤트(dict: payload, 키 showId)를 돌려준다. 릴레이는 안 띄우므로 토픽에는 아직 없다."""
        booking, user = self.confirm(self.seat_id(self._users))
        row = [r for r in self.outbox_rows() if str(r[2]) == booking["id"]][0]
        payload = row[5] if not isinstance(row[5], str) else json.loads(row[5])
        return dict(payload)

    def send_event(self, payload, key=None, raw=None):
        """payload(dict) 또는 raw 바이트를 토픽에 직접 넣는다. key 기본 = payload 의 showId."""
        value = raw if raw is not None else json.dumps(payload).encode()
        k = key if key is not None else str(payload.get("showId", "")).encode()
        self.produce_raw(k, value, [("eventId", str(payload.get("eventId", "")).encode())] if payload else [])

    def committed_offsets(self):
        """notifier 그룹이 커밋한 오프셋 합계(토픽의 모든 파티션)."""
        from confluent_kafka import Consumer, TopicPartition  # noqa: PLC0415
        c = Consumer({"bootstrap.servers": BOOTSTRAP, "group.id": self.group, "enable.auto.commit": False})
        try:
            md = c.list_topics(self.topic, timeout=10)
            if self.topic not in md.topics:
                return 0
            tps = [TopicPartition(self.topic, p) for p in md.topics[self.topic].partitions]
            return sum(max(tp.offset, 0) for tp in c.committed(tps, timeout=10))
        finally:
            c.close()

    def topic_end_total(self):
        from confluent_kafka import Consumer, TopicPartition  # noqa: PLC0415
        c = Consumer({"bootstrap.servers": BOOTSTRAP, "group.id": f"reader-{uuid.uuid4().hex[:8]}", "enable.auto.commit": False})
        try:
            md = c.list_topics(self.topic, timeout=10)
            if self.topic not in md.topics:
                return 0
            return sum(c.get_watermark_offsets(TopicPartition(self.topic, p), timeout=10)[1]
                       for p in md.topics[self.topic].partitions)
        finally:
            c.close()
