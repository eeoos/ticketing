from datetime import datetime, timezone

from backend.db import connect


def run(dsn: str | None = None) -> None:
    """예매·좌석·공연을 모두 지우고 공연 1개 + 좌석 100석(AVAILABLE)을 넣는다."""
    with connect(dsn) as conn:
        conn.execute("TRUNCATE bookings, seats, shows CASCADE")
        show_id = conn.execute(
            "INSERT INTO shows (title, venue, starts_at) VALUES (%s, %s, %s) RETURNING id",
            ("샘플 콘서트", "올림픽홀", datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc)),
        ).fetchone()[0]
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO seats (show_id, section, row_label, number) VALUES (%s, %s, %s, %s)",
                [(show_id, "A", row, n) for row in "ABCDEFGHIJ" for n in range(1, 11)],
            )


if __name__ == "__main__":
    run()
    print("seed: ok")
