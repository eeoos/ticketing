"""tests/concurrency 공용 도구. 기준: docs/concurrency.md

실제 uvicorn(워커 4) 하위 프로세스 + PostgreSQL. TestClient/시각 오버라이드 없음.
겹침 증거(HTTP 최대 동시 진행 수, pg active 최댓값, 서로 다른 application_name 수)를 모은다.
"""
import asyncio
import uuid
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DSN = "postgresql://ticketing:ticketing@localhost:5432/ticketing_concurrency"
DEV_DB_NAME = "ticketing"
WORKERS = 4
REDIS_DB = 5  # ticket_support.DB_CONCURRENCY
MOCK_PG_SECRET = os.environ.get("MOCK_PG_SECRET", "mock-pg-dev-secret")  # 테스트와 하위 uvicorn 이 같은 값을 쓴다
APP_NAME_PREFIX = "ticketing-api-"


def concurrency_dsn() -> str:
    return os.environ.get("CONCURRENCY_DATABASE_URL", DEFAULT_DSN)


def db_name(dsn: str) -> str:
    return conninfo_to_dict(dsn)["dbname"]


def guard_not_dev_db(dsn: str):
    if db_name(dsn) == DEV_DB_NAME:
        raise RuntimeError(
            f"CONCURRENCY_DATABASE_URL 이 개발 DB '{DEV_DB_NAME}' 를 가리킨다. 시나리오마다 데이터를 지우므로 "
            "별도 DB(기본 ticketing_concurrency)를 써야 한다.")


def ensure_database(dsn: str):
    guard_not_dev_db(dsn)
    admin = make_conninfo(dsn, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (db_name(dsn),)).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name(dsn))))


def reset_database(dsn: str):
    """DB 준비 + migrate + seed(초기화). backend 는 여기서 처음 import 한다."""
    ensure_database(dsn)
    os.environ["DATABASE_URL"] = dsn
    from backend import migrate, seed  # noqa: PLC0415
    migrate.run(dsn)
    seed.run(dsn)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """python -m uvicorn backend.main:app --workers 4 --port <빈 포트> 하위 프로세스."""

    def __init__(self, dsn: str):
        from ticket_support import DB_CONCURRENCY, REDIS_HOST_URL, redis_for  # noqa: PLC0415
        self.prefix = f"conctest-{uuid.uuid4().hex[:8]}:"
        self.redis_url = f"{REDIS_HOST_URL}/{DB_CONCURRENCY}"
        self.redis = redis_for(DB_CONCURRENCY)
        self.dsn, self.port = dsn, free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.log = tempfile.NamedTemporaryFile("w+", prefix="uvicorn-", suffix=".log", delete=False)
        self.proc = None

    def start(self, timeout=60):
        # 9장: 좌석맵·홀드는 입장권(=Redis)이 필요하다. 이 시나리오들의 목적은 DB 의 원자성(동시 홀드 하나만 성공 등)이므로
        # Redis 홀드 관문은 끈다(HOLD_GATE=off) — 관문이 켜져 있으면 거절이 DB 앞에서 끝나 DB 경쟁을 검증하지 못한다(관문은 tests/cache).
        env = dict(os.environ, DATABASE_URL=self.dsn, MOCK_PG_SECRET=MOCK_PG_SECRET, REDIS_URL=self.redis_url,
                   REDIS_KEY_PREFIX=self.prefix, HOLD_GATE="off")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "backend.main:app", "--workers", str(WORKERS),
             "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "warning"],
            cwd=ROOT, env=env, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"uvicorn 이 종료됐다(code {self.proc.returncode}):\n{self.log_tail()}")
            try:
                if httpx.get(f"{self.url}/shows", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.2)
        raise RuntimeError(f"uvicorn 이 {timeout}s 안에 준비되지 않았다:\n{self.log_tail()}")

    def log_tail(self, n=2000):
        self.log.flush()
        return Path(self.log.name).read_text(errors="replace")[-n:]

    def grant_tickets(self, users, show_id):
        """실제 시계 서버: 사용자마다 입장권을 Redis 에 직접 발급(실제 시각 +2시간). {user: token}.
        (실제로 얻으려면 구간당 200명 정책이라 500명이면 3초 이상 걸려 시나리오가 느려지고 대기열 동작에 의존한다 — tests/queue 가 따로 검증)"""
        from ticket_support import grant_ticket  # noqa: PLC0415
        expires = time.time() * 1000 + 2 * 3_600_000
        return {u: grant_ticket(self.redis, self.prefix, u, show_id, expires) for u in users}

    def stop(self):
        try:
            from ticket_support import delete_prefix  # noqa: PLC0415
            delete_prefix(self.redis, self.prefix)
        except Exception:  # noqa: BLE001
            pass
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.log.close()
        Path(self.log.name).unlink(missing_ok=True)


class PgSampler(threading.Thread):
    """실행 중 pg_stat_activity 를 짧은 간격으로 샘플링한다(대상 DB 한정)."""

    def __init__(self, dsn: str, interval=0.005):
        super().__init__(daemon=True)
        self.dsn, self.interval = dsn, interval
        self.run_start = None  # DB 시계(clock_timestamp) 기준 실행 시작. 이후 쿼리를 시작한 세션만 '처리 증거'로 센다.
        self.worked_names: set[str] = set()
        self.stop_event = threading.Event()
        self.max_active = 0
        self.max_sessions = 0
        self.app_names: set[str] = set()
        self.samples = 0
        self.error = None
        self.started = threading.Event()

    def run(self):
        try:
            with psycopg.connect(self.dsn, autocommit=True, application_name="concurrency-test-sampler") as conn:
                self.run_start = conn.execute("SELECT clock_timestamp()").fetchone()[0]
                self.started.set()
                while not self.stop_event.is_set():
                    rows = conn.execute(
                        "SELECT state, application_name FROM pg_stat_activity "
                        "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                        "AND backend_type = 'client backend'").fetchall()
                    self.samples += 1
                    self.max_active = max(self.max_active, sum(1 for s, _ in rows if s == "active"))
                    self.max_sessions = max(self.max_sessions, len(rows))
                    self.app_names.update(a for _, a in rows if a.startswith(APP_NAME_PREFIX))
                    self.worked_names |= self.worked_application_names(conn)
                    time.sleep(self.interval)
        except Exception as e:  # noqa: BLE001
            self.error = repr(e)
        finally:
            self.started.set()

    def worked_application_names(self, conn) -> set[str]:
        """실행 시작 이후 쿼리를 시작한 앱 세션의 application_name. 풀 연결은 끝난 뒤에도 남아 query_start 가
        마지막 쿼리 시각이므로, 5ms 샘플링과 무관하게 '이 실행 중 DB 작업을 한 워커'를 결정적으로 알 수 있다."""
        rows = conn.execute(
            "SELECT DISTINCT application_name FROM pg_stat_activity "
            "WHERE datname = current_database() AND backend_type = 'client backend' "
            "AND application_name LIKE %s AND query_start >= %s", (APP_NAME_PREFIX + "%", self.run_start)).fetchall()
        return {r[0] for r in rows}

    def finish(self):
        self.stop_event.set()
        self.join(timeout=10)
        if self.run_start is not None:  # 종료 직후 스냅샷(풀 연결이 남아 있는 동안)
            try:
                with psycopg.connect(self.dsn, autocommit=True, application_name="concurrency-test-snapshot") as c:
                    self.worked_names |= self.worked_application_names(c)
            except Exception as e:  # noqa: BLE001
                self.error = self.error or f"snapshot: {e!r}"


@dataclass
class Result:
    status: int | None
    body: object
    start: float
    end: float
    error: str | None = None
    headers: object = field(default_factory=dict)

    @property
    def code(self):
        return self.body.get("code") if isinstance(self.body, dict) else None


@dataclass
class Run:
    results: list[Result]
    max_inflight: int
    pg_max_active: int
    pg_max_sessions: int
    app_name_count: int
    app_names: list[str]
    pg_samples: int
    sampler_error: str | None = None
    worked_names: list[str] = field(default_factory=list)  # 실행 중 쿼리를 시작한 워커(application_name)
    notes: list[str] = field(default_factory=list)

    def status_counts(self):
        """실제 HTTP 상태 코드별 개수. 응답이 없으면(연결 오류 등) 'error'. 5xx 도 실제 코드 그대로 센다."""
        return dict(Counter("error" if r.status is None else r.status for r in self.results))

    def count_5xx_or_error(self):
        return sum(1 for r in self.results if r.status is None or r.status >= 500)

    def code_409_counts(self):
        return dict(Counter(r.code for r in self.results if r.status == 409))

    def evidence(self) -> str:
        first5xx = next((r for r in self.results if r.status is None or r.status >= 500), None)
        txt = (f"응답 코드 분포={self.status_counts()} 409 code 분포={self.code_409_counts()} | "
               f"겹침 증거: HTTP 최대 동시 진행={self.max_inflight}, pg active 최댓값={self.pg_max_active}"
               f"(최대 세션 {self.pg_max_sessions}, 샘플 {self.pg_samples}), "
               f"application_name 수={self.app_name_count} {self.app_names}, "
               f"실행 중 DB 작업한 워커 수={len(self.worked_names)} {self.worked_names}")
        if self.sampler_error:
            txt += f" | 샘플러 오류: {self.sampler_error}"
        if first5xx:
            txt += f" | 첫 5xx/오류 응답: status={first5xx.status} body={str(first5xx.body or first5xx.error)[:300]}"
        return txt


def _max_inflight(results: list[Result]) -> int:
    events = sorted([(r.start, 1) for r in results] + [(r.end, -1) for r in results], key=lambda e: (e[0], e[1]))
    cur = best = 0
    for _, d in events:
        cur += d
        best = max(best, cur)
    return best


async def _fire(server_url: str, requests: list) -> list[Result]:
    """requests 원소: 요청 dict 한 건, 또는 async 함수 fn(call) — 한 클라이언트가 여러 요청을 순서대로 보내는 흐름.
    모든 태스크는 같은 시작 신호(배리어) 뒤에 출발한다. 반환은 모든 요청의 Result 를 편 목록."""
    n = len(requests)
    limits = httpx.Limits(max_connections=n + 50, max_keepalive_connections=n + 50)
    gate = asyncio.Event()
    async with httpx.AsyncClient(base_url=server_url, limits=limits, timeout=httpx.Timeout(120)) as client:
        async def call(spec, out) -> Result:
            start = time.perf_counter()
            try:
                resp = await client.request(**spec)
                try:
                    body = resp.json()
                except ValueError:
                    body = resp.text
                res = Result(resp.status_code, body, start, time.perf_counter())
                res.headers = resp.headers
            except httpx.HTTPError as e:
                res = Result(None, None, start, time.perf_counter(), error=f"{type(e).__name__}: {e}")
                res.headers = {}
            out.append(res)
            return res

        async def one(spec) -> list[Result]:
            await gate.wait()
            out: list[Result] = []
            if callable(spec):
                await spec(lambda **kw: call(kw, out))
            else:
                await call(spec, out)
            return out

        tasks = [asyncio.create_task(one(s)) for s in requests]
        await asyncio.sleep(0.5)  # 모든 태스크가 배리어에서 대기하도록
        gate.set()  # 시작 신호
        return [r for group in await asyncio.gather(*tasks) for r in group]


def run_concurrently(server: Server, dsn: str, requests: list[dict]) -> Run:
    sampler = PgSampler(dsn)
    sampler.start()
    sampler.started.wait(timeout=10)  # 실행 시작 시각(DB 시계)을 잡은 뒤에 요청을 보낸다
    try:
        results = asyncio.run(_fire(server.url, requests))
    finally:
        sampler.finish()
    return Run(results, _max_inflight(results), sampler.max_active, sampler.max_sessions,
               len(sampler.app_names), sorted(sampler.app_names), sampler.samples, sampler.error,
               sorted(sampler.worked_names))


def sign(raw: bytes) -> str:
    return hmac.new(MOCK_PG_SECRET.encode(), raw, hashlib.sha256).hexdigest()


def callback_request(payment_id: str, result: str) -> dict:
    raw = json.dumps({"paymentId": payment_id, "result": result}).encode()
    return dict(method="POST", url="/payments/callback", content=raw,
                headers={"Content-Type": "application/json", "X-Mock-Pg-Signature": sign(raw)})


def sync_post(server: Server, path: str, user: str | None = None, **kw):
    headers = dict(kw.pop("headers", {}))
    if user:
        headers["X-User-Id"] = user
    return httpx.post(server.url + path, headers=headers, timeout=30, **kw)


def show_of(server: Server) -> str:
    return httpx.get(server.url + "/shows", timeout=30).json()[0]["id"]


def hold_req(server: Server, seat_id: str, user: str, token: str | None = None) -> dict:
    """POST /seats/{id}/hold 요청 dict(입장권 포함). token 이 없으면 그 사용자의 입장권을 새로 발급한다."""
    token = token or server.grant_tickets([user], show_of(server))[user]
    return dict(method="POST", url=f"/seats/{seat_id}/hold", headers={"X-User-Id": user, "X-Queue-Ticket": token})


def sync_hold(server: Server, seat_id: str, user: str):
    r = hold_req(server, seat_id, user)
    return httpx.post(server.url + r["url"], headers=r["headers"], timeout=30)


def hold_and_pay(server: Server, seat_id: str, user: str, key: str = "setup-key"):
    """(순차) 홀드 → 결제 요청. 202 를 확인하고 (hold, booking) 본문을 돌려준다."""
    h = sync_hold(server, seat_id, user)
    assert h.status_code == 201, h.text
    b = sync_post(server, "/bookings", user, json={"seatId": seat_id}, headers={"Idempotency-Key": key})
    assert b.status_code == 202, b.text
    return h.json(), b.json()


def bookings_of(dsn: str, seat_id: str):
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT id, user_id, status FROM bookings WHERE seat_id = %s ORDER BY created_at, id",
                            (seat_id,)).fetchall()


def sync_get(server: Server, path: str):
    r = httpx.get(server.url + path, timeout=30)
    assert r.status_code == 200, r.text
    return r.json()


def seats_of(server: Server, show: str) -> list:
    """좌석맵(보기 전용 사용자 + 입장권)."""
    viewer = "viewer"
    token = server.grant_tickets([viewer], show)[viewer]
    r = httpx.get(f"{server.url}/shows/{show}/seats", headers={"X-User-Id": viewer, "X-Queue-Ticket": token}, timeout=30)
    assert r.status_code == 200, r.text
    return r.json()


def first_seat(server: Server) -> str:
    return seats_of(server, show_of(server))[0]["id"]


def seat_status(server: Server, seat_id: str) -> str:
    return next(s["status"] for s in seats_of(server, show_of(server)) if s["id"] == seat_id)
