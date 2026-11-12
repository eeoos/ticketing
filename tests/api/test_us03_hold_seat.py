"""US-03 좌석 하나를 5분 동안 잡기 (docs/stories/US-03-hold-seat.md)"""
from api_support import HOLD, assert_error, parse_dt, run_concurrently


def test_us03_ac1_available_seat_can_be_held_for_five_minutes(api):
    seat = api.first_seat()
    r = api.hold(seat, user="A")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["seatId"] == seat and body["userId"] == "A"
    assert parse_dt(body["expiresAt"]) == api.now + HOLD
    assert api.status_of(seat) == "HELD"


def test_us03_ac2_other_user_cannot_hold_held_seat(api):
    seat = api.first_seat()
    assert api.hold(seat, user="A").status_code == 201
    api.advance(minutes=1)
    assert_error(api.hold(seat, user="B"), 409, "SEAT_HELD")
    assert api.status_of(seat) == "HELD"
    # 계속 A 의 홀드: B 는 확정할 수 없고 A 는 확정할 수 있다
    assert_error(api.book(seat, user="B"), 403, "HOLD_NOT_OWNED")
    assert api.book(seat, user="A").status_code == 201


def test_us03_ac3_same_user_rehold_rejected_and_expiry_unchanged(api):
    seat = api.first_seat()
    assert api.hold(seat, user="A").status_code == 201  # 만료 = T0+5분
    api.advance(minutes=1)
    assert_error(api.hold(seat, user="A"), 409, "SEAT_HELD")
    assert api.status_of(seat) == "HELD"
    api.now = api.now.replace(minute=5)  # T0+5분: 원래 만료 시각
    assert api.status_of(seat) == "AVAILABLE"  # 연장됐다면 아직 HELD 였을 것


def test_us03_ac4_expired_hold_can_be_taken_by_other_user(api):
    seat = api.first_seat()
    assert api.hold(seat, user="A").status_code == 201
    api.now += HOLD
    r = api.hold(seat, user="B")
    assert r.status_code == 201, r.text
    assert r.json()["userId"] == "B"
    assert parse_dt(r.json()["expiresAt"]) == api.now + HOLD  # 새 만료 시각
    assert api.status_of(seat) == "HELD"
    assert_error(api.book(seat, user="A"), 403, "HOLD_NOT_OWNED")  # 이제 B 의 홀드


def test_us03_ac4_boundary_one_second_before_expiry_still_held(api):
    seat = api.first_seat()
    assert api.hold(seat, user="A").status_code == 201
    api.advance(minutes=4, seconds=59)
    assert_error(api.hold(seat, user="B"), 409, "SEAT_HELD")
    api.advance(seconds=1)  # now == expiresAt
    assert api.hold(seat, user="B").status_code == 201


def test_us03_ac5_sold_seat_cannot_be_held(api):
    seat = api.first_seat()
    assert api.hold(seat, user="A").status_code == 201
    assert api.book(seat, user="A").status_code == 201
    assert_error(api.hold(seat, user="B"), 409, "SEAT_SOLD")
    assert_error(api.hold(seat, user="A"), 409, "SEAT_SOLD")
    assert api.status_of(seat) == "SOLD"


def test_us03_ac6_concurrent_holds_exactly_one_wins(api):
    seats = api.seat_ids()[:3]  # 서로 다른 좌석으로 3번 반복
    for seat in seats:
        clients = [api.new_client() for _ in range(12)]
        results = run_concurrently(12, lambda i: api.hold(seat, user=f"U{i}", client=clients[i]))
        codes = sorted(r.status_code for r in results)
        assert codes == [201] + [409] * 11, codes
        for r in results:
            if r.status_code == 409:
                assert r.json()["code"] == "SEAT_HELD"
        winner = next(r.json()["userId"] for r in results if r.status_code == 201)
        assert api.status_of(seat) == "HELD"
        assert api.book(seat, user=winner).status_code == 201  # 승자만 확정 가능


def test_us03_ac7_missing_user_header_is_unauthenticated(api):
    seat = api.first_seat()
    assert_error(api.hold(seat), 401, "UNAUTHENTICATED")
    assert_error(api.hold(seat, user=""), 401, "UNAUTHENTICATED")
    assert api.status_of(seat) == "AVAILABLE"


def test_us03_ac8_unknown_seat_is_not_found(api):
    api.show_id()
    assert_error(api.hold("no-such-seat", user="A"), 404, "NOT_FOUND")


def test_us03_contract_nul_in_seat_id_is_404_and_state_unchanged(api):
    seat = api.first_seat()
    r = api.request("POST", "/seats/%00/hold", user="A")
    assert_error(r, 404, "NOT_FOUND")
    assert_error(api.request("POST", f"/seats/{seat}%00/hold", user="A"), 404, "NOT_FOUND")
    assert api.status_of(seat) == "AVAILABLE"
    assert api.hold(seat, user="A").status_code == 201  # 서버가 멀쩡하다
