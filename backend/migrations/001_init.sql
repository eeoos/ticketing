CREATE TABLE IF NOT EXISTS shows (
    id        text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    title     text NOT NULL,
    venue     text NOT NULL,
    starts_at timestamptz NOT NULL
);

-- 좌석 상태와 홀드 정보를 한 행에 둔다. 홀드/확정은 이 행에 대한 조건부 UPDATE·행 잠금으로 직렬화된다.
-- status 는 AVAILABLE/HELD/SOLD. 만료된 HELD 는 조회·홀드 시점의 now(앱 get_now)로 판단한다.
CREATE TABLE IF NOT EXISTS seats (
    id              text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    show_id         text NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    section         text NOT NULL,
    row_label       text NOT NULL,
    number          integer NOT NULL CHECK (number >= 1),
    status          text NOT NULL DEFAULT 'AVAILABLE' CHECK (status IN ('AVAILABLE','HELD','SOLD')),
    hold_user_id    text,
    hold_expires_at timestamptz,
    UNIQUE (show_id, section, row_label, number)
);
CREATE INDEX IF NOT EXISTS seats_show_idx ON seats(show_id);

CREATE TABLE IF NOT EXISTS bookings (
    id         text PRIMARY KEY DEFAULT gen_random_uuid()::text,
    seat_id    text NOT NULL UNIQUE REFERENCES seats(id) ON DELETE CASCADE,
    user_id    text NOT NULL,
    created_at timestamptz NOT NULL
);
