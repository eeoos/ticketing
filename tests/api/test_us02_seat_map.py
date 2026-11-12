"""US-02 좌석맵 보기 (docs/stories/US-02-seat-map.md)"""
from api_support import HOLD, assert_error  # noqa: F401


def test_us02_ac1_all_seats_with_id_position_status(api):
    r = api.request("GET", f"/shows/{api.show_id()}/seats")  # X-User-Id 없이
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
    assert api.hold(seat, user="A").status_code == 201
    assert api.book(seat, user="A").status_code == 201
    seats = {s["id"]: s["status"] for s in api.seat_map()}
    assert seats[seat] == "SOLD"
    assert sum(v == "AVAILABLE" for v in seats.values()) == 99


def test_us02_ac5_unknown_show_is_not_found(api):
    api.show_id()  # 환경 준비
    assert_error(api.request("GET", "/shows/no-such-show/seats"), 404, "NOT_FOUND")


def test_us02_contract_nul_in_show_id_is_404(api):
    api.show_id()
    assert_error(api.request("GET", "/shows/%00/seats"), 404, "NOT_FOUND")
    assert_error(api.request("GET", f"/shows/{api.show_id()}%00/seats"), 404, "NOT_FOUND")
