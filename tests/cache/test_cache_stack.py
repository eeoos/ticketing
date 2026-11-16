"""앱 3대 + Nginx 스택에서 캐시 공유 (docs/cache.md, docs/infra.md 절차). 9장부터 좌석맵·홀드는 입장권이 필요하다.

분리 DB(ticketing_cache) + 테스트용 Redis DB(2)·실행별 접두사로 앱을 다시 띄우고, 모듈이 끝나면(실패해도) 기본값으로 되돌린 뒤
상태·환경변수를 확인한다(복구 실패는 teardown error). 입장권은 Redis 에 직접 발급한다(docs/queue.md).
"""
from collections import Counter

import pytest

from cache_support import Stack, uid


@pytest.fixture(scope="module")
def stack():
    s = Stack()
    yield s
    s.finalize()


def app_of(resp):
    return resp.headers.get("X-App-Id")


def view(stack, show):
    """좌석맵 조회. 게이트웨이는 X-User-Id 별 초당 5건(burst 10)으로 제한하므로 요청마다 새 사용자·입장권을 쓴다."""
    user = uid("viewer")
    return stack.client.get(f"/shows/{show}/seats", headers={"X-User-Id": user, "X-Queue-Ticket": stack.grant_ticket(user, show)})


# ---- 2. 다른 앱이 받아도 캐시를 공유 ----
def test_cache_stack_apps_share_one_seatmap_cache(stack):
    stack.ensure()
    show = stack.show_id()
    first = view(stack, show)
    assert first.status_code == 200 and first.headers.get("X-Cache") == "MISS", (first.status_code, first.headers.get("X-Cache"))
    filler = app_of(first)
    results = [view(stack, show) for _ in range(12)]
    assert [r.headers.get("X-Cache") for r in results] == ["HIT"] * 12, [r.headers.get("X-Cache") for r in results]
    others = {app_of(r) for r in results} - {filler}
    assert len(others) >= 2, f"채운 앱({filler}) 말고 다른 앱 둘 이상이 HIT 으로 답해야 한다: {Counter(app_of(r) for r in results)}"


def test_cache_stack_hold_via_one_app_is_visible_through_every_app(stack):
    stack.ensure()
    show = stack.show_id()
    first = view(stack, show).json()
    seat = next(s["id"] for s in first if s["status"] == "AVAILABLE")
    for _ in range(4):  # 캐시를 채운다
        view(stack, show)
    holder = uid("A")
    hold = stack.client.post(f"/seats/{seat}/hold", headers={"X-User-Id": holder, "X-Queue-Ticket": stack.grant_ticket(holder, show)})
    assert hold.status_code == 201, hold.text
    holding_app = app_of(hold)
    results = [view(stack, show) for _ in range(9)]
    for r in results:
        assert {s["id"]: s["status"] for s in r.json()}[seat] == "HELD", f"{app_of(r)} 가 낡은 캐시를 줬다"
    assert results[0].headers.get("X-Cache") == "MISS", "홀드가 무효화했으니 첫 조회는 MISS"
    assert [r.headers.get("X-Cache") for r in results[1:]] == ["HIT"] * 8
    assert len({app_of(r) for r in results} - {holding_app}) >= 1, "홀드한 앱과 다른 앱이 같은 새 캐시를 써야 한다"
