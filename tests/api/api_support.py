"""tests/api 공용 도구. backend 는 테스트 본문에서 처음 쓸 때 불러온다.

backend 가 없으면 테스트마다 AssertionError 하나로 개별 실패한다(수집 오류로 뭉치지 않음).
약속: docs/backend-interface.md
"""
import hashlib
import hmac
import json as jsonlib
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

TEST_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://ticketing:ticketing@localhost:5432/ticketing_test")
T0 = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
HOLD = timedelta(minutes=5)
MOCK_PG_SECRET = os.environ.get("MOCK_PG_SECRET", "mock-pg-dev-secret")
API_REDIS_PREFIX = f"apitest-{uuid.uuid4().hex[:8]}:"  # 이 pytest 프로세스 전용 접두사(끝나면 conftest 가 지운다)
VIEWER = "viewer"  # 좌석맵을 보기만 하는 시험용 사용자
NO_KEY = object()  # Idempotency-Key 헤더를 보내지 않는다


def parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    assert dt.tzinfo is not None, f"시각에 타임존이 없다: {value}"
    return dt


DEV_DB_NAME = "ticketing"


def _ensure_database():
    name = conninfo_to_dict(TEST_DSN)["dbname"]
    if name == DEV_DB_NAME:  # migrate/seed/TRUNCATE 가 개발 DB 를 지우지 않게 막는다
        raise RuntimeError(
            f"TEST_DATABASE_URL 이 개발 DB '{DEV_DB_NAME}' 를 가리킨다. 테스트는 시드가 데이터를 지우므로 "
            "별도 DB(기본 ticketing_test)를 써야 한다.")
    admin = make_conninfo(TEST_DSN, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


class Api:
    """한 테스트의 HTTP 클라이언트 + 시계 + 시드 상태. 첫 사용 시 DB 를 시드한다."""

    def __init__(self):
        self.now = T0
        self._client = None

    # ---- setup ----
    def _ensure(self):
        if self._client is not None:
            return
        try:
            from ticket_support import DB_API, REDIS_HOST_URL, redis_for  # noqa: PLC0415
            os.environ["REDIS_URL"] = f"{REDIS_HOST_URL}/{DB_API}"  # 9장부터 좌석맵·홀드는 입장권(=Redis)이 필요하다
            os.environ["REDIS_KEY_PREFIX"] = API_REDIS_PREFIX
            self._redis = redis_for(DB_API)
            os.environ["DATABASE_URL"] = TEST_DSN
            _ensure_database()
            from backend import main, migrate, seed  # noqa: PLC0415
            from fastapi.testclient import TestClient  # noqa: PLC0415
            migrate.run(TEST_DSN)
            seed.run(TEST_DSN)
            self._app, self._get_now = main.app, main.get_now
            self._app.dependency_overrides[self._get_now] = lambda: self.now
            self._make_client = lambda: TestClient(self._app, raise_server_exceptions=False)
            self._client = self._make_client()
        except Exception as e:  # backend 없음/DB 없음 등
            raise AssertionError(f"테스트 환경을 준비하지 못했다: {type(e).__name__}: {e}") from e

    def close(self):
        if self._client is not None:
            self._app.dependency_overrides.pop(self._get_now, None)

    # ---- time ----
    def advance(self, **delta):
        self.now = self.now + timedelta(**delta)

    # ---- http ----
    def request(self, method, path, user=None, client=None, **kw):
        self._ensure()
        headers = dict(kw.pop("headers", {}))
        if user is not None:
            headers["X-User-Id"] = user
        return (client or self._client).request(method, path, headers=headers, **kw)

    def new_client(self):
        self._ensure()
        return self._make_client()

    # ---- 입장권 (Redis 에 문서화된 형식으로 직접 발급; tests/queue 는 실제 대기열로 얻는다) ----
    def ticket(self, user, show_id=None, ttl=timedelta(days=1)):
        """user 의 입장권 token. 만료 = (get_now 기준 지금) + ttl."""
        self._ensure()
        from ticket_support import grant_ticket  # noqa: PLC0415
        sid = show_id or self.show_id()
        return grant_ticket(self._redis, API_REDIS_PREFIX, user, sid, (self.now + ttl).timestamp() * 1000)

    def shows(self, user=None):
        return self.request("GET", "/shows", user=user)

    def show_id(self):
        r = self.shows()
        assert r.status_code == 200, r.text
        return r.json()[0]["id"]

    def seats_request(self, show_id=None, user=VIEWER, ticket="auto"):
        """GET /shows/{id}/seats. ticket: 'auto'(그 사용자·공연의 유효한 입장권) | None(헤더 없음) | token 문자열."""
        sid = show_id or self.show_id()
        headers = {}
        if ticket == "auto" and user:
            headers["X-Queue-Ticket"] = self.ticket(user, sid)
        elif ticket not in (None, "auto"):
            headers["X-Queue-Ticket"] = ticket
        return self.request("GET", f"/shows/{sid}/seats", user=user, headers=headers)

    def seat_map(self, show_id=None):
        r = self.seats_request(show_id)
        assert r.status_code == 200, r.text
        return r.json()

    def seat_ids(self):
        return [s["id"] for s in self.seat_map()]

    def status_of(self, seat_id):
        return next(s["status"] for s in self.seat_map() if s["id"] == seat_id)

    def hold(self, seat_id, user=None, client=None, ticket="auto"):
        """POST /seats/{id}/hold. ticket: 'auto'(이 사용자의 유효한 입장권) | None(헤더 없음) | token 문자열."""
        headers = {}
        if ticket == "auto" and user:
            headers["X-Queue-Ticket"] = self.ticket(user)
        elif ticket not in (None, "auto"):
            headers["X-Queue-Ticket"] = ticket
        return self.request("POST", f"/seats/{seat_id}/hold", user=user, client=client, headers=headers)

    def pay(self, seat_id, user=None, key=None, json=None, headers=None, client=None):
        """POST /bookings. key=None 이면 새 키를 만들고, key=NO_KEY 면 헤더를 보내지 않는다."""
        h = dict(headers or {})
        if key is None:
            key = f"key-{uuid.uuid4()}"
        if key is not NO_KEY:
            h["Idempotency-Key"] = key
        body = {"seatId": seat_id} if json is None else json
        return self.request("POST", "/bookings", user=user, client=client, json=body, headers=h)

    def get_booking(self, booking_id, user=None):
        return self.request("GET", f"/bookings/{booking_id}", user=user)

    @staticmethod
    def sign(raw: bytes, secret: str = MOCK_PG_SECRET) -> str:
        return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()

    def callback(self, payment_id, result, body=None, signature="valid", client=None):
        """POST /payments/callback. 본문 원문 바이트에 서명한다. signature: 'valid' | None(헤더 없음) | 문자열."""
        payload = {"paymentId": payment_id, "result": result} if body is None else body
        raw = payload if isinstance(payload, bytes) else jsonlib.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if signature == "valid":
            headers["X-Mock-Pg-Signature"] = self.sign(raw)
        elif signature is not None:
            headers["X-Mock-Pg-Signature"] = signature
        return self.request("POST", "/payments/callback", client=client, content=raw, headers=headers)

    def hold_and_pay(self, seat_id, user, key=None):
        """홀드 → 결제 요청(202). (hold 본문, booking 본문)을 돌려준다."""
        h = self.hold(seat_id, user=user)
        assert h.status_code == 201, h.text
        p = self.pay(seat_id, user=user, key=key)
        assert p.status_code == 202, p.text
        return h.json(), p.json()

    def sell(self, seat_id, user):
        """홀드 → 결제 요청 → SUCCEEDED 콜백으로 좌석을 SOLD 로 만든다. 확정된 예매 본문을 돌려준다."""
        _, b = self.hold_and_pay(seat_id, user)
        r = self.callback(b["paymentId"], "SUCCEEDED")
        assert r.status_code == 200 and r.json()["applied"] is True, r.text
        assert r.json()["booking"]["status"] == "CONFIRMED", r.text
        return r.json()["booking"]

    def first_seat(self):
        return self.seat_ids()[0]

    # ---- DB (스키마를 모르므로 이름으로 찾는다) ----
    def empty_database(self):
        self._ensure()
        with psycopg.connect(TEST_DSN, autocommit=True) as conn:
            rows = conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                "AND tablename !~* '(migrat|schema_)'").fetchall()
            if rows:
                names = sql.SQL(", ").join(sql.Identifier(r[0]) for r in rows)
                conn.execute(sql.SQL("TRUNCATE {} CASCADE").format(names))

    def add_shows(self, n):
        """테스트 DB 에 공연 n 개를 직접 추가한다(시드 공연 외). 추가한 행을 돌려준다."""
        self._ensure()
        rows = []
        with psycopg.connect(TEST_DSN, autocommit=True) as conn:
            for i in range(n):
                row = conn.execute(
                    "INSERT INTO shows (title, venue, starts_at) VALUES (%s, %s, %s) "
                    "RETURNING id, title, venue, starts_at",
                    (f"추가 공연 {i + 1}", f"추가 장소 {i + 1}", T0 + timedelta(days=i + 1))).fetchone()
                rows.append(row)
        return rows

    def check_invariants(self):
        """DB 불변식 5: 좌석 SOLD ⇔ 그 좌석의 CONFIRMED 예매가 정확히 하나. (불변식 1: CONFIRMED 는 좌석당 최대 하나)"""
        self._ensure()
        with psycopg.connect(TEST_DSN) as conn:
            has_status = conn.execute(
                "SELECT 1 FROM information_schema.columns WHERE table_name = 'bookings' AND column_name = 'status'"
            ).fetchone()
            if not has_status:  # 새 스키마(마이그레이션 002)가 아직 없다 — 불변식을 물어볼 수 없다(구현 전 RED 상태)
                return
            multi = conn.execute("SELECT seat_id, count(*) FROM bookings WHERE status = 'CONFIRMED' "
                                 "GROUP BY seat_id HAVING count(*) > 1").fetchall()
            assert not multi, f"좌석당 CONFIRMED 예매가 둘 이상: {multi}"
            bad = conn.execute(
                "SELECT s.id, s.status, (SELECT count(*) FROM bookings b WHERE b.seat_id = s.id AND b.status = 'CONFIRMED') "
                "FROM seats s WHERE (s.status = 'SOLD') <> EXISTS "
                "(SELECT 1 FROM bookings b WHERE b.seat_id = s.id AND b.status = 'CONFIRMED')").fetchall()
            assert not bad, f"SOLD ⇔ CONFIRMED 1건 불변식 위반(좌석, 상태, CONFIRMED 수): {bad}"

    def bookings_in_db(self, seat_id=None):
        self._ensure()
        with psycopg.connect(TEST_DSN) as conn:
            q = "SELECT id, seat_id, user_id, status FROM bookings"
            if seat_id:
                return conn.execute(q + " WHERE seat_id = %s ORDER BY created_at, id", (seat_id,)).fetchall()
            return conn.execute(q + " ORDER BY created_at, id").fetchall()

    def booking_count(self):
        self._ensure()
        with psycopg.connect(TEST_DSN) as conn:
            tables = [r[0] for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename ILIKE '%booking%'")]
            assert tables, "booking 이름을 가진 테이블이 없다"
            return sum(conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(t))).fetchone()[0]
                       for t in tables)


def assert_error(resp, status, code):
    assert resp.status_code == status, f"기대 {status}, 실제 {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("code") == code, f"기대 code={code}, 실제 {body}"
    assert body.get("message"), "오류 message 가 비어 있다"


def run_concurrently(n, fn):
    """n 개 스레드가 동시에 fn(i) 을 시작하도록 맞춰 결과 리스트를 돌려준다."""
    barrier, results, errors = threading.Barrier(n), [None] * n, []

    def worker(i):
        try:
            barrier.wait(timeout=10)
            results[i] = fn(i)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    assert not errors, f"스레드 오류: {errors}"
    return results
