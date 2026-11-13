"""tests/concurrency 공용 도구. 기준: docs/concurrency.md

실제 uvicorn(워커 4) 하위 프로세스 + PostgreSQL. TestClient/시각 오버라이드 없음.
겹침 증거(HTTP 최대 동시 진행 수, pg active 최댓값, 서로 다른 application_name 수)를 모은다.
"""
import asyncio
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
        self.dsn, self.port = dsn, free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.log = tempfile.NamedTemporaryFile("w+", prefix="uvicorn-", suffix=".log", delete=False)
        self.proc = None

    def start(self, timeout=60):
        env = dict(os.environ, DATABASE_URL=self.dsn)
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

    def stop(self):
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
        c = Counter()
        for r in self.results:
            s = r.status
            c["error" if s is None else "5xx" if s >= 500 else s if s in (201, 409) else "other"] += 1
        return dict(c)

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


async def _fire(server_url: str, requests: list[dict]) -> list[Result]:
    n = len(requests)
    limits = httpx.Limits(max_connections=n + 50, max_keepalive_connections=n + 50)
    gate = asyncio.Event()
    async with httpx.AsyncClient(base_url=server_url, limits=limits, timeout=httpx.Timeout(120)) as client:
        async def one(spec) -> Result:
            await gate.wait()
            start = time.perf_counter()
            try:
                resp = await client.request(**spec)
                try:
                    body = resp.json()
                except ValueError:
                    body = resp.text
                return Result(resp.status_code, body, start, time.perf_counter())
            except httpx.HTTPError as e:
                return Result(None, None, start, time.perf_counter(), error=f"{type(e).__name__}: {e}")

        tasks = [asyncio.create_task(one(s)) for s in requests]
        await asyncio.sleep(0.5)  # 모든 태스크가 배리어에서 대기하도록
        gate.set()  # 시작 신호
        return await asyncio.gather(*tasks)


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


def sync_get(server: Server, path: str):
    r = httpx.get(server.url + path, timeout=30)
    assert r.status_code == 200, r.text
    return r.json()


def first_seat(server: Server) -> str:
    show = sync_get(server, "/shows")[0]["id"]
    return sync_get(server, f"/shows/{show}/seats")[0]["id"]


def seat_status(server: Server, seat_id: str) -> str:
    show = sync_get(server, "/shows")[0]["id"]
    return next(s["status"] for s in sync_get(server, f"/shows/{show}/seats") if s["id"] == seat_id)
