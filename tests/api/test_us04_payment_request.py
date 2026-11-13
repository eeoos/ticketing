"""US-04 결제 요청(멱등) (docs/stories/US-04-payment-request.md). 시각은 get_now 오버라이드."""
import pytest

from api_support import HOLD, NO_KEY, T0, assert_error, parse_dt, run_concurrently


def booking_shape(b):
    for f in ("id", "seatId", "userId", "status", "paymentId", "holdId", "holdExpiresAt", "createdAt", "updatedAt"):
        assert f in b, f"예매 본문에 {f} 가 없다: {b}"
    for f in ("id", "paymentId", "holdId"):
        assert isinstance(b[f], str) and b[f]
    for f in ("holdExpiresAt", "createdAt", "updatedAt"):
        parse_dt(b[f])


# ---- AC1 ----
def test_us04_ac1_valid_hold_payment_request_is_202_pending(api):
    seat = api.first_seat()
    hold = api.hold(seat, user="A").json()
    api.advance(minutes=4, seconds=59)
    r = api.pay(seat, user="A", key="k1")
    assert r.status_code == 202, r.text
    b = r.json()
    booking_shape(b)
    assert b["status"] == "PENDING"
    assert b["seatId"] == seat and b["userId"] == "A"
    assert b["holdId"] == hold["holdId"]
    assert parse_dt(b["holdExpiresAt"]) == T0 + HOLD
    assert api.status_of(seat) == "HELD"  # 아직 팔리지 않았다
    assert r.headers.get("Idempotent-Replayed") != "true"
    assert len(api.bookings_in_db(seat)) == 1


# ---- AC2 ----
@pytest.mark.parametrize("seconds", [300, 301], ids=["t_plus_5m00s", "t_plus_5m01s"])
def test_us04_ac2_first_request_after_expiry_is_hold_expired(api, seconds):
    seat = api.first_seat()
    assert api.hold(seat, user="A").status_code == 201
    api.advance(seconds=seconds)
    assert_error(api.pay(seat, user="A"), 409, "HOLD_EXPIRED")
    assert api.booking_count() == 0
    assert api.status_of(seat) == "AVAILABLE"


# ---- AC3 ----
def test_us04_ac3_other_users_valid_hold_is_403_and_untouched(api):
    seat = api.first_seat()
    hold = api.hold(seat, user="B").json()
    assert_error(api.pay(seat, user="A"), 403, "HOLD_NOT_OWNED")
    assert api.booking_count() == 0
    assert api.status_of(seat) == "HELD"
    b = api.pay(seat, user="B")  # B 의 홀드는 그대로
    assert b.status_code == 202 and b.json()["holdId"] == hold["holdId"]


# ---- AC4: 같은 키·같은 본문 = 최초 응답 재현 ----
def test_us04_ac4_same_key_same_body_replays_first_response(api):
    seat = api.first_seat()
    api.hold(seat, user="A")
    first = api.pay(seat, user="A", key="k1")
    assert first.status_code == 202
    again = api.pay(seat, user="A", key="k1")
    assert again.status_code == 202
    assert again.json() == first.json()
    assert again.headers.get("Idempotent-Replayed") == "true"
    assert len(api.bookings_in_db(seat)) == 1


def test_us04_ac4_replay_after_hold_expired_still_returns_first_response(api):
    seat = api.first_seat()
    api.hold(seat, user="A")
    first = api.pay(seat, user="A", key="k1")
    api.advance(minutes=10)  # 홀드 만료
    again = api.pay(seat, user="A", key="k1")
    assert again.status_code == 202, again.text
    assert again.json() == first.json()
    assert again.headers.get("Idempotent-Replayed") == "true"
    assert len(api.bookings_in_db(seat)) == 1


def test_us04_ac4_replay_after_confirmed_still_returns_first_pending_response(api):
    seat = api.first_seat()
    api.hold(seat, user="A")
    first = api.pay(seat, user="A", key="k1")
    cb = api.callback(first.json()["paymentId"], "SUCCEEDED")
    assert cb.json()["booking"]["status"] == "CONFIRMED"
    again = api.pay(seat, user="A", key="k1")  # 키 재요청이 '이미 확정(200)'보다 우선
    assert again.status_code == 202, again.text
    assert again.json() == first.json() and again.json()["status"] == "PENDING"
    assert again.headers.get("Idempotent-Replayed") == "true"
    assert len(api.bookings_in_db(seat)) == 1


# ---- AC5 ----
def test_us04_ac5_same_key_different_body_is_key_mismatch(api):
    seats = api.seat_ids()
    api.hold(seats[0], user="A")
    assert api.pay(seats[0], user="A", key="k1").status_code == 202
    assert_error(api.pay(seats[1], user="A", key="k1"), 409, "IDEMPOTENCY_KEY_MISMATCH")
    assert len(api.bookings_in_db()) == 1
    assert api.status_of(seats[1]) == "AVAILABLE"


# ---- AC6 ----
def test_us04_ac6_key_scope_is_per_user(api):
    seats = api.seat_ids()
    api.hold(seats[0], user="A")
    api.hold(seats[1], user="B")
    a = api.pay(seats[0], user="A", key="k1")
    b = api.pay(seats[1], user="B", key="k1")
    assert a.status_code == 202 and b.status_code == 202, (a.text, b.text)
    assert a.json()["id"] != b.json()["id"] and b.json()["userId"] == "B"
    assert b.headers.get("Idempotent-Replayed") != "true"
    assert api.pay(seats[0], user="A", key="k1").json() == a.json()  # A 의 키는 A 의 것


# ---- AC7 ----
def test_us04_ac7_pending_payment_blocks_new_key(api):
    seat = api.first_seat()
    api.hold(seat, user="A")
    assert api.pay(seat, user="A", key="k1").status_code == 202
    assert_error(api.pay(seat, user="A", key="k2"), 409, "PAYMENT_IN_PROGRESS")
    assert len(api.bookings_in_db(seat)) == 1


# ---- AC8 ----
def test_us04_ac8_retry_after_failure_creates_new_pending_for_same_hold(api):
    seat = api.first_seat()
    hold = api.hold(seat, user="A").json()
    first = api.pay(seat, user="A", key="k1").json()
    assert api.callback(first["paymentId"], "FAILED").json()["booking"]["status"] == "FAILED"
    r = api.pay(seat, user="A", key="k2")
    assert r.status_code == 202, r.text
    b = r.json()
    assert b["status"] == "PENDING" and b["id"] != first["id"] and b["paymentId"] != first["paymentId"]
    assert b["holdId"] == hold["holdId"] == first["holdId"]
    assert parse_dt(b["holdExpiresAt"]) == parse_dt(hold["expiresAt"])
    assert api.status_of(seat) == "HELD"
    assert len(api.bookings_in_db(seat)) == 2


# ---- AC9 ----
def test_us04_ac9_confirmed_own_booking_with_new_key_is_200_same_booking(api):
    seat = api.first_seat()
    confirmed = api.sell(seat, "A")
    r = api.pay(seat, user="A", key="brand-new-key")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == confirmed["id"] and r.json()["status"] == "CONFIRMED"
    assert r.json()["paymentId"] == confirmed["paymentId"]
    assert len(api.bookings_in_db(seat)) == 1


# ---- AC10 ----
def test_us04_ac10_seat_sold_to_someone_else_is_seat_sold(api):
    seat = api.first_seat()
    api.sell(seat, "B")
    assert_error(api.pay(seat, user="A"), 409, "SEAT_SOLD")
    assert len(api.bookings_in_db(seat)) == 1


# ---- AC11 ----
def test_us04_ac11_available_seat_without_hold_is_no_hold(api):
    seat = api.first_seat()
    assert_error(api.pay(seat, user="A"), 409, "NO_HOLD")
    assert api.booking_count() == 0
    assert api.status_of(seat) == "AVAILABLE"


# ---- AC12 ----
def test_us04_ac12_missing_user_header_is_401_before_other_checks(api):
    seat = api.first_seat()
    api.hold(seat, user="A")
    assert_error(api.pay(seat, key="k1"), 401, "UNAUTHENTICATED")
    assert_error(api.pay(seat, user="", key="k1"), 401, "UNAUTHENTICATED")
    assert_error(api.pay(seat, key=NO_KEY), 401, "UNAUTHENTICATED")  # 키도 없을 때는 401 이 먼저
    assert api.booking_count() == 0
    assert api.status_of(seat) == "HELD"


@pytest.mark.parametrize("key", [NO_KEY, "", "a\x00b", "k" * 256], ids=["missing", "empty", "nul", "256_chars"])
def test_us04_ac12_invalid_idempotency_key_is_400(api, key):
    seat = api.first_seat()
    api.hold(seat, user="A")
    assert_error(api.pay(seat, user="A", key=key), 400, "INVALID_REQUEST")
    assert api.booking_count() == 0


def test_us04_ac12_key_of_255_chars_is_accepted(api):
    seat = api.first_seat()
    api.hold(seat, user="A")
    assert api.pay(seat, user="A", key="k" * 255).status_code == 202


@pytest.mark.parametrize("body", [{}, {"seatId": 123}, {"seatId": ""}, {"seatId": "\u0000"}, [], "x"],
                         ids=["empty_object", "non_string", "empty_string", "nul", "array", "string"])
def test_us04_ac12_invalid_body_is_400(api, body):
    seat = api.first_seat()
    api.hold(seat, user="A")
    assert_error(api.pay(None, user="A", json=body), 400, "INVALID_REQUEST")
    assert api.booking_count() == 0


def test_us04_ac12_non_json_body_is_400(api):
    api.hold(api.first_seat(), user="A")
    r = api.request("POST", "/bookings", user="A", content=b"not json",
                    headers={"Content-Type": "application/json", "Idempotency-Key": "k1"})
    assert_error(r, 400, "INVALID_REQUEST")


def test_us04_ac12_nul_in_seat_id_suffix_is_400_and_state_unchanged(api):
    seat = api.first_seat()
    assert_error(api.pay(seat + "\u0000", user="A"), 400, "INVALID_REQUEST")
    assert api.status_of(seat) == "AVAILABLE" and api.booking_count() == 0


def test_us04_ac12_unknown_seat_is_404(api):
    api.show_id()
    assert_error(api.pay("no-such-seat", user="A"), 404, "NOT_FOUND")


def test_us04_ac12_errors_do_not_consume_the_key(api):
    seat = api.first_seat()
    assert_error(api.pay("no-such-seat", user="A", key="k1"), 404, "NOT_FOUND")
    assert_error(api.pay(seat, user="A", key="k1"), 409, "NO_HOLD")  # 같은 키, 다른 요청 → 불일치가 아니다
    api.hold(seat, user="A")
    r = api.pay(seat, user="A", key="k1")
    assert r.status_code == 202, r.text
    assert r.headers.get("Idempotent-Replayed") != "true"


def test_us04_ac9_confirmed_200_does_not_consume_the_key(api):
    seats = api.seat_ids()
    api.sell(seats[0], "A")
    assert api.pay(seats[0], user="A", key="k1").status_code == 200
    api.hold(seats[1], user="A")
    r = api.pay(seats[1], user="A", key="k1")  # 200 은 키를 저장하지 않았다
    assert r.status_code == 202, r.text


# ---- AC13: 같은 키 동시 요청 (프로세스 안; 실제 서버 버전은 tests/concurrency C-03) ----
def test_us04_ac13_concurrent_same_key_creates_one_booking(api):
    seat = api.first_seat()
    api.hold(seat, user="A")
    clients = [api.new_client() for _ in range(12)]
    results = run_concurrently(12, lambda i: api.pay(seat, user="A", key="same-key", client=clients[i]))
    assert [r.status_code for r in results] == [202] * 12, [(r.status_code, r.text) for r in results]
    assert len({r.json()["id"] for r in results}) == 1
    assert len(api.bookings_in_db(seat)) == 1


# ---- AC14: 예매 조회 ----
def test_us04_ac14_owner_reads_booking_other_user_gets_404(api):
    seat = api.first_seat()
    api.hold(seat, user="A")
    b = api.pay(seat, user="A").json()
    r = api.get_booking(b["id"], user="A")
    assert r.status_code == 200, r.text
    assert r.json() == b
    assert_error(api.get_booking(b["id"], user="B"), 404, "NOT_FOUND")
    assert_error(api.get_booking("no-such-booking", user="A"), 404, "NOT_FOUND")
    assert_error(api.get_booking(b["id"]), 401, "UNAUTHENTICATED")


def test_us04_ac14_booking_reflects_callback_result(api):
    seat = api.first_seat()
    confirmed = api.sell(seat, "A")
    r = api.get_booking(confirmed["id"], user="A")
    assert r.status_code == 200 and r.json()["status"] == "CONFIRMED"
    assert r.json()["id"] == confirmed["id"]


# ---- 계약 보충: 짝 없는 surrogate (JSON 이스케이프 \ud800 → 파이썬 str 로 복원되지만 UTF-8 로 저장 불가) ----
SURROGATE_BODIES = [b'{"seatId":"\\ud800"}', b'{"seatId":"\\udfff"}', b'{"seatId":"a\\ud800b"}',
                    b'{"seatId":"\\ud800\\ud800"}', b'{"seatId":"\\udc00\\ud800"}']


@pytest.mark.parametrize("raw", SURROGATE_BODIES,
                         ids=["lone_high", "lone_low", "embedded_high", "two_highs", "low_then_high"])
def test_us04_contract_lone_surrogate_in_seat_id_is_400_and_key_not_consumed(api, raw):
    seat = api.first_seat()
    api.hold(seat, user="A")
    headers = {"Content-Type": "application/json", "Idempotency-Key": "k1"}
    r = api.request("POST", "/bookings", user="A", content=raw, headers=headers)
    assert_error(r, 400, "INVALID_REQUEST")
    assert api.booking_count() == 0
    assert api.status_of(seat) == "HELD"
    ok = api.pay(seat, user="A", key="k1")  # 오류는 키를 소비하지 않는다
    assert ok.status_code == 202, ok.text
