"""앱 3대 + Nginx 스택에서 대기열·입장권 (docs/queue.md). 분리 DB·Redis DB 6·실행별 접두사, 끝나면 기본값으로 복구(실패하면 teardown error)."""
import json
import time
from collections import Counter

import pytest

from cache_support import Stack, uid
from queue_support import fire_urls
from ticket_support import DB_QUEUE_STACK, redis_for

GATEWAY = "http://localhost:8080"


@pytest.fixture(scope="module")
def stack():
    s = Stack(redis_db=DB_QUEUE_STACK)
    yield s
    s.finalize()


def test_queue_stack_1000_users_through_the_gateway_get_numbers_1_to_1000_exactly_once(stack):
    stack.ensure()
    show = stack.show_id()
    users = [uid("u") for _ in range(1000)]
    responses = fire_urls([dict(method="POST", url=f"{GATEWAY}/shows/{show}/queue", headers={"X-User-Id": u}) for u in users])
    bad = [x for x in responses if isinstance(x, Exception)]
    assert not bad, f"응답 없는 요청 {len(bad)}건: {bad[:2]}"
    assert Counter(x.status_code for x in responses) == {201: 1000}, Counter(x.status_code for x in responses)
    apps = Counter(x.headers.get("X-App-Id") for x in responses)
    assert set(apps) == {"app1", "app2", "app3"}, f"세 앱 모두 줄 서기를 처리해야 한다: {dict(apps)}"
    numbers = sorted(x.json()["number"] for x in responses)
    assert numbers == list(range(1, 1001)), f"겹침·빠짐(최대 {numbers[-1]})"
    assert int(redis_for(DB_QUEUE_STACK).get(f"{stack.prefix}queue:{show}:seq")) == 1000


def test_queue_stack_a_ticket_obtained_through_one_app_works_in_every_app(stack):
    stack.ensure()
    show, user = stack.show_id(), uid("A")
    joined = stack.client.post(f"/shows/{show}/queue", headers={"X-User-Id": user})
    assert joined.status_code in (200, 201), joined.text[:200]
    time.sleep(1.5)  # 실제 시계: 1초 구간이 지나야 전광판이 올라 입장할 수 있다
    me = stack.client.get(f"/shows/{show}/queue/me", headers={"X-User-Id": user})
    assert me.status_code == 200 and me.json()["status"] == "ADMITTED", me.text[:300]
    token = me.json()["ticket"]["token"]
    headers = {"X-User-Id": user, "X-Queue-Ticket": token}
    # 게이트웨이는 X-User-Id 별 초당 5건(burst 10)으로 제한한다: 이 사용자는 줄서기·/me 포함 8건만 쓴다
    results = [stack.client.get(f"/shows/{show}/seats", headers=headers) for _ in range(6)]
    assert [r.status_code for r in results] == [200] * 6, [(r.status_code, r.text[:80]) for r in results]
    assert len({r.headers.get("X-App-Id") for r in results}) >= 2, "여러 앱이 같은 입장권을 검증해야 한다"
    seat = next(s["id"] for s in results[0].json() if s["status"] == "AVAILABLE")
    held = stack.client.post(f"/seats/{seat}/hold", headers=headers)
    assert held.status_code == 201, held.text[:200]
    other = stack.client.get(f"/shows/{show}/seats", headers={"X-User-Id": uid("B"), "X-Queue-Ticket": token})
    assert other.status_code == 403 and other.json()["code"] == "QUEUE_TICKET_INVALID"
