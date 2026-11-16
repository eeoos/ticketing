"""US-02 좌석맵 보기 (docs/stories/US-02-seat-map.md)"""
from api_support import HOLD, assert_error  # noqa: F401


def test_us02_ac1_all_seats_with_id_position_status(api):
    r = api.seats_request()  # 9장: X-User-Id 와 그 공연의 유효한 입장권이 필요하다
    assert r.status_code == 200, r.text
    seats = r.json()
    assert len(seats) == 100
    assert len({s["id"] for s in seats}) == 100
    for s in seats:
        assert isinstance(s["id"], str) and s["id"]
        assert isinstance(s["section"], str) and s["section"]
        assert isinstance(s["row"], str) and s["row"]
        assert isinstance(s["number"], int) and s["number"] >= 1
        assert s["status"] == "AVAILABLE"
    assert len({(s["section"], s["row"], s["number"]) for s in seats}) == 100  # 위치 중복 없음


def test_us02_ac2_held_seat_shows_held_before_expiry(api):
    seat = api.first_seat()
    assert api.hold(seat, user="A").status_code == 201
    api.advance(minutes=4, seconds=59)
    seats = {s["id"]: s["status"] for s in api.seat_map()}
    assert seats[seat] == "HELD"
    assert sum(v == "AVAILABLE" for v in seats.values()) == 99


def test_us02_ac3_expired_hold_shows_available(api):
    seat = api.first_seat()
    assert api.hold(seat, user="A").status_code == 201
    api.now += HOLD  # now == expiresAt 이면 만료
    assert api.status_of(seat) == "AVAILABLE"
    api.advance(minutes=30)
    assert api.status_of(seat) == "AVAILABLE"


def test_us02_ac4_sold_seat_shows_sold(api):
    seat = api.first_seat()
    api.sell(seat, "A")  # 홀드 → 결제 요청 → SUCCEEDED 콜백
    seats = {s["id"]: s["status"] for s in api.seat_map()}
    assert seats[seat] == "SOLD"
    assert sum(v == "AVAILABLE" for v in seats.values()) == 99


def test_us02_ac5_unknown_show_ends_in_403_because_no_ticket_can_exist_for_it(api):
    """9장 변경: 없는 공연의 입장권은 발급될 수 없으므로(줄 서기가 404) 조회는 입장권 검증에서 403 으로 끝난다."""
    api.show_id()  # 환경 준비
    missing = api.seats_request("no-such-show", user="A", ticket=None)
    assert_error(missing, 403, "QUEUE_TICKET_REQUIRED")
    other_show_ticket = api.ticket("A")  # 있는 공연의 입장권을 없는 공연에 써도
    assert_error(api.seats_request("no-such-show", user="A", ticket=other_show_ticket), 403, "QUEUE_TICKET_INVALID")


def test_us02_contract_nul_in_show_id_is_403_ticket_rejection_not_5xx(api):
    """NUL 이 든 공연 id 는 어떤 입장권도 맞지 않으므로 403(REQUIRED/INVALID) — 5xx 가 아니다."""
    api.show_id()
    ticket = api.ticket("A")
    for path in ("/shows/%00/seats", f"/shows/{api.show_id()}%00/seats"):
        no_ticket = api.request("GET", path, user="A")
        assert_error(no_ticket, 403, "QUEUE_TICKET_REQUIRED")
        wrong = api.request("GET", path, user="A", headers={"X-Queue-Ticket": ticket})
        assert_error(wrong, 403, "QUEUE_TICKET_INVALID")


def test_us02_ac2_pending_payment_keeps_seat_held(api):
    seat = api.first_seat()
    api.hold_and_pay(seat, "A")  # 결제 요청만으로는 팔리지 않는다
    assert api.status_of(seat) == "HELD"


def test_us02_ac3_refund_required_late_success_shows_available(api):
    seat = api.first_seat()
    _, b = api.hold_and_pay(seat, "A")
    api.now += HOLD
    assert api.callback(b["paymentId"], "SUCCEEDED").json()["booking"]["status"] == "REFUND_REQUIRED"
    assert api.status_of(seat) == "AVAILABLE"


# ---- 9장 변경: 좌석맵은 X-User-Id 와 입장권이 필요하다 (docs/stories/US-02 '9장 변경') ----
def test_us02_ac6_missing_user_header_is_401_before_the_ticket_check(api):
    show = api.show_id()
    assert_error(api.request("GET", f"/shows/{show}/seats"), 401, "UNAUTHENTICATED")  # 입장권도 없지만 401 이 먼저
    assert_error(api.seats_request(show, user=None, ticket=api.ticket("A")), 401, "UNAUTHENTICATED")


def test_us02_ac7_missing_ticket_is_403_required_and_the_cache_is_not_consulted(api):
    show = api.show_id()
    assert_error(api.seats_request(show, user="A", ticket=None), 403, "QUEUE_TICKET_REQUIRED")
    r = api._redis
    assert r.exists(f"{__import__('api_support').API_REDIS_PREFIX}seatmap:{show}") == 0, "거절된 요청이 캐시를 채웠다(검증이 캐시보다 먼저여야 한다)"


def test_us02_ac8_ticket_of_another_user_or_another_show_or_unknown_token_is_invalid(api):
    show = api.show_id()
    others_ticket = api.ticket("B")
    assert_error(api.seats_request(show, user="A", ticket=others_ticket), 403, "QUEUE_TICKET_INVALID")  # 남의 입장권
    assert_error(api.seats_request(show, user="A", ticket="no-such-token"), 403, "QUEUE_TICKET_INVALID")  # 모르는 token
    ticket_of_other_show = api.ticket("A", show_id="another-show")
    assert_error(api.seats_request(show, user="A", ticket=ticket_of_other_show), 403, "QUEUE_TICKET_INVALID")  # 다른 공연


def test_us02_ac8_expired_ticket_is_403_expired_at_exactly_expiry(api):
    show = api.show_id()
    from datetime import timedelta
    ticket = api.ticket("A", ttl=timedelta(minutes=2))  # 발급 T0 → 만료 T0+2:00
    api.advance(minutes=1, seconds=59)
    assert api.seats_request(show, user="A", ticket=ticket).status_code == 200  # 만료 1초 전
    api.advance(seconds=1)  # now == expiresAt
    assert_error(api.seats_request(show, user="A", ticket=ticket), 403, "QUEUE_TICKET_EXPIRED")
    api.advance(seconds=1)
    assert_error(api.seats_request(show, user="A", ticket=ticket), 403, "QUEUE_TICKET_EXPIRED")


def test_us02_ac8_validating_a_ticket_never_extends_its_expiry(api):
    from datetime import timedelta
    show = api.show_id()
    ticket = api.ticket("A", ttl=timedelta(minutes=2))
    for _ in range(3):  # 반복 사용
        api.advance(seconds=30)
        assert api.seats_request(show, user="A", ticket=ticket).status_code == 200
    api.advance(seconds=30)  # T0+2:00
    assert_error(api.seats_request(show, user="A", ticket=ticket), 403, "QUEUE_TICKET_EXPIRED")
