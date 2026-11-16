"""앱 3대 + Nginx 스택에서 캐시 공유 (docs/cache.md 테스트에서 쓰는 법, docs/infra.md 절차).

분리 DB(APP_DB_NAME=ticketing_cache) + 테스트용 Redis DB(APP_REDIS_DB=2)·접두사(APP_REDIS_KEY_PREFIX)로 앱을 다시 띄우고,
모듈이 끝나면(실패해도) 기본값으로 되돌린 뒤 상태·환경변수를 확인한다(복구 실패는 teardown error).
"""
import time
from collections import Counter

import httpx
import pytest

from cache_support import (APPS, CACHE_DSN, STACK_REDIS_DB, compose, db_name, delete_prefix, new_prefix, reset_database,
                           uid)

BASE_URL = "http://localhost:8080"


class Stack:
    def __init__(self):
        self.prefix = new_prefix()
        self.touched = False
        self.error = None
        self.client = None

    def ensure(self):
        if self.error:
            raise AssertionError(self.error)
        if self.client:
            return
        try:
            reset_database()
            self.touched = True
            compose("up", "-d", "--build", "--wait", *APPS, "nginx",
                    env_extra={"APP_DB_NAME": db_name(), "APP_REDIS_DB": str(STACK_REDIS_DB),
                               "APP_REDIS_KEY_PREFIX": self.prefix})
            self.client = httpx.Client(base_url=BASE_URL, timeout=30)
            self._wait_apps()
        except Exception as e:  # noqa: BLE001
            self.error = f"3대 스택을 준비하지 못했다: {type(e).__name__}: {str(e)[-1200:]}"
            raise AssertionError(self.error) from e

    def _wait_apps(self, timeout=60):
        seen, deadline = set(), time.monotonic() + timeout
        while time.monotonic() < deadline and seen != set(APPS):
            try:
                r = self.client.get("/shows")
                if r.status_code == 200 and r.headers.get("X-App-Id"):
                    seen.add(r.headers["X-App-Id"])
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        if seen != set(APPS):
            raise RuntimeError(f"세 앱이 모두 응답하지 않았다: {sorted(seen)}")

    def verify_default(self):
        problems = []
        for app in APPS:
            for var, expect in (("DATABASE_URL", "/ticketing"), ("REDIS_URL", "/0"), ("REDIS_KEY_PREFIX", "tkt:")):
                p = compose("exec", "-T", app, "printenv", var, check=False)
                val = p.stdout.strip()
                ok = val.endswith(expect) if var != "REDIS_KEY_PREFIX" else val == expect
                if p.returncode or not ok:
                    problems.append(f"{app}: {var}={val!r} (기본값 {expect} 아님)")
        ps = compose("ps", "--format", "json", check=False)
        import json
        states = {}
        for line in ps.stdout.splitlines():
            if line.strip().startswith("{"):
                row = json.loads(line)
                states[row.get("Service")] = row
        for svc in (*APPS, "nginx", "redis", "db"):
            row = states.get(svc)
            if not row or row.get("State") != "running" or row.get("Health") not in ("healthy", ""):
                problems.append(f"{svc}: {None if not row else (row.get('State'), row.get('Health'))} (running/healthy 아님)")
        return problems

    def finalize(self):
        if self.client:
            self.client.close()
        if not self.touched:
            return
        delete_prefix(self.prefix, STACK_REDIS_DB)
        errors = []
        p = compose("up", "-d", "--wait", *APPS, "nginx", drop_env=("APP_DB_NAME", "APP_REDIS_DB", "APP_REDIS_KEY_PREFIX"), check=False)
        if p.returncode:
            errors.append(f"기본 설정 복구(up) 실패: {(p.stdout + p.stderr).strip()[-300:]}")
        errors.extend(self.verify_default())
        if errors:
            raise RuntimeError("복구되지 않은 항목: " + " | ".join(errors))


@pytest.fixture(scope="module")
def stack():
    s = Stack()
    yield s
    s.finalize()


def app_of(resp):
    return resp.headers.get("X-App-Id")


# ---- 2. 다른 앱이 받아도 캐시를 공유 ----
def test_cache_stack_apps_share_one_seatmap_cache(stack):
    stack.ensure()
    show = stack.client.get("/shows").json()[0]["id"]
    first = stack.client.get(f"/shows/{show}/seats")
    assert first.status_code == 200 and first.headers.get("X-Cache") == "MISS", first.headers.get("X-Cache")
    filler = app_of(first)
    results = [stack.client.get(f"/shows/{show}/seats") for _ in range(12)]
    assert [r.headers.get("X-Cache") for r in results] == ["HIT"] * 12, [r.headers.get("X-Cache") for r in results]
    others = {app_of(r) for r in results} - {filler}
    assert len(others) >= 2, f"채운 앱({filler}) 말고 다른 앱 둘 이상이 HIT 으로 답해야 한다: {Counter(app_of(r) for r in results)}"


def test_cache_stack_hold_via_one_app_is_visible_through_every_app(stack):
    stack.ensure()
    show = stack.client.get("/shows").json()[0]["id"]
    seats = stack.client.get(f"/shows/{show}/seats").json()
    seat = next(s["id"] for s in seats if s["status"] == "AVAILABLE")
    for _ in range(4):  # 캐시를 채운다
        stack.client.get(f"/shows/{show}/seats")
    hold = stack.client.post(f"/seats/{seat}/hold", headers={"X-User-Id": uid("A")})
    assert hold.status_code == 201, hold.text
    holder = app_of(hold)
    results = [stack.client.get(f"/shows/{show}/seats") for _ in range(9)]
    for r in results:
        assert {s["id"]: s["status"] for s in r.json()}[seat] == "HELD", f"{app_of(r)} 가 낡은 캐시를 줬다"
    assert results[0].headers.get("X-Cache") == "MISS", "홀드가 무효화했으니 첫 조회는 MISS"
    assert [r.headers.get("X-Cache") for r in results[1:]] == ["HIT"] * 8
    assert len({app_of(r) for r in results} - {holder}) >= 1, "홀드한 앱과 다른 앱이 같은 새 캐시를 써야 한다"
