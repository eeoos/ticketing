"""booking-events → notifications (docs/events.md). INSERT(ON CONFLICT DO NOTHING) → DB 커밋 → 오프셋 동기 커밋.

오류는 둘로 나눈다. 영구(메시지 자체가 계약 밖이거나 DB 가 값 때문에 거절) → 로그 후 오프셋만 커밋해 넘김.
일시(연결 끊김·시간 초과 등) → 롤백, 같은 메시지로 되돌아가(seek) 백오프 뒤 재처리. 그동안 poll 을 유지해 그룹에서 쫓겨나지 않는다.
"""
import json
import logging
import os
import signal
import threading
import time
from datetime import datetime

import psycopg
from confluent_kafka import Consumer, TopicPartition
from psycopg import errors as pgerr

from backend.db import resolve_dsn

log = logging.getLogger("notifier")
stop = threading.Event()
STR_FIELDS = ("eventId", "type", "bookingId", "showId", "seatId", "userId", "paymentId")
BACKOFF_MAX = 10.0


class Poison(Exception):
    pass


def _str_ok(v) -> bool:
    return isinstance(v, str) and 1 <= len(v) <= 255 and "\x00" not in v


def validate(msg) -> dict:
    """받아들이는 메시지면 이벤트 dict, 아니면 Poison(원인)."""
    try:
        ev = json.loads(msg.value())
    except (TypeError, ValueError, UnicodeDecodeError):
        raise Poison("JSON 이 아님") from None
    if not isinstance(ev, dict):
        raise Poison("JSON 객체가 아님")
    missing = [k for k in (*STR_FIELDS, "version", "occurredAt") if k not in ev]
    if missing:
        raise Poison(f"필드 없음: {missing}")
    bad = [k for k in STR_FIELDS if not _str_ok(ev[k])]
    if bad:
        raise Poison(f"문자열 필드 형식 위반(비어 있음·NUL·255자 초과·문자열 아님): {bad}")
    if ev["type"] != "BookingConfirmed":
        raise Poison(f"type 이 BookingConfirmed 가 아님: {ev['type']!r}")
    if type(ev["version"]) is not int or ev["version"] != 1:
        raise Poison(f"version 이 정수 1 이 아님: {ev['version']!r}")
    try:
        if not isinstance(ev["occurredAt"], str):
            raise ValueError
        datetime.fromisoformat(ev["occurredAt"])
    except ValueError:
        raise Poison(f"occurredAt 이 ISO 8601 이 아님: {ev['occurredAt']!r}") from None
    try:
        key = msg.key().decode() if msg.key() is not None else None
    except UnicodeDecodeError:
        key = None
    if key != ev["showId"]:
        raise Poison(f"메시지 키({key!r})가 showId({ev['showId']!r})와 다름")
    return ev


def save(conn: psycopg.Connection, ev: dict) -> None:
    with conn.transaction():
        conn.execute(
            """INSERT INTO notifications (event_id, user_id, booking_id, type, message)
               VALUES (%s, %s, %s, 'BOOKING_CONFIRMED', %s) ON CONFLICT (event_id) DO NOTHING""",
            (ev["eventId"], ev["userId"], ev["bookingId"], "예매가 확정되었습니다."))
    conn.commit()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s notifier %(levelname)s %(message)s")
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    topic = os.environ.get("EVENTS_TOPIC", "booking-events")
    group = os.environ.get("NOTIFIER_GROUP", "notifier")
    consumer = Consumer({"bootstrap.servers": os.environ.get("KAFKA_BOOTSTRAP", "localhost:9094"),
                         "group.id": group, "enable.auto.commit": False, "auto.offset.reset": "earliest"})
    consumer.subscribe([topic])
    conn = None
    backoff, paused_until = 0.0, 0.0
    log.info("start topic=%s group=%s", topic, group)
    try:
        while not stop.is_set():
            if paused_until and time.monotonic() >= paused_until:
                consumer.resume(consumer.assignment())
                paused_until = 0.0
            msg = consumer.poll(0.5)  # 일시 오류로 멈춘 동안에도 poll 을 돌려 그룹에 남는다
            if msg is None:
                continue
            if msg.error():
                log.warning("consumer error: %s", msg.error())
                continue
            where = f"partition={msg.partition()} offset={msg.offset()}"
            try:
                ev = validate(msg)
                if conn is None or conn.closed:
                    conn = psycopg.connect(resolve_dsn(), application_name="ticketing-notifier")
                save(conn, ev)
            except Poison as e:
                log.error("영구 오류, 건너뜀: %s eventId=%s %s", e, _event_id(msg), where)
            except (pgerr.DataError, pgerr.IntegrityError) as e:
                _rollback(conn)
                log.error("영구 오류(DB 가 값을 거절), 건너뜀: %s eventId=%s %s", e, _event_id(msg), where)
            except Exception as e:  # noqa: BLE001  일시 오류: 커밋하지 않고 같은 메시지로 되돌아간다
                log.warning("일시 오류, 재처리 예정: %s %s", e, where)
                _rollback(conn)
                try:
                    if conn is not None:
                        conn.close()
                except Exception:  # noqa: BLE001
                    pass
                conn = None
                backoff = min(max(backoff * 2, 1.0), BACKOFF_MAX)
                consumer.pause(consumer.assignment())
                consumer.seek(TopicPartition(msg.topic(), msg.partition(), msg.offset()))
                paused_until = time.monotonic() + backoff
                continue
            backoff = 0.0
            consumer.commit(message=msg, asynchronous=False)
    finally:
        try:
            consumer.close()
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            log.info("stopped")


def _event_id(msg) -> str | None:
    for k, v in msg.headers() or []:
        if k == "eventId":
            return v.decode(errors="replace") if v else None
    return None


def _rollback(conn) -> None:
    try:
        if conn is not None and not conn.closed:
            conn.rollback()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
