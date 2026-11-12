"""US-04 모의 결제로 예매 확정 (docs/stories/US-04-confirm-booking.md)"""
from api_support import HOLD, assert_error, parse_dt


def test_us04_ac1_holder_confirms_booking(api):
    seat = api.first_seat()
    assert api.hold(seat, user="A").status_code == 201
    api.advance(minutes=2)
    r = api.book(seat, user="A")
    assert r.status_code == 201, r.text
    b = r.json()
    assert isinstance(b["id"], str) and b["id"]
    assert b["seatId"] == seat and b["userId"] == "A"
    assert parse_dt(b["createdAt"]) == api.now
    assert api.status_of(seat) == "SOLD"
    assert api.booking_count() == 1


def test_us04_ac1_boundary_one_second_before_expiry_succeeds(api):
    seat = api.first_seat()
    assert api.hold(seat, user="A").status_code == 201
    api.advance(minutes=4, seconds=59)
    assert api.book(seat, user="A").status_code == 201


def test_us04_ac2_other_users_hold_cannot_be_confirmed(api):
    seat = api.first_seat()
    assert api.hold(seat, user="A").status_code == 201
    assert_error(api.book(seat, user="B"), 403, "HOLD_NOT_OWNED")
    assert api.status_of(seat) == "HELD"
    assert api.booking_count() == 0
    assert api.book(seat, user="A").status_code == 201  # 여전히 A 의 홀드


def test_us04_ac3_expired_hold_cannot_be_confirmed(api):
    seat = api.first_seat()
    assert api.hold(seat, user="A").status_code == 201
    api.now += HOLD  # now == expiresAt: 만료
    assert_error(api.book(seat, user="A"), 409, "HOLD_EXPIRED")
    assert api.status_of(seat) == "AVAILABLE"
    assert api.booking_count() == 0


def test_us04_ac4_seat_without_hold_cannot_be_confirmed(api):
    seat = api.first_seat()
    assert_error(api.book(seat, user="A"), 409, "NO_HOLD")
    assert api.status_of(seat) == "AVAILABLE"
    assert api.booking_count() == 0


def test_us04_ac5_sold_seat_cannot_be_confirmed_again(api):
    seat = api.first_seat()
    assert api.hold(seat, user="A").status_code == 201
    assert api.book(seat, user="A").status_code == 201
    assert_error(api.book(seat, user="A"), 409, "SEAT_SOLD")  # 구매자 본인 포함
    assert_error(api.book(seat, user="B"), 409, "SEAT_SOLD")
    assert api.status_of(seat) == "SOLD"
    assert api.booking_count() == 1


def test_us04_ac6_sold_seat_is_never_held_again(api):
    seat = api.first_seat()
    assert api.hold(seat, user="A").status_code == 201
    assert api.book(seat, user="A").status_code == 201
    api.advance(minutes=5)
    assert_error(api.hold(seat, user="B"), 409, "SEAT_SOLD")
    api.advance(hours=24)
    assert_error(api.hold(seat, user="B"), 409, "SEAT_SOLD")
    assert api.status_of(seat) == "SOLD"


def test_us04_ac7_missing_user_header_is_unauthenticated(api):
    seat = api.first_seat()
    assert api.hold(seat, user="A").status_code == 201
    assert_error(api.book(seat), 401, "UNAUTHENTICATED")
    assert_error(api.book(seat, user=""), 401, "UNAUTHENTICATED")
    assert api.status_of(seat) == "HELD"
    assert api.booking_count() == 0


def test_us04_contract_invalid_body_is_400_invalid_request(api):
    api.show_id()
    assert_error(api.book(None, user="A", json={}), 400, "INVALID_REQUEST")
    assert_error(api.request("POST", "/bookings", user="A", content=b"not json",
                             headers={"Content-Type": "application/json"}), 400, "INVALID_REQUEST")


def test_us04_contract_unknown_seat_is_404(api):
    api.show_id()
    assert_error(api.book("no-such-seat", user="A"), 404, "NOT_FOUND")


def test_us04_contract_nul_in_seat_id_is_400_and_state_unchanged(api):
    seat = api.first_seat()
    assert_error(api.book("\u0000", user="A"), 400, "INVALID_REQUEST")
    assert_error(api.book(seat + "\u0000", user="A"), 400, "INVALID_REQUEST")
    assert api.status_of(seat) == "AVAILABLE"
    assert api.booking_count() == 0
    assert api.hold(seat, user="A").status_code == 201
    assert api.book(seat, user="A").status_code == 201  # 서버가 멀쩡하다
