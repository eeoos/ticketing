"""10장 outbox: 예매 확정 트랜잭션 안에서 이벤트 1줄 (docs/events.md '언제 이벤트를 쓰나'·'outbox 테이블'). Kafka·릴레이 없이 API+DB 만 본다."""
import json
import threading
import uuid
from datetime import datetime

import pytest

from events_support import T0


def _payload(row):
    p = row[5]
    return json.loads(p) if isinstance(p, str) else p


def test_confirm_writes_exactly_one_outbox_row_with_contract_format(ev):
    seat = ev.seat_id()
    booking, user = ev.confirm(seat)
    rows = ev.outbox_rows()
    assert len(rows) == 1, f"확정 1건에 outbox 1줄이어야 한다: {rows}"
    event_id, type_, booking_id, show_id, user_id, _p, created_at, sent_at, attempts, last_error = rows[0]
    assert type_ == "BookingConfirmed"
    assert str(booking_id) == booking["id"] and str(show_id) == ev.show_id() and user_id == user
    assert str(uuid.UUID(str(event_id))) == str(event_id), "event_id 는 uuid 문자열"
    assert sent_at is None and attempts == 0 and last_error is None, "릴레이가 없으면 미전송 상태"
    p = _payload(rows[0])
    assert p["eventId"] == str(event_id)
    assert p["type"] == "BookingConfirmed" and p["version"] == 1
    assert p["bookingId"] == booking["id"] and p["showId"] == ev.show_id() and p["seatId"] == seat
    assert p["userId"] == user and p["paymentId"] == booking["paymentId"]
    assert set(p) == {"eventId", "type", "version", "occurredAt", "bookingId", "showId", "seatId", "userId", "paymentId"}
    occurred = datetime.fromisoformat(p["occurredAt"])
    assert occurred.tzinfo is not None and occurred == T0, "occurredAt 은 콜백 처리 시 get_now"
    assert created_at == T0, "outbox.created_at 은 get_now"


def test_duplicate_success_callback_keeps_single_outbox_row(ev):
    b = ev.hold_and_pay(ev.seat_id(), ev.user())
    first = ev.callback(b["paymentId"], "SUCCEEDED")
    assert first.json()["applied"] is True
    before = ev.outbox_rows()
    assert len(before) == 1
    for _ in range(3):
        again = ev.callback(b["paymentId"], "SUCCEEDED")
        assert again.status_code == 200 and again.json()["applied"] is False
    assert ev.outbox_rows() == before, "applied=false 인 콜백은 outbox 를 바꾸지 않는다(eventId 도 그대로)"


def test_concurrent_success_callbacks_write_one_outbox_row(ev):
    b = ev.hold_and_pay(ev.seat_id(), ev.user())
    n = 8
    clients = [ev.new_client() for _ in range(n)]
    barrier, results, errors = threading.Barrier(n), [None] * n, []

    def work(i):
        try:
            barrier.wait(timeout=10)
            results[i] = ev.callback(b["paymentId"], "SUCCEEDED", client=clients[i])
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join(timeout=30) for t in ts]
    assert not errors, errors
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert sum(r.json()["applied"] is True for r in results) == 1
    assert ev.outbox_count() == 1, "동시에 같은 성공 콜백이 와도 outbox 는 1줄"


def test_rebooking_a_confirmed_seat_with_new_key_does_not_add_outbox(ev):
    seat = ev.seat_id()
    booking, user = ev.confirm(seat)
    before = ev.outbox_rows()
    r = ev.pay(seat, user)  # 이미 확정된 예매를 새 키로 다시 요청 → 200 (docs/events.md)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == booking["id"]
    assert ev.outbox_rows() == before


def test_failed_callback_writes_no_outbox(ev):
    b = ev.hold_and_pay(ev.seat_id(), ev.user())
    r = ev.callback(b["paymentId"], "FAILED")
    assert r.status_code == 200 and r.json()["booking"]["status"] == "FAILED"
    assert ev.outbox_count() == 0


def test_expired_success_refund_required_writes_no_outbox(ev):
    b = ev.hold_and_pay(ev.seat_id(), ev.user())
    ev.advance(minutes=5)
    r = ev.callback(b["paymentId"], "SUCCEEDED")
    assert r.status_code == 200 and r.json()["booking"]["status"] == "REFUND_REQUIRED", r.text
    assert ev.outbox_count() == 0


def test_success_after_failed_refund_required_writes_no_outbox(ev):
    b = ev.hold_and_pay(ev.seat_id(), ev.user())
    assert ev.callback(b["paymentId"], "FAILED").json()["booking"]["status"] == "FAILED"
    r = ev.callback(b["paymentId"], "SUCCEEDED")
    assert r.status_code == 200, r.text
    assert r.json()["booking"]["status"] == "REFUND_REQUIRED", r.text
    assert ev.outbox_count() == 0


def test_each_confirmation_gets_its_own_event_id(ev):
    seats = ev.seat_ids(3)
    for s in seats:
        ev.confirm(s)
    rows = ev.outbox_rows()
    assert len(rows) == 3
    assert len({str(r[0]) for r in rows}) == 3 and len({str(r[2]) for r in rows}) == 3


def test_outbox_booking_and_type_is_unique_in_db(ev):
    """DB 보장: (booking_id, type) UNIQUE, event_id 기본키. 같은 예매에 두 번째 줄을 직접 넣으면 거절된다."""
    import psycopg  # noqa: PLC0415
    from events_support import EVENTS_DSN  # noqa: PLC0415
    booking, _ = ev.confirm()
    row = ev.outbox_rows()[0]
    with pytest.raises(psycopg.errors.UniqueViolation):
        with psycopg.connect(EVENTS_DSN) as conn:
            conn.execute(
                "INSERT INTO outbox (event_id, type, booking_id, show_id, user_id, payload, created_at) "
                "VALUES (%s, 'BookingConfirmed', %s, %s, %s, '{}'::jsonb, now())",
                (str(uuid.uuid4()), row[2], row[3], row[4]))


def test_every_confirmed_booking_has_exactly_one_outbox_row(ev):
    """예매 상태 변경과 outbox 는 같은 트랜잭션이라 함께 반영된다(불변식: CONFIRMED 예매마다 outbox 정확히 1줄).
    롤백 쪽(콜백 트랜잭션 실패 → outbox 도 없음)은 장애 주입 방법이 인터페이스에 없어 여기서는 다루지 않는다."""
    ev.confirm()
    ev.confirm(ev.seat_id(1))
    rows = ev.q("SELECT count(*) FROM bookings b WHERE b.status='CONFIRMED'")
    out = ev.q("SELECT count(*) FROM outbox o JOIN bookings b ON b.id=o.booking_id WHERE b.status='CONFIRMED'")
    assert rows[0][0] == out[0][0] == 2, "CONFIRMED 예매마다 outbox 가 정확히 1줄"
