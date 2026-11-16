"""10장 전달: outbox → 릴레이 → Kafka → notifier → notifications (docs/events.md). 릴레이·notifier 는 호스트 하위 프로세스.

기다림 상한은 events_support 머리말: EVENTAL=30초(결국 도착), RECOVER=90초(Kafka 복구 뒤).
'안 생겼다'는 같은 공연(같은 키·같은 파티션) 표지(sentinel) 확정의 알림이 도착한 뒤에 확인한다(시계로 단정하지 않음).
"""
import json
import uuid

import pytest

from events_support import EVENTAL, RECOVER, wait_until


def _wait_notified(ev, booking_ids, timeout=EVENTAL):
    want = {str(b) for b in booking_ids}
    ev.wait(lambda: {str(r[2]) for r in ev.notifications()} >= want, timeout, f"알림 {len(want)}건 도착")


def test_confirmation_flows_to_kafka_message_and_one_notification(ev):
    booking, user = ev.confirm()
    ev.start_pipeline()
    _wait_notified(ev, [booking["id"]])
    row = ev.outbox_rows()[0]
    rows = ev.notifications_for(booking["id"])
    assert len(rows) == 1 and rows[0][1] == user and str(rows[0][0]) == str(row[0])
    n = ev.notifications()[0]
    assert n[3] == "BOOKING_CONFIRMED" and n[4] and n[6] is None  # type, message, read_at
    ev.wait(lambda: ev.outbox_rows()[0][7] is not None, EVENTAL, "outbox.sent_at 채움")
    msgs = ev.read_topic()
    assert len(msgs) == 1, f"토픽에 메시지 1건: {msgs}"
    key, headers, value, _part = msgs[0]
    assert key.decode() == ev.show_id(), "키는 showId"
    assert headers["eventId"].decode() == str(row[0]), "헤더 eventId = outbox.event_id"
    body = json.loads(value)
    assert body["eventId"] == str(row[0]) and body["bookingId"] == booking["id"] and body["userId"] == user
    stored = row[5] if not isinstance(row[5], str) else json.loads(row[5])
    assert body == stored, "Kafka 메시지 값은 outbox.payload 와 같다"


def test_duplicate_callbacks_yield_one_outbox_one_notification(ev):
    b = ev.hold_and_pay(ev.seat_id(), ev.user())
    for _ in range(3):
        ev.callback(b["paymentId"], "SUCCEEDED")
    ev.start_pipeline()
    _wait_notified(ev, [b["id"]])
    assert ev.outbox_count() == 1 and ev.notification_count() == 1
    sentinel, _ = ev.confirm(ev.seat_id(1))  # 같은 공연·같은 키 → 같은 파티션에서 뒤에 처리됨
    _wait_notified(ev, [sentinel["id"]])
    assert ev.notification_count() == 2, "표지 도착 뒤에도 중복 알림이 없다"


def test_non_confirmations_never_notify(ev):
    failed = ev.hold_and_pay(ev.seat_id(0), ev.user())
    ev.callback(failed["paymentId"], "FAILED")
    late = ev.hold_and_pay(ev.seat_id(1), ev.user())
    ev.advance(minutes=5)
    ev.callback(late["paymentId"], "SUCCEEDED")  # REFUND_REQUIRED
    ev.start_pipeline()
    ev.advance(minutes=1)
    sentinel, _ = ev.confirm(ev.seat_id(2))
    _wait_notified(ev, [sentinel["id"]])
    assert ev.outbox_count() == 1
    assert [str(r[2]) for r in ev.notifications()] == [sentinel["id"]]


def test_relay_sets_sent_at_only_after_broker_confirms(ev):
    """Kafka 가 없는 동안엔 sent_at 이 NULL 이고 attempts 만 늘고, 예매 API 는 영향 없다. 켜면 한 번에 따라잡는다."""
    ev.start_pipeline()
    ev.stop_kafka()
    booking, _ = ev.confirm()  # 콜백은 Kafka 없이도 200 CONFIRMED + outbox 1줄
    row = ev.outbox_rows()[0]
    assert row[7] is None
    ev.wait(lambda: ev.outbox_rows()[0][8] >= 1, 60, "릴레이가 전송에 실패해 attempts 증가")
    row = ev.outbox_rows()[0]
    assert row[7] is None, "전송 확인 전에는 sent_at 을 채우지 않는다"
    assert row[9], "last_error 에 실패 이유가 남는다"
    assert ev.notification_count() == 0
    ev.start_kafka()
    _wait_notified(ev, [booking["id"]], RECOVER)
    ev.wait(lambda: ev.outbox_rows()[0][7] is not None, RECOVER, "복구 뒤 sent_at 채움")
    assert str(ev.outbox_rows()[0][0]) == str(row[0]), "재시도해도 eventId 는 그대로"
    assert len(ev.notifications_for(booking["id"])) == 1


def test_kafka_down_does_not_break_callback_and_recovers_to_single_notification(ev):
    ev.stop_kafka()
    outs = [ev.confirm(ev.seat_id(i))[0] for i in range(2)]  # 각 호출이 200 CONFIRMED 를 assert
    assert ev.outbox_count() == 2 and all(r[7] is None for r in ev.outbox_rows())
    ev.start_kafka()
    ev.start_pipeline()
    _wait_notified(ev, [b["id"] for b in outs], RECOVER)
    assert ev.notification_count() == 2 and ev.outbox_count() == 2


def test_notifier_restart_resumes_without_loss_or_duplicates(ev):
    relay = ev.start_relay()
    n1 = ev.start_notifier()
    first, _ = ev.confirm(ev.seat_id(0))
    _wait_notified(ev, [first["id"]])
    n1.stop()
    pending = [ev.confirm(ev.seat_id(i))[0] for i in range(1, 5)]  # notifier 가 꺼진 동안 4건
    ev.wait(lambda: all(r[7] is not None for r in ev.outbox_rows()), EVENTAL, "릴레이가 4건을 Kafka 에 보냄")
    assert ev.notification_count() == 1, "notifier 가 꺼져 있으면 알림이 늘지 않는다"
    ev.start_notifier()
    _wait_notified(ev, [first["id"]] + [b["id"] for b in pending])
    sentinel, _ = ev.confirm(ev.seat_id(5))
    _wait_notified(ev, [sentinel["id"]])
    assert ev.notification_count() == 6 and ev.outbox_count() == 6
    ids = [str(r[0]) for r in ev.notifications()]
    assert len(set(ids)) == 6
    assert relay.alive()


def test_redelivered_event_id_does_not_add_notification(ev):
    ev.start_pipeline()
    booking, _ = ev.confirm()
    _wait_notified(ev, [booking["id"]])
    ev.wait(lambda: ev.outbox_rows()[0][7] is not None, EVENTAL, "sent_at 채움")
    _, headers, value, _ = ev.read_topic()[0]
    ev.produce_raw(ev.show_id().encode(), value, [("eventId", headers["eventId"])])  # 같은 eventId 를 토픽에 직접 다시
    # 표지: 뒤에 다른 이벤트가 처리되면 위 재전송 메시지도 이미 처리된 것(같은 키·같은 파티션 순서)
    sentinel, _ = ev.confirm(ev.seat_id(1))
    _wait_notified(ev, [sentinel["id"]])
    assert len(ev.notifications_for(booking["id"])) == 1 and ev.notification_count() == 2


def test_relay_resend_after_sent_at_reset_does_not_add_notification(ev):
    ev.start_pipeline()
    booking, _ = ev.confirm()
    _wait_notified(ev, [booking["id"]])
    ev.wait(lambda: ev.outbox_rows()[0][7] is not None, EVENTAL, "sent_at 채움")
    event_id = str(ev.outbox_rows()[0][0])
    import psycopg  # noqa: PLC0415
    from events_support import EVENTS_DSN  # noqa: PLC0415
    with psycopg.connect(EVENTS_DSN) as conn:
        conn.execute("UPDATE outbox SET sent_at = NULL WHERE event_id = %s", (event_id,))
    ev.wait(lambda: ev.outbox_rows()[0][7] is not None, EVENTAL, "릴레이가 다시 보내고 sent_at 을 채움")
    assert str(ev.outbox_rows()[0][0]) == event_id, "다시 보내도 eventId 는 그대로"
    wait_until(lambda: len(ev.read_topic()) >= 2, EVENTAL, "토픽에 같은 eventId 메시지가 두 번")
    sentinel, _ = ev.confirm(ev.seat_id(1))
    _wait_notified(ev, [sentinel["id"]])
    assert len(ev.notifications_for(booking["id"])) == 1 and ev.notification_count() == 2


def test_offset_committed_only_after_notification_stored(ev):
    """처음 시작은 earliest 이므로, 메시지를 먼저 쌓고 notifier 를 켜면 그룹 오프셋이 알림 수만큼 오른다(DB 커밋 뒤 오프셋 커밋)."""
    ev.start_relay()
    done = [ev.confirm(ev.seat_id(i))[0] for i in range(3)]
    ev.wait(lambda: all(r[7] is not None for r in ev.outbox_rows()), EVENTAL, "3건 전송")
    assert ev.committed_offsets() == 0
    ev.start_notifier()
    _wait_notified(ev, [b["id"] for b in done])
    wait_until(lambda: ev.committed_offsets() == 3, EVENTAL, "그룹 오프셋 3 커밋")
    assert ev.notification_count() == 3, "오프셋이 알림보다 앞서 가면 안 된다(알림 없이 오프셋만 간 메시지 없음)"


def test_malformed_messages_are_skipped_and_do_not_block_notifier(ev):
    ev.start_notifier()
    ev.produce_raw(b"k1", b"this is not json", [("eventId", b"bad-1")])
    ev.produce_raw(b"k2", json.dumps({"eventId": str(uuid.uuid4())}).encode(), [("eventId", b"bad-2")])  # 필드 없음
    ev.produce_raw(b"k3", b"[1,2,3]")
    ev.start_relay()
    booking, _ = ev.confirm()
    _wait_notified(ev, [booking["id"]])
    assert ev.notification_count() == 1, "형식이 틀린 메시지는 알림이 되지 않고 넘어간다"
    wait_until(lambda: ev.committed_offsets() >= 4, EVENTAL, "틀린 메시지 3건 + 정상 1건의 오프셋이 커밋됨(무한 재시도 금지)")
    assert ev.procs[0].alive(), f"notifier 가 형식이 틀린 메시지에 죽었다\n{ev.diag()}"
