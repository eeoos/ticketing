"""Lua·버전 키의 경쟁 순서를 결정적으로 재현하는 회귀 (backend.cache 함수를 정해진 순서로 직접 호출).

실제 Redis(테스트용 DB 1)·실행별 접두사. 시각 인자는 get_now 에 해당하는 값을 테스트가 정한다.
이 테스트들은 버전 비교·token 검사를 지운 변형 구현에서는 실패해야 한다.
"""
import json
from datetime import timedelta

import pytest

from cache_support import REDIS_HOST_URL, T0, TEST_REDIS_DB


@pytest.fixture
def cache(monkeypatch, prefix, r):
    monkeypatch.setenv("REDIS_URL", f"{REDIS_HOST_URL}/{TEST_REDIS_DB}")
    monkeypatch.setenv("REDIS_KEY_PREFIX", prefix)
    monkeypatch.setenv("HOLD_GATE", "on")
    monkeypatch.setenv("REDIS_TIMEOUT_MS", "1000")
    from backend import cache as c  # noqa: PLC0415
    return c


BODY = [{"id": "s1", "section": "A", "row": "1", "number": 1, "status": "AVAILABLE"}]


def seat_value(r, prefix, seat):
    raw = r.get(f"{prefix}hold:{seat}")
    return None if raw is None else json.loads(raw)


# ---- 좌석맵: 무효화 → 늦은 put ----
def test_cache_order_invalidate_then_late_put_stores_nothing(cache, prefix, r):
    show, now = "show-1", T0
    state, body, ver = cache.seatmap_read(show, now)  # DB 읽기 전에 버전을 얻는다
    assert state == "MISS" and body is None and ver is not None
    cache.seatmap_invalidate(show)  # 그 사이 다른 요청이 상태를 바꾸고 무효화
    cache.seatmap_put(show, BODY, now + timedelta(seconds=5), now, ver)  # 낡은 DB 읽기 결과를 늦게 저장 시도
    assert r.exists(f"{prefix}seatmap:{show}") == 0, "무효화 뒤 늦게 도착한 put 은 저장되면 안 된다(버전이 달라졌다)"
    assert cache.seatmap_read(show, now)[0] == "MISS"


def test_cache_order_put_with_the_current_version_is_stored(cache, prefix, r):
    show, now = "show-1", T0
    cache.seatmap_invalidate(show)  # 버전을 올려 둔 뒤
    state, _, ver = cache.seatmap_read(show, now)  # 최신 버전을 읽고
    cache.seatmap_put(show, BODY, now + timedelta(seconds=5), now, ver)  # 같은 버전이면 저장
    assert state == "MISS" and r.exists(f"{prefix}seatmap:{show}") == 1
    state, body, _ = cache.seatmap_read(show, now)
    assert state == "HIT" and body == BODY


def test_cache_order_two_invalidations_after_read_still_block_the_put(cache, prefix, r):
    show, now = "show-1", T0
    _, _, ver = cache.seatmap_read(show, now)
    cache.seatmap_invalidate(show)
    cache.seatmap_invalidate(show)
    cache.seatmap_put(show, BODY, now + timedelta(seconds=5), now, ver)
    assert r.exists(f"{prefix}seatmap:{show}") == 0


# ---- 관문: 이전 token 의 release/confirm 은 다른 token 의 표시를 건드리지 않는다 ----
def test_cache_order_old_token_release_and_confirm_leave_a_new_pending_mark(cache, prefix, r):
    seat, now = "seat-1", T0
    assert cache.gate(seat, "A", "tok-old", now)[0] == "PASS"  # 내 요청이 PENDING 을 잡음
    r.delete(f"{prefix}hold:{seat}")  # (2초 TTL 이 지나 표시가 사라졌다고 가정)
    assert cache.gate(seat, "B", "tok-new", now)[0] == "PASS"  # 다른 요청이 새 PENDING 을 잡음
    before = seat_value(r, prefix, seat)
    assert before["state"] == "PENDING" and before["token"] == "tok-new" and before["owner"] == "B"
    cache.gate_release(seat, "tok-old")  # 늦게 도착한 이전 token 의 되돌리기
    assert seat_value(r, prefix, seat) == before, "이전 token 의 release 가 새 PENDING 을 지웠다"
    cache.gate_confirm(seat, "A", "tok-old", now, now + timedelta(minutes=5))  # 늦은 이전 token 의 확정
    assert seat_value(r, prefix, seat) == before, "이전 token 의 confirm 이 새 PENDING 을 HELD 로 바꿨다"
    cache.gate_confirm(seat, "B", "tok-new", now, now + timedelta(minutes=5))  # 내 token 은 정상 동작
    assert seat_value(r, prefix, seat)["state"] == "HELD" and seat_value(r, prefix, seat)["owner"] == "B"


def test_cache_order_old_token_does_not_touch_a_held_mark_of_another_token(cache, prefix, r):
    seat, now = "seat-1", T0
    assert cache.gate(seat, "A", "tok-old", now)[0] == "PASS"
    r.delete(f"{prefix}hold:{seat}")
    assert cache.gate(seat, "B", "tok-new", now)[0] == "PASS"
    cache.gate_confirm(seat, "B", "tok-new", now, now + timedelta(minutes=5))  # B 가 HELD 로 확정
    held = seat_value(r, prefix, seat)
    cache.gate_release(seat, "tok-old")
    cache.gate_confirm(seat, "A", "tok-old", now, now + timedelta(minutes=5))
    assert seat_value(r, prefix, seat) == held, "이전 token 이 B 의 HELD 표시를 바꿨다"


# ---- 관문: SOLD 뒤 늦은 confirm/release ----
def test_cache_order_late_confirm_and_release_after_mark_sold_keep_sold(cache, prefix, r):
    seat, now = "seat-1", T0
    assert cache.gate(seat, "A", "tok-a", now)[0] == "PASS"
    cache.mark_sold(seat, "A")  # 결제 성공 콜백이 SOLD 로 표시
    sold = seat_value(r, prefix, seat)
    assert sold["state"] == "SOLD"
    cache.gate_confirm(seat, "A", "tok-a", now, now + timedelta(minutes=5))  # 늦게 도착한 홀드 확정
    assert seat_value(r, prefix, seat) == sold, "mark_sold 뒤 늦은 confirm 이 SOLD 를 HELD 로 되돌렸다"
    cache.gate_release(seat, "tok-a")
    assert seat_value(r, prefix, seat) == sold, "mark_sold 뒤 늦은 release 가 SOLD 표시를 지웠다"
    assert 0 < r.pttl(f"{prefix}hold:{seat}") <= 60_000
    assert cache.gate(seat, "B", "tok-b", now) == ("REJECT", "SEAT_SOLD")


# ---- 관문 판정 자체(Lua 직렬 규칙) ----
def test_cache_order_gate_second_caller_is_rejected_while_pending_and_held(cache, prefix, r):
    seat, now = "seat-1", T0
    assert cache.gate(seat, "A", "tok-a", now) == ("PASS", None)
    assert cache.gate(seat, "B", "tok-b", now) == ("REJECT", "SEAT_HELD"), "PENDING 중에는 거절"
    cache.gate_confirm(seat, "A", "tok-a", now, now + timedelta(minutes=5))
    assert cache.gate(seat, "B", "tok-b", now + timedelta(minutes=4, seconds=59)) == ("REJECT", "SEAT_HELD")
    assert cache.gate(seat, "B", "tok-b", now + timedelta(minutes=5))[0] == "PASS", "만료 시각부터는 표시를 무시"
