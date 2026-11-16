"""Redis 를 멈춘 상태 (docs/cache.md 설정 표: Redis 오류는 요청 단위로 BYPASS). 복구 실패는 teardown error."""
import os
import re
import subprocess
import sys
import time

from cache_support import ROOT, TEST_REDIS_DB, REDIS_HOST_URL, new_prefix, uid


def test_cache_redis_stopped_requests_bypass_quickly_without_5xx(redis_down, make_server):
    redis_down.stop()
    srv = make_server(redis_url=f"{REDIS_HOST_URL}/{TEST_REDIS_DB}", timeout_ms=200)
    show, seat, user = srv.show_id(), srv.seat_ids(1)[0], uid("A")
    started = time.monotonic()
    for _ in range(2):
        resp = srv.seatmap(show)
        assert resp.status_code == 200 and resp.headers.get("X-Cache") == "BYPASS", (resp.status_code, resp.headers.get("X-Cache"))
    ok = srv.hold(seat, user)
    assert ok.status_code == 201 and ok.headers.get("X-Hold-Gate") == "BYPASS", (ok.status_code, ok.headers.get("X-Hold-Gate"), ok.text)
    dup = srv.hold(seat, uid("B"))
    assert dup.status_code == 409 and dup.json()["code"] == "SEAT_HELD" and dup.headers.get("X-Hold-Gate") == "BYPASS"
    pay = srv.pay(seat, user)
    assert pay.status_code == 202, pay.text
    assert srv.callback(pay.json()["paymentId"], "SUCCEEDED").json()["booking"]["status"] == "CONFIRMED"
    assert srv.statuses(srv.seatmap(show))[seat] == "SOLD"
    assert time.monotonic() - started < 10, "Redis 가 꺼져 있어도 시간 제한 안에 포기하고 DB 로 처리해야 한다"


def test_cache_redis_stopped_existing_concurrency_scenarios_still_pass(redis_down, report):
    """tests/concurrency(C-01~C-07)를 하위 pytest 로 그대로 돌린다. 서버는 REDIS_URL 을 받은 채 뜨지만 Redis 는 꺼져 있다.
    직접 재사용 대신 하위 pytest 를 쓴 이유: 시나리오·겹침 증거 판정을 한 줄도 바꾸지 않고 그대로 검증하기 위해서(환경변수는
    tests/concurrency 의 uvicorn 하위 프로세스로 상속된다)."""
    redis_down.stop()
    env = dict(os.environ, REDIS_URL=f"{REDIS_HOST_URL}/{TEST_REDIS_DB}", REDIS_KEY_PREFIX=new_prefix(), REDIS_TIMEOUT_MS="200")
    p = subprocess.run([sys.executable, "-m", "pytest", "tests/concurrency", "-q", "-p", "no:cacheprovider"],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    tail = "\n".join(p.stdout.splitlines()[-25:])
    summary = re.findall(r"\d+ (?:passed|failed)[^\n]*", p.stdout)
    report("Redis 정지 + tests/concurrency", summary[-1] if summary else "결과 줄 없음")
    assert p.returncode == 0, f"Redis 가 꺼진 상태에서 동시성 시나리오가 실패했다:\n{tail}"
    assert re.search(r"\b7 passed\b", p.stdout), f"C-01~C-07 7개가 모두 통과해야 한다:\n{tail}"
