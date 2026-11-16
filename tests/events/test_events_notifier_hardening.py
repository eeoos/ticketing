"""10장 리뷰 반영(docs/events.md notifier '받아들이는 메시지'·'오류 나누기'·커밋 순서). US-05 AC11~15 의 notifier 쪽 보강.

- 독이 든 메시지: 같은 키(showId)로 [독 → 정상 표지] 를 넣어 표지 알림이 생기고 그룹 오프셋이 둘 다 넘어가는지.
- 커밋 순서(a)(b), 롤백(c): 현재 구현에서 통과해야 하는 회귀 방지 테스트.
"""
import copy
import json
import uuid

import psycopg
import pytest

from events_support import EVENTAL, EVENTS_DSN, wait_until


def _fresh(payload, **changes):
    p = copy.deepcopy(payload)
    p["eventId"] = str(uuid.uuid4())
    p.update(changes)
    return p


POISON = {
    "userId_has_nul": lambda p: _fresh(p, userId="u\x00x"),
    "four_fields_only": lambda p: {k: p[k] for k in ("eventId", "bookingId", "userId", "type")} | {"eventId": str(uuid.uuid4())},
    "wrong_type": lambda p: _fresh(p, type="BookingCancelled"),
    "version_2": lambda p: _fresh(p, version=2),
    "version_string_1": lambda p: _fresh(p, version="1"),
    "empty_userId": lambda p: _fresh(p, userId=""),
    "empty_bookingId": lambda p: _fresh(p, bookingId=""),
    "userId_256_chars": lambda p: _fresh(p, userId="a" * 256),
    "bad_occurredAt": lambda p: _fresh(p, occurredAt="not-a-date"),
    "non_string_userId": lambda p: _fresh(p, userId=123),
}


def _expect_skipped_and_sentinel_delivered(ev, poison_payload, key=None):
    sentinel = ev.confirmed_event()
    ev.start_notifier()
    ev.send_event(poison_payload, key=key)
    ev.send_event(sentinel)  # 같은 키(showId) → 같은 파티션, 독 뒤에 처리됨
    ev.wait(lambda: any(r[0] == sentinel["eventId"] for r in ev.notifications()), EVENTAL,
            "독이 든 메시지 뒤의 정상 표지 알림 도착(독이 같은 파티션 뒤를 막지 않음)")
    ev.wait(lambda: ev.committed_offsets() == 2, EVENTAL, "그룹 오프셋이 독·표지 둘 다 넘어감(2)")
    assert [r[0] for r in ev.notifications()] == [sentinel["eventId"]], "독이 든 메시지로는 알림이 생기지 않는다"
    assert ev.procs[0].alive(), f"notifier 가 독이 든 메시지에 죽었다\n{ev.diag()}"


@pytest.mark.parametrize("case", sorted(POISON))
def test_us05_ac15_poison_message_is_skipped_without_blocking_partition(ev, case):
    base = ev.confirmed_event()
    _expect_skipped_and_sentinel_delivered(ev, POISON[case](base))


def test_us05_ac15_poison_key_not_equal_show_id_is_skipped(ev):
    base = ev.confirmed_event()
    _expect_skipped_and_sentinel_delivered(ev, _fresh(base), key=b"some-other-show")


def test_us05_ac15_valid_255_char_user_id_is_accepted(ev):
    """경계: 1~255자는 받아들인다(256자부터 독)."""
    base = ev.confirmed_event()
    ev.start_notifier()
    ev.send_event(_fresh(base, userId="b" * 255))
    ev.wait(lambda: ev.notification_count() == 1, EVENTAL, "255자 userId 알림")
    assert ev.notifications()[0][1] == "b" * 255


def test_us05_ac15_transient_db_block_does_not_advance_offset_then_delivers_once(ev):
    """(a) 알림 저장이 일시적으로 막히는 동안(SHARE 잠금: 읽기는 되고 쓰기만 막음) 오프셋은 그대로·알림 0. 풀리면 알림 1·오프셋 전진."""
    ev.start_pipeline()
    ev.wait(lambda: ev.procs[1].alive(), 5, "notifier 시작")
    locker = psycopg.connect(EVENTS_DSN)
    try:
        locker.execute("LOCK TABLE notifications IN SHARE MODE")
        booking, _ = ev.confirm()
        ev.wait(lambda: ev.outbox_rows()[0][7] is not None, EVENTAL, "릴레이가 Kafka 로 보냄(sent_at)")
        # 막힌 동안의 '안 일어남'은 시계로 관찰(이 3초 동안 오프셋·알림 불변). 막힘이 풀린 뒤의 도착은 상한(EVENTAL)으로 단정.
        import time  # noqa: PLC0415
        time.sleep(3)
        assert ev.notification_count() == 0
        assert ev.committed_offsets() == 0, "DB 저장이 막힌 동안 오프셋이 전진했다(DB 커밋 전에 오프셋 커밋)"
    finally:
        locker.rollback()
        locker.close()
    ev.wait(lambda: ev.notification_count() == 1, EVENTAL, "막힘이 풀린 뒤 알림 1")
    ev.wait(lambda: ev.committed_offsets() == 1, EVENTAL, "알림 저장 뒤 오프셋 전진")
    assert len(ev.notifications_for(booking["id"])) == 1


def test_us05_ac15_notification_already_committed_before_offset_commit_stays_single(ev):
    """(b) DB 커밋은 됐고 오프셋 커밋 전(알림 행이 이미 있음) 상태에서 같은 메시지를 받아도 알림 1 그대로, 오프셋 전진."""
    event = ev.confirmed_event()
    with psycopg.connect(EVENTS_DSN) as conn:
        conn.execute("INSERT INTO notifications (event_id, user_id, booking_id, type, message) "
                     "VALUES (%s, %s, %s, 'BOOKING_CONFIRMED', %s)",
                     (event["eventId"], event["userId"], event["bookingId"], "이전 처리"))
    ev.start_notifier()
    ev.send_event(event)
    ev.wait(lambda: ev.committed_offsets() == 1, EVENTAL, "중복 메시지도 처리 후 오프셋 전진")
    rows = ev.notifications()
    assert len(rows) == 1 and rows[0][4] == "이전 처리", "기존 알림을 덮어쓰거나 늘리지 않는다(ON CONFLICT DO NOTHING)"


def test_us05_ac11_outbox_insert_failure_rolls_back_booking_and_seat(ev):
    """(c) outbox INSERT 가 실패하면 콜백 트랜잭션 전체가 롤백된다(예매 PENDING·좌석 HELD·outbox 0). 장애를 없애고 같은 콜백을 다시 보내면 정상."""
    seat = ev.seat_id()
    b = ev.hold_and_pay(seat, ev.user())
    with psycopg.connect(EVENTS_DSN, autocommit=True) as conn:
        conn.execute("CREATE OR REPLACE FUNCTION test_fail_outbox() RETURNS trigger LANGUAGE plpgsql AS "
                     "$$ BEGIN RAISE EXCEPTION 'outbox down (test)'; END $$")
        conn.execute("CREATE TRIGGER test_fail_outbox BEFORE INSERT ON outbox FOR EACH ROW EXECUTE FUNCTION test_fail_outbox()")
    try:
        r = ev.callback(b["paymentId"], "SUCCEEDED")
        assert r.status_code >= 500, f"outbox 를 못 쓰면 5xx 여야 한다: {r.status_code} {r.text}"
        assert ev.q("SELECT status FROM bookings WHERE id=%s", (b["id"],))[0][0] == "PENDING", "예매가 롤백되지 않았다"
        assert ev.q("SELECT status FROM seats WHERE id=%s", (seat,))[0][0] == "HELD", "좌석이 롤백되지 않았다"
        assert ev.outbox_count() == 0
    finally:
        with psycopg.connect(EVENTS_DSN, autocommit=True) as conn:
            conn.execute("DROP TRIGGER IF EXISTS test_fail_outbox ON outbox")
            conn.execute("DROP FUNCTION IF EXISTS test_fail_outbox()")
    again = ev.callback(b["paymentId"], "SUCCEEDED")
    assert again.status_code == 200, again.text
    assert again.json()["applied"] is True and again.json()["booking"]["status"] == "CONFIRMED"
    assert ev.outbox_count() == 1
    assert ev.q("SELECT status FROM seats WHERE id=%s", (seat,))[0][0] == "SOLD"
