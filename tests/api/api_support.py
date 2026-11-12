"""tests/api 공용 도구. backend 는 테스트 본문에서 처음 쓸 때 불러온다.

backend 가 없으면 테스트마다 AssertionError 하나로 개별 실패한다(수집 오류로 뭉치지 않음).
약속: docs/backend-interface.md
"""
import os
import threading
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

TEST_DSN = os.environ.get("TEST_DATABASE_URL", "postgresql://ticketing:ticketing@localhost:5432/ticketing_test")
T0 = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
HOLD = timedelta(minutes=5)


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

    def shows(self, user=None):
        return self.request("GET", "/shows", user=user)

    def show_id(self):
        r = self.shows()
        assert r.status_code == 200, r.text
        return r.json()[0]["id"]

    def seat_map(self, show_id=None):
        r = self.request("GET", f"/shows/{show_id or self.show_id()}/seats")
        assert r.status_code == 200, r.text
        return r.json()

    def seat_ids(self):
        return [s["id"] for s in self.seat_map()]

    def status_of(self, seat_id):
        return next(s["status"] for s in self.seat_map() if s["id"] == seat_id)

    def hold(self, seat_id, user=None, client=None):
        return self.request("POST", f"/seats/{seat_id}/hold", user=user, client=client)

    def book(self, seat_id, user=None, json=None):
        body = {"seatId": seat_id} if json is None else json
        return self.request("POST", "/bookings", user=user, json=body)

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
