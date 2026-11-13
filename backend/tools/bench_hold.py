"""홀드 구현(조건부 UPDATE) 측정.

--mode http (기본): 서버(uvicorn 워커 4)에 클라이언트를 여러 프로세스로 나눠(기본 10×50) 같은 좌석 1개를 동시 홀드.
  각 프로세스는 연결을 미리 열고 정해진 시각에 동시에 시작한다(단일 프로세스 httpx 한계가 측정을 지배하지 않게).
--mode db: HTTP 없이 홀드 트랜잭션만 실행. 500 트랜잭션, 스레드 40 × 연결 40, 같은 좌석 1개.
공통: 측정마다 ticketing_bench 초기화(migrate+seed), 7회(첫 회는 예열 표시).

실행: uv run python -m backend.tools.bench_hold --mode http|db [--rounds 7] [--users 500] [--procs 10] [--out 파일]
"""
import argparse
import asyncio
import json
import multiprocessing as mp
import os
import platform
import queue
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DSN = "postgresql://ticketing:ticketing@localhost:5432/ticketing_bench"
WORKERS = 4
POOL_MAX = 10


def bench_dsn() -> str:
    dsn = os.environ.get("BENCH_DATABASE_URL", DEFAULT_DSN)
    if conninfo_to_dict(dsn)["dbname"] == "ticketing":
        raise SystemExit("BENCH_DATABASE_URL 이 개발 DB 'ticketing' 를 가리킨다. 별도 DB 를 써야 한다.")
    return dsn


def reset_db(dsn: str):
    name = conninfo_to_dict(dsn)["dbname"]
    with psycopg.connect(make_conninfo(dsn, dbname="postgres"), autocommit=True) as c:
        if not c.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
            c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    from backend import migrate, seed
    migrate.run(dsn)
    seed.run(dsn)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(dsn: str):
    port = free_port()
    log = tempfile.NamedTemporaryFile("w+", prefix="bench-uvicorn-", suffix=".log", delete=False)
    env = dict(os.environ, DATABASE_URL=dsn, DB_POOL_MAX_SIZE=str(POOL_MAX))
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--workers", str(WORKERS),
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"uvicorn 종료: {Path(log.name).read_text()[-1500:]}")
        try:
            if httpx.get(f"{url}/shows", timeout=2).status_code == 200:
                for _ in range(8):  # 예열
                    httpx.get(f"{url}/shows", timeout=5)
                return proc, url, log
        except httpx.HTTPError:
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError("uvicorn 준비 실패")


def stop_server(proc, log):
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    log.close()
    Path(log.name).unlink(missing_ok=True)


def _proc_main(url, seat, ids, start_at, q):
    """클라이언트 프로세스: 연결을 미리 열고 start_at(epoch 초)에 동시에 시작."""
    async def run():
        n = len(ids)
        limits = httpx.Limits(max_connections=n + 10, max_keepalive_connections=n + 10)
        async with httpx.AsyncClient(base_url=url, limits=limits, timeout=httpx.Timeout(120)) as client:
            await asyncio.gather(*[client.get("/shows") for _ in range(n)])  # 연결 미리 열기

            async def one(i):
                await asyncio.sleep(max(0.0, start_at - time.time()))
                t0 = time.time()
                try:
                    r = await client.post(f"/seats/{seat}/hold", headers={"X-User-Id": f"user-{i}"})
                    return r.status_code, t0, time.time()
                except httpx.HTTPError:
                    return None, t0, time.time()
            return await asyncio.gather(*[one(i) for i in ids])
    q.put(asyncio.run(run()))


def fire(url, seat, users, procs):
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    chunks = [list(range(i, users, procs)) for i in range(procs)]
    start_at = time.time() + 3.0 + 0.05 * procs  # 모든 프로세스가 연결을 열 시간
    ps = [ctx.Process(target=_proc_main, args=(url, seat, c, start_at, q)) for c in chunks]
    res, deadline = [], time.time() + 3.0 + 0.05 * procs + 180  # 시작 시각 + 요청 timeout(120s) 여유
    try:
        for p in ps:
            p.start()
        got = 0
        while got < len(ps):
            try:
                res.extend(q.get(timeout=1.0))
                got += 1
            except queue.Empty:
                dead = [p for p in ps if p.exitcode not in (None, 0)]
                if dead:
                    raise RuntimeError(f"클라이언트 프로세스 비정상 종료: exitcode={[p.exitcode for p in dead]}")
                if time.time() > deadline:
                    raise RuntimeError("클라이언트 결과 수신 timeout")
        for p in ps:
            p.join(timeout=10)
    finally:
        for p in ps:
            if p.is_alive():
                p.terminate()
                p.join(timeout=5)
    return res


def pct(sorted_ms, q):
    return sorted_ms[min(len(sorted_ms) - 1, int(round(q * (len(sorted_ms) - 1))))]


def summarize(res, with_inflight=True):
    lat = sorted((e - s) * 1000 for _, s, e in res)
    codes = [c for c, _, _ in res]
    total = max(e for _, _, e in res) - min(s for _, s, _ in res)
    out = {
        "201": codes.count(201), "409": codes.count(409),
        "5xx": sum(1 for c in codes if c is None or c >= 500),
        "other": sum(1 for c in codes if c is not None and c < 500 and c not in (201, 409)),
        "p50_ms": round(pct(lat, 0.5), 1), "p95_ms": round(pct(lat, 0.95), 1), "max_ms": round(lat[-1], 1),
        "total_s": round(total, 3), "throughput_rps": round(len(res) / total, 1),
    }
    if with_inflight:
        ev = sorted([(s, 1) for _, s, _ in res] + [(e, -1) for _, _, e in res], key=lambda x: (x[0], x[1]))
        cur = best = 0
        for _, d in ev:
            cur += d
            best = max(best, cur)
        out["max_inflight"] = best
    return out


def http_round(dsn, users, procs):
    reset_db(dsn)
    proc, url, log = start_server(dsn)
    try:
        show = httpx.get(f"{url}/shows").json()[0]["id"]
        seat = httpx.get(f"{url}/shows/{show}/seats").json()[0]["id"]
        res = fire(url, seat, users, procs)
    finally:
        stop_server(proc, log)
    return summarize(res)


def hold_txn(conn, seat, user, now):
    """main.py hold_seat 와 같은 조건부 UPDATE 를 한 트랜잭션으로. 반환: 201 | 409."""
    expires = now + timedelta(minutes=5)
    with conn.transaction():
        row = conn.execute(
            """UPDATE seats SET status = 'HELD', hold_user_id = %s, hold_expires_at = %s
               WHERE id = %s AND (status = 'AVAILABLE' OR (status = 'HELD' AND hold_expires_at <= %s))
               RETURNING id""", (user, expires, seat, now)).fetchone()
        if row:
            return 201
        conn.execute("SELECT status FROM seats WHERE id = %s", (seat,)).fetchone()
        return 409


def db_round(dsn, users, threads=40):
    reset_db(dsn)
    with psycopg.connect(dsn) as c:
        seat = c.execute("SELECT id FROM seats ORDER BY section, row_label, number LIMIT 1").fetchone()[0]
    conns = [psycopg.connect(dsn, autocommit=True, application_name="bench-db") for _ in range(threads)]
    free = list(conns)
    lock = threading.Lock()
    barrier = threading.Barrier(threads)

    def work(i):
        with lock:
            conn = free.pop()
        try:
            t0 = time.time()
            code = hold_txn(conn, seat, f"user-{i}", datetime.now(timezone.utc))
            return code, t0, time.time()
        finally:
            with lock:
                free.append(conn)

    try:
        for c in conns:  # 예열
            c.execute("SELECT 1")
        with ThreadPoolExecutor(threads) as ex:
            res = list(ex.map(work, range(users)))
    finally:
        for c in conns:
            c.close()
    return summarize(res, with_inflight=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=7)
    ap.add_argument("--users", type=int, default=500)
    ap.add_argument("--mode", choices=["http", "db"], default="http")
    ap.add_argument("--procs", type=int, default=10)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    dsn = bench_dsn()
    os.environ["DATABASE_URL"] = dsn
    with psycopg.connect(make_conninfo(dsn, dbname="postgres")) as c:
        pgver = c.execute("SHOW server_version").fetchone()[0]
    env = {"mode": a.mode, "client_procs": a.procs if a.mode == "http" else None,
           "db_concurrency": 40 if a.mode == "db" else None, "cpu_cores": os.cpu_count(), "postgresql": pgver, "python": platform.python_version(),
           "os": platform.platform(), "workers": WORKERS, "db_pool_max_size": POOL_MAX, "users": a.users,
           "client_server_same_machine": True}
    print(json.dumps({"env": env}, ensure_ascii=False))
    out = Path(a.out or f"/private/tmp/bench_hold_{a.mode}.jsonl")
    out.write_text(json.dumps({"env": env}, ensure_ascii=False) + "\n")
    rows = []
    for n in range(1, a.rounds + 1):
        r = http_round(dsn, a.users, a.procs) if a.mode == "http" else db_round(dsn, a.users)
        row = {"round": n, "warmup": n == 1, **r}
        rows.append(row)
        line = json.dumps(row, ensure_ascii=False)
        print(line, flush=True)
        with out.open("a") as f:
            f.write(line + "\n")
    print("\n## 요약 (예열 1회 제외)")
    m = [r for r in rows if not r["warmup"]]
    p50, p95 = [r["p50_ms"] for r in m], [r["p95_ms"] for r in m]
    print(json.dumps({
        "rounds": len(m),
        "p50_median_ms": statistics.median(p50), "p50_range_ms": [min(p50), max(p50)],
        "p95_median_ms": statistics.median(p95), "p95_range_ms": [min(p95), max(p95)],
        "throughput_median_rps": statistics.median(r["throughput_rps"] for r in m),
        "total_201": sum(r["201"] for r in m), "total_409": sum(r["409"] for r in m),
        "total_5xx": sum(r["5xx"] for r in m), "total_other": sum(r["other"] for r in m),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
