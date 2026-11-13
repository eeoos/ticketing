"""동시성 C-01 / C-02 (docs/concurrency.md). 실제 uvicorn 워커 4 + PostgreSQL."""
import psycopg
import pytest

from concurrency_support import APP_NAME_PREFIX, first_seat, run_concurrently, seat_status

HOLDERS = 500
CONFIRMS = 20


def check_overlap(run, report, name):
    """겹침 증거가 없으면 RED/GREEN 판정 무효(실패)."""
    report(name, run.evidence())
    ev = run.evidence()
    assert run.sampler_error is None, f"pg_stat_activity 샘플러 오류 — 판정 무효. {ev}"
    assert run.pg_samples > 0, f"유효한 pg_stat_activity 샘플이 0 — 판정 무효. {ev}"
    assert run.max_inflight >= 2, f"요청이 겹치지 않았다 — 판정 무효. {ev}"
    assert len(run.worked_names) >= 2, (
        f"실행 중 DB 작업을 한 워커('{APP_NAME_PREFIX}*')가 2개 미만 — 단일 워커 처리이거나 "
        f"application_name 미설정이라 판정 무효. {ev}")


def test_c01_500_users_hold_same_seat_exactly_one_wins(server, dsn, report):
    seat = first_seat(server)
    reqs = [dict(method="POST", url=f"/seats/{seat}/hold", headers={"X-User-Id": f"user-{i}"})
            for i in range(HOLDERS)]
    run = run_concurrently(server, dsn, reqs)
    check_overlap(run, report, "C-01")
    ev = run.evidence()
    counts = run.status_counts()
    assert counts.get("5xx", 0) == 0 and counts.get("error", 0) == 0, f"5xx/연결 오류가 있다. {ev}"
    assert counts.get(201, 0) == 1, f"201 이 정확히 1건이어야 한다. {ev}"
    assert counts.get(409, 0) == HOLDERS - 1, f"409 가 정확히 {HOLDERS - 1}건이어야 한다. {ev}"
    assert counts.get("other", 0) == 0, f"예상 밖 응답이 있다. {ev}"
    assert run.code_409_counts() == {"SEAT_HELD": HOLDERS - 1}, f"409 code 는 모두 SEAT_HELD. {ev}"
    winner = next(r.body["userId"] for r in run.results if r.status == 201)
    assert seat_status(server, seat) == "HELD", f"좌석은 HELD 여야 한다. {ev}"
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT status, hold_user_id FROM seats WHERE id = %s", (seat,)).fetchone()
    assert row == ("HELD", winner), f"DB 의 홀드 사용자는 201 을 받은 {winner} 여야 한다: {row}. {ev}"


def test_c02_same_user_confirms_booking_20_times_exactly_one_booking(server, dsn, report):
    seat = first_seat(server)
    held = run_concurrently(server, dsn, [dict(method="POST", url=f"/seats/{seat}/hold",
                                               headers={"X-User-Id": "A"})])
    assert held.results[0].status == 201, held.evidence()
    reqs = [dict(method="POST", url="/bookings", headers={"X-User-Id": "A"}, json={"seatId": seat})
            for _ in range(CONFIRMS)]
    run = run_concurrently(server, dsn, reqs)
    check_overlap(run, report, "C-02")
    ev = run.evidence()
    with psycopg.connect(dsn) as conn:
        bookings = conn.execute("SELECT count(*) FROM bookings WHERE seat_id = %s", (seat,)).fetchone()[0]
    counts = run.status_counts()
    assert bookings == 1, f"DB bookings 에 그 좌석 행이 정확히 1건이어야 한다(실제 {bookings}). {ev}"
    assert counts.get("5xx", 0) == 0 and counts.get("error", 0) == 0, f"5xx/연결 오류가 있다. {ev}"
    assert counts.get(201, 0) == 1, f"201 이 정확히 1건이어야 한다. {ev}"
    assert counts.get(409, 0) == CONFIRMS - 1, f"409 가 정확히 {CONFIRMS - 1}건이어야 한다. {ev}"
    assert counts.get("other", 0) == 0, f"예상 밖 응답이 있다. {ev}"
    assert run.code_409_counts() == {"SEAT_SOLD": CONFIRMS - 1}, f"409 code 는 모두 SEAT_SOLD. {ev}"
    assert seat_status(server, seat) == "SOLD", f"좌석은 SOLD 여야 한다. {ev}"
