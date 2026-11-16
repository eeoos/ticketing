"""outbox → Kafka 릴레이 (docs/events.md). 전송 확인(delivery report)을 받은 줄만 sent_at 을 채운다."""
import json
import logging
import os
import signal
import threading
import time

import psycopg
from confluent_kafka import Producer

from backend.db import resolve_dsn

log = logging.getLogger("relay")
stop = threading.Event()


def _config() -> dict:
    return {"bootstrap.servers": os.environ.get("KAFKA_BOOTSTRAP", "localhost:9094"),
            "acks": "all", "enable.idempotence": True, "client.id": "outbox-relay"}


def relay_batch(conn: psycopg.Connection, producer: Producer, topic: str, batch: int) -> int:
    """한 트랜잭션: 미전송 줄을 잠그고(SKIP LOCKED) 전송 → 확인된 줄만 sent_at, 실패는 attempts·last_error. 처리한 줄 수."""
    rows = conn.execute(
        """SELECT event_id, show_id, payload FROM outbox WHERE sent_at IS NULL
           ORDER BY created_at, event_id LIMIT %s FOR UPDATE SKIP LOCKED""", (batch,)).fetchall()
    if not rows:
        conn.rollback()
        return 0
    results: dict[str, str | None] = {}

    def make_cb(event_id):
        def cb(err, _msg):
            results[event_id] = None if err is None else str(err)
        return cb

    for event_id, show_id, payload in rows:
        while True:
            try:
                producer.produce(topic, value=json.dumps(payload).encode(), key=show_id.encode(),
                                 headers=[("eventId", event_id.encode())], on_delivery=make_cb(event_id))
                break
            except BufferError:
                producer.poll(0.5)
            except Exception as e:  # noqa: BLE001
                results[event_id] = str(e)
                break
    deadline = time.monotonic() + 30
    while len(results) < len(rows) and time.monotonic() < deadline:
        producer.poll(0.2)
    for event_id, _, _ in rows:
        err = results.get(event_id, "delivery report timeout")
        if err is None:
            conn.execute("UPDATE outbox SET sent_at = now(), attempts = attempts + 1, last_error = NULL WHERE event_id = %s",
                         (event_id,))
        else:
            conn.execute("UPDATE outbox SET attempts = attempts + 1, last_error = %s WHERE event_id = %s",
                         (err[:500], event_id))
    conn.commit()
    return len(rows)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s relay %(levelname)s %(message)s")
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    topic = os.environ.get("EVENTS_TOPIC", "booking-events")
    batch = int(os.environ.get("RELAY_BATCH", "100"))
    poll = int(os.environ.get("RELAY_POLL_MS", "500")) / 1000
    producer = Producer(_config())
    backoff = 0.0
    conn = None
    log.info("start topic=%s batch=%d poll=%.3fs", topic, batch, poll)
    while not stop.is_set():
        try:
            if conn is None or conn.closed:
                conn = psycopg.connect(resolve_dsn(), application_name="ticketing-relay")
            n = relay_batch(conn, producer, topic, batch)
            failed = n and conn.execute("SELECT count(*) FROM outbox WHERE sent_at IS NULL AND last_error IS NOT NULL").fetchone()[0]
            conn.rollback()
            backoff = min(max(backoff * 2, 1.0), 10.0) if failed else 0.0
            if n and not failed:
                continue  # 더 있을 수 있으니 바로 다음 배치
        except Exception as e:  # noqa: BLE001
            log.warning("relay error: %s", e)
            backoff = min(max(backoff * 2, 1.0), 10.0)
            try:
                if conn is not None:
                    conn.close()
            except Exception:  # noqa: BLE001
                pass
            conn = None
        stop.wait(max(poll, backoff))
    try:
        producer.flush(10)
        if conn is not None:
            conn.close()
    finally:
        log.info("stopped")


if __name__ == "__main__":
    main()
