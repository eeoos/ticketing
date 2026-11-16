-- 10장: 예매 확정 알림. outbox(콜백 트랜잭션에서 1줄) + notifications(notifier 가 넣는 알림함).
CREATE TABLE IF NOT EXISTS outbox (
    event_id   text PRIMARY KEY,
    type       text NOT NULL,
    booking_id text NOT NULL,
    show_id    text NOT NULL,
    user_id    text NOT NULL,
    payload    jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    sent_at    timestamptz,
    attempts   integer NOT NULL DEFAULT 0,
    last_error text,
    UNIQUE (booking_id, type)
);
CREATE INDEX IF NOT EXISTS outbox_unsent_idx ON outbox (created_at, event_id) WHERE sent_at IS NULL;

CREATE TABLE IF NOT EXISTS notifications (
    id         bigserial PRIMARY KEY,
    event_id   text NOT NULL UNIQUE,
    user_id    text NOT NULL,
    booking_id text NOT NULL,
    type       text NOT NULL,
    message    text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    read_at    timestamptz
);
CREATE INDEX IF NOT EXISTS notifications_user_idx ON notifications (user_id, created_at);
