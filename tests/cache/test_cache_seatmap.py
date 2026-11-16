"""좌석맵 캐시 (docs/cache.md ①). get_now 는 파일 시계로 조종하고 Redis TTL 은 실제 시계로 흐른다."""
from datetime import timedelta

from cache_support import T0, ClockServer, ms, read_value, uid


def xcache(resp):
    return resp.headers.get("X-Cache")


def entry_key(srv, show_id):
    return f"{srv.prefix}seatmap:{show_id}"


# ---- 1. MISS → HIT ----
def test_cache_seatmap_first_request_is_miss_then_hit(server):
    show = server.show_id()
    first, second, third = server.seatmap(show), server.seatmap(show), server.seatmap(show)
    assert [first.status_code, second.status_code, third.status_code] == [200, 200, 200]
    assert [xcache(first), xcache(second), xcache(third)] == ["MISS", "HIT", "HIT"], \
        [first.headers.get("X-Cache"), second.headers.get("X-Cache"), third.headers.get("X-Cache")]
    assert first.json() == second.json() == third.json()
    assert len(first.json()) == 100


def test_cache_seatmap_unknown_show_is_404_and_never_cached(server):
    for _ in range(2):
        r = server.seatmap("no-such-show")
        assert r.status_code == 404 and r.json()["code"] == "NOT_FOUND"
        assert xcache(r) != "HIT"


# ---- 시각 규칙: 값 안의 유효 시각은 get_now 기준, Redis TTL 은 청소용 ----
def test_cache_seatmap_entry_stores_valid_until_from_get_now_not_wall_clock(server, r):
    show = server.show_id()
    assert xcache(server.seatmap(show)) == "MISS"
    entry = read_value(r, entry_key(server, show))
    assert entry is not None, "MISS 뒤 좌석맵 항목이 Redis 에 저장돼야 한다"
    assert int(entry["validUntilMs"]) == ms(T0 + timedelta(seconds=5)), entry.get("validUntilMs")  # get_now+5초(2030년)
    assert "body" in entry
    pttl = r.pttl(entry_key(server, show))
    assert pttl > 0, "청소용 PX TTL 이 있어야 한다"


def test_cache_seatmap_entry_past_valid_until_is_ignored_even_if_redis_ttl_remains(server, r):
    show = server.show_id()
    assert xcache(server.seatmap(show)) == "MISS"
    assert xcache(server.seatmap(show)) == "HIT"
    assert r.pttl(entry_key(server, show)) > 0
    server.set_time(T0 + timedelta(seconds=6))  # 실제 시계로는 거의 안 지났지만 get_now 로는 유효 시각이 지났다
    assert xcache(server.seatmap(show)) == "MISS"


# ---- 3. 홀드·결제 성공 뒤 바로 반영 ----
def test_cache_seatmap_hold_invalidates_cache_and_shows_held_immediately(server):
    show, seat = server.show_id(), server.seat_ids(1)[0]
    server.set_time(T0)
    assert xcache(server.seatmap(show)) == "MISS"  # seat_ids 는 DB 에서 읽으므로 캐시는 비어 있다
    warm = server.seatmap(show)
    assert xcache(warm) == "HIT" and server.statuses(warm)[seat] == "AVAILABLE"  # 캐시가 있다
    assert server.hold(seat, uid("A")).status_code == 201
    after = server.seatmap(show)
    assert server.statuses(after)[seat] == "HELD", "홀드 직후 조회는 HELD 여야 한다(캐시가 있었어도)"
    assert xcache(after) == "MISS", "홀드가 커밋된 뒤 캐시 항목이 지워져야 한다"
    assert xcache(server.seatmap(show)) == "HIT"


def test_cache_seatmap_payment_request_and_failed_callback_do_not_invalidate(server):
    show, seat, user = server.show_id(), server.seat_ids(1)[0], uid("A")
    assert server.hold(seat, user).status_code == 201
    assert xcache(server.seatmap(show)) == "MISS" and xcache(server.seatmap(show)) == "HIT"
    pay = server.pay(seat, user)
    assert pay.status_code == 202
    assert xcache(server.seatmap(show)) == "HIT", "결제 요청(PENDING)은 좌석을 바꾸지 않으므로 캐시를 지우지 않는다"
    assert server.callback(pay.json()["paymentId"], "FAILED").status_code == 200
    again = server.seatmap(show)
    assert xcache(again) == "HIT" and server.statuses(again)[seat] == "HELD", "FAILED 콜백도 캐시를 지우지 않는다"


def test_cache_seatmap_success_callback_invalidates_and_shows_sold_immediately(server):
    show, seat, user = server.show_id(), server.seat_ids(1)[0], uid("A")
    assert server.hold(seat, user).status_code == 201
    pay = server.pay(seat, user)
    assert pay.status_code == 202
    assert xcache(server.seatmap(show)) == "MISS" and xcache(server.seatmap(show)) == "HIT"
    assert server.callback(pay.json()["paymentId"], "SUCCEEDED").json()["booking"]["status"] == "CONFIRMED"
    after = server.seatmap(show)
    assert server.statuses(after)[seat] == "SOLD", "성공 콜백 직후 조회는 SOLD 여야 한다(캐시가 있었어도)"
    assert xcache(after) == "MISS"


# ---- 4. 홀드 만료 시각부터 AVAILABLE (캐시가 있었어도) ----
def test_cache_seatmap_hit_still_held_before_expiry_and_available_at_expiry(server, r):
    show, seat = server.show_id(), server.seat_ids(1)[0]
    assert server.hold(seat, uid("A")).status_code == 201  # T0 에 홀드, 만료 T0+5:00
    first = server.seatmap(show)
    assert xcache(first) == "MISS" and server.statuses(first)[seat] == "HELD"
    server.set_time(T0 + timedelta(minutes=4, seconds=57))  # 만료 3초 전에 채운다: 상한이 없으면 유효 시각이 T0+5:02
    warm = server.seatmap(show)
    assert xcache(warm) == "MISS" and server.statuses(warm)[seat] == "HELD"
    entry = read_value(r, entry_key(server, show))
    assert int(entry["validUntilMs"]) == ms(T0 + timedelta(minutes=5)), \
        f"유효 시각은 min(now+5초, 가장 이른 HELD 만료=T0+5:00) 여야 한다: {entry['validUntilMs']}"
    server.set_time(T0 + timedelta(minutes=4, seconds=59))
    hit = server.seatmap(show)
    assert xcache(hit) == "HIT" and server.statuses(hit)[seat] == "HELD", "만료 1초 전에는 HIT 이어도 HELD"
    server.set_time(T0 + timedelta(minutes=5))  # now == expiresAt
    expired = server.seatmap(show)
    assert server.statuses(expired)[seat] == "AVAILABLE", "만료 시각부터는 캐시 응답도 AVAILABLE"
    assert xcache(expired) == "MISS", "항목은 만료 시각에 무효가 돼 DB 에서 다시 계산한다"
    server.set_time(T0 + timedelta(minutes=5, seconds=1))
    later = server.seatmap(show)
    assert xcache(later) == "HIT" and server.statuses(later)[seat] == "AVAILABLE"


def test_cache_seatmap_valid_until_is_the_earliest_held_expiry_of_several_holds(server, r):
    show = server.show_id()
    s1, s2 = server.seat_ids(2)
    assert server.hold(s1, uid("A")).status_code == 201  # 만료 T0+5:00
    server.set_time(T0 + timedelta(minutes=2))
    assert server.hold(s2, uid("B")).status_code == 201  # 만료 T0+7:00
    server.set_time(T0 + timedelta(minutes=4, seconds=58))
    assert xcache(server.seatmap(show)) == "MISS"
    entry = read_value(r, entry_key(server, show))
    assert int(entry["validUntilMs"]) == ms(T0 + timedelta(minutes=5)), entry["validUntilMs"]  # 둘 중 이른 쪽
    server.set_time(T0 + timedelta(minutes=5))
    st = server.statuses(server.seatmap(show))
    assert st[s1] == "AVAILABLE" and st[s2] == "HELD"


def test_cache_seatmap_without_held_seats_is_valid_for_five_seconds_of_get_now(server):
    show = server.show_id()
    assert xcache(server.seatmap(show)) == "MISS"
    server.set_time(T0 + timedelta(seconds=4, milliseconds=900))
    assert xcache(server.seatmap(show)) == "HIT"
    server.set_time(T0 + timedelta(seconds=5))  # now == validUntil → 무효
    assert xcache(server.seatmap(show)) == "MISS"


# ---- 2'. 프로세스(앱) 둘이 캐시·무효화를 공유 ----
def test_cache_seatmap_two_server_processes_share_cache_and_invalidation(make_server, prefix):
    a = make_server()
    b = make_server(clock_file=a.clock_file)  # 같은 시계·DB·Redis·접두사의 두 번째 프로세스
    show, seat = a.show_id(), a.seat_ids(1)[0]
    assert xcache(a.seatmap(show)) == "MISS"
    assert xcache(b.seatmap(show)) == "HIT", "다른 프로세스가 채운 캐시를 공유해야 한다"
    assert b.hold(seat, uid("A")).status_code == 201  # b 가 무효화
    seen = a.seatmap(show)
    assert a.statuses(seen)[seat] == "HELD" and xcache(seen) == "MISS", "b 의 홀드가 a 의 캐시도 지운다"
