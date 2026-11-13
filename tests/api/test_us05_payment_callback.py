"""US-05 모의 결제사 콜백 (docs/stories/US-05-payment-callback.md). 시각은 get_now 오버라이드."""
import pytest

from api_support import HOLD, T0, assert_error, parse_dt, run_concurrently


def booking_status(api, booking, user):
    r = api.get_booking(booking["id"], user=user)
    assert r.status_code == 200, r.text
    return r.json()["status"]


# ---- AC1 ----
def test_us05_ac1_success_before_expiry_confirms_booking_and_sells_seat(api):
    seat = api.first_seat()
    _, b = api.hold_and_pay(seat, "A")
    api.advance(minutes=4, seconds=59)
    r = api.callback(b["paymentId"], "SUCCEEDED")
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True
    assert r.json()["booking"]["status"] == "CONFIRMED"
    assert r.json()["booking"]["id"] == b["id"]
    assert api.status_of(seat) == "SOLD"
    assert booking_status(api, b, "A") == "CONFIRMED"


# ---- AC2 ----
@pytest.mark.parametrize("seconds", [300, 301], ids=["t_plus_5m00s", "t_plus_5m01s"])
def test_us05_ac2_success_at_or_after_expiry_is_refund_required(api, seconds):
    seat = api.first_seat()
    _, b = api.hold_and_pay(seat, "A")
    api.advance(seconds=seconds)
    r = api.callback(b["paymentId"], "SUCCEEDED")
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True
    assert r.json()["booking"]["status"] == "REFUND_REQUIRED"
    assert api.status_of(seat) == "AVAILABLE"  # 팔리지 않았다
    assert booking_status(api, b, "A") == "REFUND_REQUIRED"


# ---- AC3 ----
def test_us05_ac3_failed_callback_marks_failed_and_keeps_hold(api):
    seat = api.first_seat()
    hold, b = api.hold_and_pay(seat, "A")
    api.advance(minutes=1)
    r = api.callback(b["paymentId"], "FAILED")
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True and r.json()["booking"]["status"] == "FAILED"
    assert api.status_of(seat) == "HELD"
    assert_error(api.hold(seat, user="B"), 409, "SEAT_HELD")  # 여전히 A 의 홀드
    retry = api.pay(seat, user="A")  # 같은 홀드·같은 만료 시각
    assert retry.status_code == 202 and retry.json()["holdId"] == hold["holdId"]
    assert parse_dt(retry.json()["holdExpiresAt"]) == T0 + HOLD


# ---- AC4: 중복 콜백 ----
def test_us05_ac4_duplicate_success_is_applied_false(api):
    seat = api.first_seat()
    _, b = api.hold_and_pay(seat, "A")
    first = api.callback(b["paymentId"], "SUCCEEDED")
    assert first.json()["applied"] is True
    api.advance(minutes=1)
    again = api.callback(b["paymentId"], "SUCCEEDED")
    assert again.status_code == 200, again.text
    assert again.json()["applied"] is False
    assert again.json()["booking"] == first.json()["booking"]  # updatedAt 포함 변화 없음
    assert api.status_of(seat) == "SOLD"


def test_us05_ac4_duplicate_failed_is_applied_false(api):
    _, b = api.hold_and_pay(api.first_seat(), "A")
    first = api.callback(b["paymentId"], "FAILED")
    again = api.callback(b["paymentId"], "FAILED")
    assert again.status_code == 200 and again.json()["applied"] is False
    assert again.json()["booking"] == first.json()["booking"]


def test_us05_ac4_duplicate_late_success_on_refund_required_is_applied_false(api):
    _, b = api.hold_and_pay(api.first_seat(), "A")
    api.now += HOLD
    first = api.callback(b["paymentId"], "SUCCEEDED")
    assert first.json()["booking"]["status"] == "REFUND_REQUIRED"
    again = api.callback(b["paymentId"], "SUCCEEDED")
    assert again.status_code == 200 and again.json()["applied"] is False
    assert again.json()["booking"]["status"] == "REFUND_REQUIRED"


def test_us05_ac4_concurrent_duplicate_callbacks_apply_exactly_once(api):
    seat = api.first_seat()
    _, b = api.hold_and_pay(seat, "A")
    clients = [api.new_client() for _ in range(12)]
    results = run_concurrently(12, lambda i: api.callback(b["paymentId"], "SUCCEEDED", client=clients[i]))
    assert [r.status_code for r in results] == [200] * 12, [(r.status_code, r.text) for r in results]
    assert sum(r.json()["applied"] for r in results) == 1
    assert {r.json()["booking"]["status"] for r in results} == {"CONFIRMED"}
    assert api.status_of(seat) == "SOLD"
    assert len([x for x in api.bookings_in_db(seat) if x[3] == "CONFIRMED"]) == 1


# ---- AC5 ----
def test_us05_ac5_late_failed_after_confirmed_is_conflict(api):
    seat = api.first_seat()
    confirmed = api.sell(seat, "A")
    assert_error(api.callback(confirmed["paymentId"], "FAILED"), 409, "CALLBACK_CONFLICT")
    assert booking_status(api, confirmed, "A") == "CONFIRMED"
    assert api.status_of(seat) == "SOLD"


# ---- AC6 ----
def test_us05_ac6_late_success_after_failed_is_refund_required_even_if_hold_valid(api):
    seat = api.first_seat()
    _, b = api.hold_and_pay(seat, "A")
    assert api.callback(b["paymentId"], "FAILED").json()["applied"] is True
    api.advance(minutes=1)  # 홀드는 아직 유효
    r = api.callback(b["paymentId"], "SUCCEEDED")
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True and r.json()["booking"]["status"] == "REFUND_REQUIRED"
    assert api.status_of(seat) == "HELD"  # 팔리지 않았다


# ---- AC7 ----
def test_us05_ac7_failed_after_refund_required_is_conflict(api):
    seat = api.first_seat()
    _, b = api.hold_and_pay(seat, "A")
    api.now += HOLD
    assert api.callback(b["paymentId"], "SUCCEEDED").json()["booking"]["status"] == "REFUND_REQUIRED"
    assert_error(api.callback(b["paymentId"], "FAILED"), 409, "CALLBACK_CONFLICT")
    assert booking_status(api, b, "A") == "REFUND_REQUIRED"


# ---- AC8: 늦은 콜백은 새 홀드에 영향이 없다 ----
def _expired_then_new_hold(api, new_user):
    seat = api.first_seat()
    _, b = api.hold_and_pay(seat, "A")
    api.now += HOLD  # A 의 홀드 만료
    new = api.hold(seat, user=new_user)
    assert new.status_code == 201, new.text
    return seat, b, new.json()


def _new_hold_untouched(api, seat, new_hold, new_user):
    assert api.status_of(seat) == "HELD"
    other = "A" if new_user == "B" else "B"
    assert_error(api.hold(seat, user=other), 409, "SEAT_HELD")
    p = api.pay(seat, user=new_user)  # 새 홀드의 주인·holdId·만료 시각 그대로
    assert p.status_code == 202, p.text
    assert p.json()["holdId"] == new_hold["holdId"]
    assert parse_dt(p.json()["holdExpiresAt"]) == parse_dt(new_hold["expiresAt"])
    if new_user != "A":
        assert_error(api.pay(seat, user="A"), 403, "HOLD_NOT_OWNED")


def test_us05_ac8_late_success_after_other_user_rehold_is_refund_required(api):
    seat, old, new_hold = _expired_then_new_hold(api, "B")
    r = api.callback(old["paymentId"], "SUCCEEDED")
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True and r.json()["booking"]["status"] == "REFUND_REQUIRED"
    _new_hold_untouched(api, seat, new_hold, "B")


def test_us05_ac8_late_failed_after_other_user_rehold_does_not_touch_new_hold(api):
    seat, old, new_hold = _expired_then_new_hold(api, "B")
    r = api.callback(old["paymentId"], "FAILED")
    assert r.status_code == 200, r.text
    assert r.json()["booking"]["status"] == "FAILED"
    _new_hold_untouched(api, seat, new_hold, "B")


def test_us05_ac8_late_success_after_same_user_rehold_is_refund_required(api):
    seat, old, new_hold = _expired_then_new_hold(api, "A")
    assert new_hold["holdId"] != old["holdId"]
    r = api.callback(old["paymentId"], "SUCCEEDED")
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True and r.json()["booking"]["status"] == "REFUND_REQUIRED"
    _new_hold_untouched(api, seat, new_hold, "A")


def test_us05_ac8_late_failed_after_same_user_rehold_does_not_touch_new_hold(api):
    seat, old, new_hold = _expired_then_new_hold(api, "A")
    r = api.callback(old["paymentId"], "FAILED")
    assert r.status_code == 200 and r.json()["booking"]["status"] == "FAILED"
    _new_hold_untouched(api, seat, new_hold, "A")


# ---- AC9: 서명·본문·알 수 없는 결제 ----
def test_us05_ac9_missing_or_wrong_signature_is_401_and_changes_nothing(api):
    seat = api.first_seat()
    _, b = api.hold_and_pay(seat, "A")
    assert_error(api.callback(b["paymentId"], "SUCCEEDED", signature=None), 401, "INVALID_SIGNATURE")
    assert_error(api.callback(b["paymentId"], "SUCCEEDED", signature="0" * 64), 401, "INVALID_SIGNATURE")
    assert_error(api.callback(b["paymentId"], "SUCCEEDED", signature=api.sign(b"{}")), 401, "INVALID_SIGNATURE")
    wrong_secret = api.sign(b'{"paymentId": "%s", "result": "SUCCEEDED"}' % b["paymentId"].encode(), secret="not-the-secret")
    assert_error(api.callback(b["paymentId"], "SUCCEEDED", signature=wrong_secret), 401, "INVALID_SIGNATURE")
    assert booking_status(api, b, "A") == "PENDING"
    assert api.status_of(seat) == "HELD"


def test_us05_ac9_signature_is_checked_before_parsing_body(api):
    api.show_id()
    r = api.callback(None, None, body=b"not json", signature="0" * 64)
    assert_error(r, 401, "INVALID_SIGNATURE")  # 400 이 아니다


def test_us05_ac9_signature_covers_the_exact_body_bytes(api):
    _, b = api.hold_and_pay(api.first_seat(), "A")
    raw = b'{"paymentId": "%s",  "result": "SUCCEEDED"}' % b["paymentId"].encode()  # 공백이 다른 원문
    r = api.callback(None, None, body=raw, signature=api.sign(raw))
    assert r.status_code == 200 and r.json()["applied"] is True
    tampered = raw.replace(b"SUCCEEDED", b"FAILED  ")
    assert_error(api.callback(None, None, body=tampered, signature=api.sign(raw)), 401, "INVALID_SIGNATURE")


def test_us05_ac9_unknown_payment_id_is_404(api):
    api.show_id()
    assert_error(api.callback("no-such-payment", "SUCCEEDED"), 404, "NOT_FOUND")


@pytest.mark.parametrize("body", [b"not json", b"{}", b"[]", b'{"result": "SUCCEEDED"}', b'{"paymentId": "x"}',
                                  b'{"paymentId": "x", "result": "OK"}', b'{"paymentId": 1, "result": "SUCCEEDED"}',
                                  b'{"paymentId": "x", "result": "succeeded"}'],
                         ids=["not_json", "empty_object", "array", "no_payment_id", "no_result", "bad_result",
                              "non_string_payment_id", "lowercase_result"])
def test_us05_ac9_invalid_body_with_valid_signature_is_400(api, body):
    api.show_id()
    r = api.callback(None, None, body=body)
    assert_error(r, 400, "INVALID_REQUEST")


def test_us05_ac9_invalid_body_changes_nothing(api):
    seat = api.first_seat()
    _, b = api.hold_and_pay(seat, "A")
    assert_error(api.callback(b["paymentId"], None, body={"paymentId": b["paymentId"], "result": "OK"}), 400, "INVALID_REQUEST")
    assert booking_status(api, b, "A") == "PENDING"


def test_us05_ac9_callback_body_time_fields_are_ignored(api):
    seat = api.first_seat()
    _, b = api.hold_and_pay(seat, "A")
    api.now += HOLD  # 서버 시각 기준으로는 만료
    body = {"paymentId": b["paymentId"], "result": "SUCCEEDED", "timestamp": "2030-01-01T12:00:01Z",
            "now": "2030-01-01T12:00:01Z", "occurredAt": "2030-01-01T12:00:01Z"}
    r = api.callback(None, None, body=body)
    assert r.status_code == 200, r.text
    assert r.json()["booking"]["status"] == "REFUND_REQUIRED"  # 본문 시각(만료 전)을 믿지 않는다
    assert api.status_of(seat) == "AVAILABLE"


def test_us05_ac9_callback_does_not_need_user_header(api):
    _, b = api.hold_and_pay(api.first_seat(), "A")
    r = api.callback(b["paymentId"], "SUCCEEDED")  # X-User-Id 없이
    assert r.status_code == 200, r.text


# ---- AC10: 실패 후 재시도 ----
def test_us05_ac10_retry_after_failure_confirms_only_the_retry(api):
    seat = api.first_seat()
    _, first = api.hold_and_pay(seat, "A", key="k1")
    assert api.callback(first["paymentId"], "FAILED").json()["booking"]["status"] == "FAILED"
    retry = api.pay(seat, user="A", key="k2").json()
    assert retry["id"] != first["id"]
    r = api.callback(retry["paymentId"], "SUCCEEDED")
    assert r.status_code == 200 and r.json()["booking"]["status"] == "CONFIRMED"
    assert r.json()["booking"]["id"] == retry["id"]
    assert api.status_of(seat) == "SOLD"
    assert booking_status(api, first, "A") == "FAILED"
    confirmed = [x for x in api.bookings_in_db(seat) if x[3] == "CONFIRMED"]
    assert len(confirmed) == 1 and confirmed[0][0] == retry["id"]


def test_us05_ac10_late_success_of_failed_attempt_does_not_break_the_sold_seat(api):
    seat = api.first_seat()
    _, first = api.hold_and_pay(seat, "A", key="k1")
    api.callback(first["paymentId"], "FAILED")
    retry = api.pay(seat, user="A", key="k2").json()
    assert api.callback(retry["paymentId"], "SUCCEEDED").json()["booking"]["status"] == "CONFIRMED"
    late = api.callback(first["paymentId"], "SUCCEEDED")  # 이전 시도의 늦은 성공
    assert late.status_code == 200, late.text
    assert late.json()["booking"]["status"] == "REFUND_REQUIRED"
    assert api.status_of(seat) == "SOLD"
    assert booking_status(api, retry, "A") == "CONFIRMED"
    assert len([x for x in api.bookings_in_db(seat) if x[3] == "CONFIRMED"]) == 1


def test_us05_ac10_concurrent_success_and_failed_callbacks_end_in_an_allowed_state(api):
    seat = api.first_seat()
    _, b = api.hold_and_pay(seat, "A")
    clients = [api.new_client() for _ in range(12)]
    results = run_concurrently(
        12, lambda i: api.callback(b["paymentId"], "SUCCEEDED" if i % 2 == 0 else "FAILED", client=clients[i]))
    assert all(r.status_code in (200, 409) for r in results), [(r.status_code, r.text) for r in results]
    final = booking_status(api, b, "A")
    # 표의 두 순서: SUCCEEDED→FAILED(409) = CONFIRMED / FAILED→SUCCEEDED = REFUND_REQUIRED
    assert final in ("CONFIRMED", "REFUND_REQUIRED"), final
    assert api.status_of(seat) == ("SOLD" if final == "CONFIRMED" else "HELD")


# ---- 계약 보충: 짝 없는 surrogate ----
@pytest.mark.parametrize("raw", [b'{"paymentId":"\\ud800","result":"SUCCEEDED"}',
                                 b'{"paymentId":"\\udfff","result":"FAILED"}',
                                 b'{"paymentId":"a\\ud800b","result":"SUCCEEDED"}',
                                 b'{"paymentId":"x","result":"\\ud800"}'],
                         ids=["lone_high", "lone_low", "embedded_high", "result_lone_high"])
def test_us05_contract_lone_surrogate_in_body_with_valid_signature_is_400_and_changes_nothing(api, raw):
    seat = api.first_seat()
    _, b = api.hold_and_pay(seat, "A")
    r = api.callback(None, None, body=raw)  # 올바른 서명
    assert_error(r, 400, "INVALID_REQUEST")
    assert booking_status(api, b, "A") == "PENDING"
    assert api.status_of(seat) == "HELD"
