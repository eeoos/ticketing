-- 6장: 결제 2단계(PENDING → 콜백), 멱등 키, 홀드 id. 기존 데이터가 있는 DB 에도 적용된다.
-- 기존 예매 행 = 이미 확정된 예매 → CONFIRMED, 새 payment_id·hold_id·updated_at 을 받는다.
-- 기존 HELD 좌석 = 새 hold_id 를 받는다. SOLD 좌석은 그 좌석의 예매 hold_id 를 가져 짝(불변식 5)을 유지한다.

ALTER TABLE seats ADD COLUMN hold_id text;
UPDATE seats SET hold_id = gen_random_uuid()::text WHERE status = 'HELD';

ALTER TABLE bookings
    ADD COLUMN status          text,
    ADD COLUMN payment_id      text,
    ADD COLUMN hold_id         text,
    ADD COLUMN hold_expires_at timestamptz,
    ADD COLUMN updated_at      timestamptz,
    ADD COLUMN idempotency_key text,      -- 최초 202 를 만든 요청의 키(기존 행·재시도 외 행은 NULL 이 아님)
    ADD COLUMN request_seat_id text,      -- 요청 지문: 정규화한 본문 {seatId}
    ADD COLUMN response_status integer,   -- 최초 응답(202)의 상태 코드
    ADD COLUMN response_body   jsonb;     -- 최초 응답 본문 전체(재요청 시 그대로 재현)

UPDATE bookings SET status = 'CONFIRMED',
                    payment_id = gen_random_uuid()::text,
                    hold_id = gen_random_uuid()::text,
                    hold_expires_at = created_at + interval '5 minutes',
                    updated_at = created_at;

UPDATE seats s SET hold_id = b.hold_id FROM bookings b WHERE b.seat_id = s.id AND s.status = 'SOLD';

ALTER TABLE bookings
    ALTER COLUMN status SET NOT NULL,
    ALTER COLUMN payment_id SET NOT NULL,
    ALTER COLUMN hold_id SET NOT NULL,
    ALTER COLUMN hold_expires_at SET NOT NULL,
    ALTER COLUMN updated_at SET NOT NULL,
    ADD CONSTRAINT bookings_status_check CHECK (status IN ('PENDING', 'CONFIRMED', 'FAILED', 'REFUND_REQUIRED')),
    ADD CONSTRAINT bookings_idempotency_complete CHECK (
        idempotency_key IS NULL
        OR (request_seat_id IS NOT NULL AND response_status IS NOT NULL AND response_body IS NOT NULL));

-- 불변식 1: 좌석당 CONFIRMED 예매는 최대 하나. 전체 UNIQUE(seat_id)는 실패·환불 필요 기록과 재시도를 담을 수 없어 대체한다.
ALTER TABLE bookings DROP CONSTRAINT IF EXISTS bookings_seat_id_key;
CREATE UNIQUE INDEX bookings_one_confirmed_per_seat ON bookings (seat_id) WHERE status = 'CONFIRMED';
-- 불변식 2: 홀드당 PENDING 예매는 최대 하나.
CREATE UNIQUE INDEX bookings_one_pending_per_hold ON bookings (hold_id) WHERE status = 'PENDING';
-- 불변식 3: 멱등 키는 사용자별 하나(NULL 은 서로 다른 값으로 취급되므로 기존 행은 영향 없음).
ALTER TABLE bookings ADD CONSTRAINT bookings_user_idempotency_key UNIQUE (user_id, idempotency_key);
-- 불변식 4: payment_id UNIQUE.
ALTER TABLE bookings ADD CONSTRAINT bookings_payment_id_key UNIQUE (payment_id);
CREATE INDEX bookings_seat_idx ON bookings (seat_id);
