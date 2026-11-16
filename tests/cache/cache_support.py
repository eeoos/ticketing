"""tests/cache 공용 도구. 기준: docs/cache.md

- DB: 개발 DB 와 분리(CACHE_DATABASE_URL, 기본 ticketing_cache; 개발 DB 이름이면 거절).
- Redis: 호스트 localhost:6379, DB 1 이상 + 실행별 접두사. 개발 키(DB 0, 'tkt:')는 건드리지 않고 끝나면 자기 접두사 키만 지운다.
- ClockServer: get_now 를 파일로 조종하는 실제 HTTP 서버(하위 프로세스). Server: 실제 시계·uvicorn 워커 여러 개.
- Stack: docs/infra.md 절차로 앱 3대+Nginx 를 테스트용 DB·Redis DB·접두사로 다시 띄우고, 끝나면 기본값으로 되돌린다(실패하면 예외).
"""
import asyncio
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import psycopg
import redis
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parents[2]
CACHE_DSN = os.environ.get("CACHE_DATABASE_URL", "postgresql://ticketing:ticketing@localhost:5432/ticketing_cache")
REDIS_HOST_URL = "redis://localhost:6379"
TEST_REDIS_DB = 1  # 개발 스택은 DB 0
STACK_REDIS_DB = 2  # 3대 스택 테스트용
DEV_DB_NAME = "ticketing"
MOCK_PG_SECRET = os.environ.get("MOCK_PG_SECRET", "mock-pg-dev-secret")
T0 = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
APPS = ("app1", "app2", "app3")


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def new_prefix() -> str:
    return f"cachetest-{uuid.uuid4().hex[:8]}:"


def uid(prefix="u") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def db_name(dsn=CACHE_DSN) -> str:
    return conninfo_to_dict(dsn)["dbname"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------- DB ----------------
def reset_database(dsn=CACHE_DSN):
    if db_name(dsn) == DEV_DB_NAME:
        raise RuntimeError(f"CACHE_DATABASE_URL 이 개발 DB '{DEV_DB_NAME}' 를 가리킨다. 분리 DB(기본 ticketing_cache)를 써야 한다.")
    admin = make_conninfo(dsn, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (db_name(dsn),)).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name(dsn))))
    os.environ["DATABASE_URL"] = dsn
    from backend import migrate, seed  # noqa: PLC0415
    migrate.run(dsn)
    seed.run(dsn)


def db_seat(seat_id, dsn=CACHE_DSN):
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT status, hold_user_id, hold_expires_at FROM seats WHERE id=%s", (seat_id,)).fetchone()


def xact_total(dsn=CACHE_DSN):
    """테스트 DB 의 pg_stat_database xact_commit + xact_rollback (통계 반영이 늦을 수 있어 호출 전에 잠깐 기다린다)."""
    with psycopg.connect(make_conninfo(dsn, dbname="postgres"), autocommit=True) as conn:
        row = conn.execute("SELECT xact_commit + xact_rollback FROM pg_stat_database WHERE datname = %s",
                           (db_name(dsn),)).fetchone()
    return int(row[0])


# ---------------- Redis ----------------
def redis_client(db=TEST_REDIS_DB):
    return redis.Redis.from_url(f"{REDIS_HOST_URL}/{db}", decode_responses=True, socket_timeout=3)


def delete_prefix(prefix, db=TEST_REDIS_DB):
    """자기 접두사 키만 지운다."""
    try:
        r = redis_client(db)
        keys = list(r.scan_iter(match=f"{prefix}*", count=500))
        if keys:
            r.delete(*keys)
    except redis.RedisError:
        pass


def read_value(r, key):
    """해시(HSET)로 저장했든 JSON 문자열로 저장했든 같은 dict 로 읽는다. 없으면 None."""
    kind = r.type(key)
    if kind == "hash":
        return r.hgetall(key)
    if kind == "string":
        return json.loads(r.get(key))
    return None


# ---------------- HTTP 서버들 ----------------
class _Proc:
    def __init__(self):
        self.log = tempfile.NamedTemporaryFile("w+", prefix="cache-srv-", suffix=".log", delete=False)
        self.proc = None
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.client = None

    def _wait_ready(self, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"서버가 종료됐다(code {self.proc.returncode}):\n{self.log_tail()}")
            try:
                if httpx.get(f"{self.url}/shows", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.2)
        raise RuntimeError(f"서버가 {timeout}s 안에 준비되지 않았다:\n{self.log_tail()}")

    def log_tail(self, n=2000):
        self.log.flush()
        return Path(self.log.name).read_text(errors="replace")[-n:]

    def stop(self):
        if self.client:
            self.client.close()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.log.close()
        Path(self.log.name).unlink(missing_ok=True)


def _server_env(dsn, redis_url, prefix, gate, timeout_ms):
    env = dict(os.environ, DATABASE_URL=dsn, MOCK_PG_SECRET=MOCK_PG_SECRET, PYTHONPATH=str(ROOT))
    for k in ("REDIS_URL", "REDIS_KEY_PREFIX", "HOLD_GATE", "REDIS_TIMEOUT_MS"):
        env.pop(k, None)
    if redis_url:
        env["REDIS_URL"] = redis_url
        env["REDIS_KEY_PREFIX"] = prefix
    if gate:
        env["HOLD_GATE"] = gate
    if timeout_ms:
        env["REDIS_TIMEOUT_MS"] = str(timeout_ms)
    return env


class ClockServer(_Proc):
    """get_now 를 파일로 조종하는 단일 프로세스 서버. 같은 clock_file 을 여러 서버가 공유하면 '여러 앱'을 흉내 낸다."""

    def __init__(self, prefix, redis_url=f"{REDIS_HOST_URL}/{TEST_REDIS_DB}", gate=None, timeout_ms=None,
                 clock_file=None, dsn=CACHE_DSN):
        super().__init__()
        self.prefix, self.dsn = prefix, dsn
        self.clock_file = Path(clock_file) if clock_file else Path(tempfile.mkstemp(prefix="clock-", suffix=".txt")[1])
        self.owns_clock = clock_file is None
        self.redis_url, self.gate, self.timeout_ms = redis_url, gate, timeout_ms
        self.set_time(T0)

    @property
    def now(self):
        return datetime.fromisoformat(self.clock_file.read_text().strip())

    def set_time(self, dt):
        tmp = self.clock_file.with_suffix(".tmp")
        tmp.write_text(dt.isoformat())
        os.replace(tmp, self.clock_file)

    def advance(self, **delta):
        self.set_time(self.now + timedelta(**delta))

    def start(self):
        env = _server_env(self.dsn, self.redis_url, self.prefix, self.gate, self.timeout_ms)
        env.update(CACHE_TEST_CLOCK_FILE=str(self.clock_file), CACHE_TEST_PORT=str(self.port))
        self.proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name("_clock_server.py"))],
                                     cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        self._wait_ready()
        self.client = httpx.Client(base_url=self.url, timeout=30)
        return self

    def stop(self):
        super().stop()
        if self.owns_clock:
            self.clock_file.unlink(missing_ok=True)

    # ---- API 도우미 ----
    def show_id(self):
        return self.client.get("/shows").json()[0]["id"]

    def seat_ids(self, n=None):
        """좌석 id 를 DB 에서 직접 읽는다(API 와 같은 정렬). 좌석맵 엔드포인트를 부르면 캐시가 미리 채워져
        'MISS' 단언이 깨지므로 캐시를 건드리지 않는 경로를 쓴다."""
        with psycopg.connect(self.dsn) as conn:
            rows = conn.execute("SELECT id FROM seats WHERE show_id = %s ORDER BY section, row_label, number",
                                (self.show_id(),)).fetchall()
        ids = [r[0] for r in rows]
        return ids if n is None else ids[:n]

    def seatmap(self, show_id=None):
        return self.client.get(f"/shows/{show_id or self.show_id()}/seats")

    @staticmethod
    def statuses(resp):
        return {s["id"]: s["status"] for s in resp.json()}

    def hold(self, seat, user):
        return self.client.post(f"/seats/{seat}/hold", headers={"X-User-Id": user})

    def pay(self, seat, user, key=None):
        return self.client.post("/bookings", json={"seatId": seat},
                                headers={"X-User-Id": user, "Idempotency-Key": key or uid("key")})

    def callback(self, payment_id, result):
        raw = json.dumps({"paymentId": payment_id, "result": result}).encode()
        sig = hmac.new(MOCK_PG_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        return self.client.post("/payments/callback", content=raw,
                                headers={"Content-Type": "application/json", "X-Mock-Pg-Signature": sig})

    def sell(self, seat, user):
        assert self.hold(seat, user).status_code == 201
        b = self.pay(seat, user)
        assert b.status_code == 202, b.text
        c = self.callback(b.json()["paymentId"], "SUCCEEDED")
        assert c.status_code == 200 and c.json()["booking"]["status"] == "CONFIRMED", c.text
        return c.json()["booking"]


class Server(_Proc):
    """실제 시계·uvicorn 워커 여러 개(tests/concurrency 와 같은 방식). HOLD_GATE on/off 비교용."""

    def __init__(self, prefix, gate, workers=4, dsn=CACHE_DSN, redis_url=f"{REDIS_HOST_URL}/{TEST_REDIS_DB}"):
        super().__init__()
        self.prefix, self.gate, self.workers, self.dsn, self.redis_url = prefix, gate, workers, dsn, redis_url

    def start(self):
        env = _server_env(self.dsn, self.redis_url, self.prefix, self.gate, None)
        self.proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.main:app", "--workers", str(self.workers),
                                      "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "warning"],
                                     cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        self._wait_ready()
        self.client = httpx.Client(base_url=self.url, timeout=30)
        return self


async def _fire(url, requests):
    n = len(requests)
    gate = asyncio.Event()
    limits = httpx.Limits(max_connections=n + 20, max_keepalive_connections=n + 20)
    async with httpx.AsyncClient(base_url=url, limits=limits, timeout=httpx.Timeout(120)) as c:
        async def one(spec):
            await gate.wait()
            try:
                return await c.request(**spec)
            except httpx.HTTPError as e:
                return e
        tasks = [asyncio.create_task(one(s)) for s in requests]
        await asyncio.sleep(0.5)
        gate.set()
        return await asyncio.gather(*tasks)


def fire(server, requests):
    """배리어 뒤 한꺼번에 전송. 응답 없는 요청은 예외 객체로 돌려준다."""
    return asyncio.run(_fire(server.url, requests))


# ---------------- docker ----------------
def compose(*args, env_extra=None, drop_env=(), timeout=600, check=True):
    env = {k: v for k, v in os.environ.items() if k not in drop_env}
    env.update(env_extra or {})
    p = subprocess.run(["docker", "compose", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)} 실패(code {p.returncode}):\n{(p.stdout + p.stderr)[-1500:]}")
    return p


def wait_redis_healthy(timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if redis.Redis.from_url(f"{REDIS_HOST_URL}/0", socket_timeout=1).ping():
                return
        except redis.RedisError:
            time.sleep(0.3)
    raise RuntimeError(f"{timeout}s 안에 Redis 가 응답하지 않았다")


class RedisStopped:
    """docker compose stop redis → (끝나면 실패해도) start. 복구 실패는 예외(teardown error)."""

    def __init__(self):
        self.stopped = False

    def stop(self):
        compose("stop", "redis")
        self.stopped = True
        try:
            redis.Redis.from_url(f"{REDIS_HOST_URL}/0", socket_connect_timeout=1, socket_timeout=1).ping()
        except redis.RedisError:
            return
        raise RuntimeError("redis 를 멈췄는데 아직 응답한다")

    def restore(self):
        if not self.stopped:
            return
        p = compose("start", "redis", check=False)
        if p.returncode:
            raise RuntimeError(f"redis 복구 실패: docker compose start redis ({(p.stderr or p.stdout).strip()[-300:]})")
        wait_redis_healthy()
        self.stopped = False
