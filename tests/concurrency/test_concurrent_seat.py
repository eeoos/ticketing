"""동시성 C-01 / C-02 (docs/concurrency.md). 실제 uvicorn 워커 4 + PostgreSQL."""
import psycopg
import pytest

from concurrency_support import (APP_NAME_PREFIX, bookings_of, callback_request, first_seat, hold_and_pay,
                                 run_concurrently, seat_status, sync_get)

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
    assert run.count_5xx_or_error() == 0, f"5xx/연결 오류가 있다. {ev}"
    assert counts.get(201, 0) == 1, f"201 이 정확히 1건이어야 한다. {ev}"
    assert counts.get(409, 0) == HOLDERS - 1, f"409 가 정확히 {HOLDERS - 1}건이어야 한다. {ev}"
    assert counts == {201: 1, 409: HOLDERS - 1}, f"예상 밖 응답이 있다. {ev}"
    assert run.code_409_counts() == {"SEAT_HELD": HOLDERS - 1}, f"409 code 는 모두 SEAT_HELD. {ev}"
    winner = next(r.body["userId"] for r in run.results if r.status == 201)
    assert seat_status(server, seat) == "HELD", f"좌석은 HELD 여야 한다. {ev}"
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT status, hold_user_id FROM seats WHERE id = %s", (seat,)).fetchone()
    assert row == ("HELD", winner), f"DB 의 홀드 사용자는 201 을 받은 {winner} 여야 한다: {row}. {ev}"


def assert_no_5xx(run, ev):
    """5xx·연결 오류만 판정한다. 그 밖의 예상 밖 코드는 각 테스트의 정확한 분포 단언이 잡는다."""
    assert run.count_5xx_or_error() == 0, f"5xx/연결 오류가 있다. {ev}"


def pay_req(seat, user, key):
    return dict(method="POST", url="/bookings", headers={"X-User-Id": user, "Idempotency-Key": key},
                json={"seatId": seat})


def first_two_seats(server):
    show = sync_get(server, "/shows")[0]["id"]
    seats = sync_get(server, f"/shows/{show}/seats")
    return seats[0]["id"], seats[1]["id"]


def db_hold(dsn, seat):
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT status FROM seats WHERE id = %s", (seat,)).fetchone()[0]


def test_c02_same_user_20_concurrent_payment_flows_end_with_one_confirmed_booking(server, dsn, report):
    """홀드한 본인이 새 키로 '결제 요청 → (202 이면) 성공 콜백' 흐름을 20개 동시에 한다. 좌석당 확정 예매는 하나."""
    seat = first_seat(server)
    assert run_concurrently(server, dsn, [dict(method="POST", url=f"/seats/{seat}/hold",
                                               headers={"X-User-Id": "A"})]).results[0].status == 201

    def flow(i):
        async def run(call):
            r = await call(**pay_req(seat, "A", f"flow-{i}"))
            if r.status == 202:
                await call(**callback_request(r.body["paymentId"], "SUCCEEDED"))
        return run

    run = run_concurrently(server, dsn, [flow(i) for i in range(CONFIRMS)])
    check_overlap(run, report, "C-02")
    ev = run.evidence()
    assert_no_5xx(run, ev)
    # 결제 요청 응답(202/409/200=이미 확정)과 콜백 응답(본문에 applied)을 구분한다
    pay_results = [r for r in run.results if not (isinstance(r.body, dict) and "applied" in r.body)]
    assert {r.status for r in run.results} <= {200, 202, 409}, f"200/202/409 만 허용. {ev}"
    n202 = sum(1 for r in pay_results if r.status == 202)
    assert n202 == 1, f"결제 요청 202 가 정확히 1건이어야 한다(실제 {n202}). {ev}"
    bad409 = {r.code for r in pay_results if r.status == 409} - {"PAYMENT_IN_PROGRESS"}
    assert not bad409, f"409 code 는 PAYMENT_IN_PROGRESS 만 허용(확정 뒤에는 200). 실제 {bad409}. {ev}"
    rows = bookings_of(dsn, seat)
    assert [x[2] for x in rows] == ["CONFIRMED"], f"예매 행은 CONFIRMED 하나뿐이어야 한다: {rows}. {ev}"
    assert seat_status(server, seat) == "SOLD", ev


def test_c03_same_user_same_key_20_concurrent_requests_create_one_booking(server, dsn, report):
    seat = first_seat(server)
    assert run_concurrently(server, dsn, [dict(method="POST", url=f"/seats/{seat}/hold",
                                               headers={"X-User-Id": "A"})]).results[0].status == 201
    run = run_concurrently(server, dsn, [pay_req(seat, "A", "same-key") for _ in range(CONFIRMS)])
    check_overlap(run, report, "C-03")
    ev = run.evidence()
    assert_no_5xx(run, ev)
    assert run.status_counts() == {202: CONFIRMS}, f"모두 202 여야 한다. {ev}"
    assert len({r.body["id"] for r in run.results}) == 1, f"모든 응답이 같은 예매 id 여야 한다. {ev}"
    assert len({r.body["paymentId"] for r in run.results}) == 1, ev
    assert all(r.body["status"] == "PENDING" for r in run.results), ev
    rows = bookings_of(dsn, seat)
    assert len(rows) == 1, f"bookings 행이 정확히 1건이어야 한다: {rows}. {ev}"


def test_c04_same_hold_20_different_keys_one_pending(server, dsn, report):
    seat = first_seat(server)
    assert run_concurrently(server, dsn, [dict(method="POST", url=f"/seats/{seat}/hold",
                                               headers={"X-User-Id": "A"})]).results[0].status == 201
    run = run_concurrently(server, dsn, [pay_req(seat, "A", f"key-{i}") for i in range(CONFIRMS)])
    check_overlap(run, report, "C-04")
    ev = run.evidence()
    assert_no_5xx(run, ev)
    assert run.status_counts() == {202: 1, 409: CONFIRMS - 1}, f"202 1건·409 {CONFIRMS - 1}건. {ev}"
    assert run.code_409_counts() == {"PAYMENT_IN_PROGRESS": CONFIRMS - 1}, ev
    rows = bookings_of(dsn, seat)
    assert [x[2] for x in rows] == ["PENDING"], f"PENDING 예매 1행만 있어야 한다: {rows}. {ev}"


def test_c05_same_success_callback_20_concurrent_applies_exactly_once(server, dsn, report):
    seat = first_seat(server)
    _, booking = hold_and_pay(server, seat, "A")
    run = run_concurrently(server, dsn, [callback_request(booking["paymentId"], "SUCCEEDED")
                                         for _ in range(CONFIRMS)])
    check_overlap(run, report, "C-05")
    ev = run.evidence()
    assert_no_5xx(run, ev)
    assert run.status_counts() == {200: CONFIRMS}, f"모두 200 이어야 한다. {ev}"
    applied = sum(1 for r in run.results if r.body["applied"] is True)
    assert applied == 1, f"applied:true 는 정확히 1건이어야 한다(실제 {applied}). {ev}"
    assert {r.body["booking"]["status"] for r in run.results} == {"CONFIRMED"}, ev
    rows = bookings_of(dsn, seat)
    assert [x[2] for x in rows] == ["CONFIRMED"], f"CONFIRMED 1행: {rows}. {ev}"
    assert seat_status(server, seat) == "SOLD" and db_hold(dsn, seat) == "SOLD", ev


def test_c06_success_and_failed_callbacks_concurrently_match_one_of_the_two_orders(server, dsn, report):
    seat = first_seat(server)
    _, booking = hold_and_pay(server, seat, "A")
    reqs = ([callback_request(booking["paymentId"], "SUCCEEDED") for _ in range(10)]
            + [callback_request(booking["paymentId"], "FAILED") for _ in range(10)])
    run = run_concurrently(server, dsn, reqs)
    check_overlap(run, report, "C-06")
    ev = run.evidence()
    assert_no_5xx(run, ev)
    statuses = run.status_counts()
    assert set(statuses) <= {200, 409}, f"200/409 만 허용. {ev}"
    assert run.code_409_counts() in ({}, {"CALLBACK_CONFLICT": statuses.get(409, 0)}), f"409 는 CALLBACK_CONFLICT. {ev}"
    rows = bookings_of(dsn, seat)
    assert len(rows) == 1, f"예매는 하나뿐이어야 한다: {rows}. {ev}"
    final = rows[0][2]
    applied = sum(1 for r in run.results if r.status == 200 and r.body["applied"] is True)
    if final == "CONFIRMED":  # 순서 1: SUCCEEDED 먼저 → FAILED 는 전부 409
        assert seat_status(server, seat) == "SOLD" and db_hold(dsn, seat) == "SOLD", ev
        assert applied == 1, f"CONFIRMED 경로의 applied:true 는 1건. 실제 {applied}. {ev}"
        assert statuses.get(409) == 10, f"FAILED 10건은 전부 409 여야 한다. {ev}"
    else:  # 순서 2: FAILED 먼저 → SUCCEEDED 가 REFUND_REQUIRED
        assert final == "REFUND_REQUIRED", f"허용되는 최종 상태는 CONFIRMED / REFUND_REQUIRED. 실제 {final}. {ev}"
        assert seat_status(server, seat) == "HELD" and db_hold(dsn, seat) == "HELD", \
            f"REFUND_REQUIRED 일 때 좌석은 팔리면 안 된다. {ev}"
        assert applied == 2, f"FAILED 1회 + SUCCEEDED 1회 = applied:true 2건. 실제 {applied}. {ev}"
    assert not [r for r in rows if r[2] == "CONFIRMED"] or db_hold(dsn, seat) == "SOLD", ev  # 불변식 5


def test_c07_same_user_same_key_different_seats_concurrently_one_booking_other_seat_mismatch(server, dsn, report):
    """같은 사용자가 두 좌석을 홀드하고 같은 키로 S1·S2 결제 요청을 섞어 동시 20건(각 10).
    좌석 행 잠금이 직렬화해 주지 않으므로 (user_id, idempotency_key) UNIQUE 충돌 재시도 분기를 탄다."""
    seat1, seat2 = first_two_seats(server)
    for seat in (seat1, seat2):
        assert run_concurrently(server, dsn, [dict(method="POST", url=f"/seats/{seat}/hold",
                                                   headers={"X-User-Id": "A"})]).results[0].status == 201
    reqs = []
    for _ in range(CONFIRMS // 2):  # S1, S2 번갈아
        reqs += [pay_req(seat1, "A", "k"), pay_req(seat2, "A", "k")]
    run = run_concurrently(server, dsn, reqs)
    check_overlap(run, report, "C-07")
    ev = run.evidence()
    assert_no_5xx(run, ev)
    assert run.status_counts() == {202: CONFIRMS // 2, 409: CONFIRMS // 2}, f"202 10건·409 10건. {ev}"
    assert run.code_409_counts() == {"IDEMPOTENCY_KEY_MISMATCH": CONFIRMS // 2}, ev
    rows = bookings_of(dsn, seat1) + bookings_of(dsn, seat2)
    assert len(rows) == 1 and rows[0][2] == "PENDING", f"예매 1행(PENDING)만 있어야 한다: {rows}. {ev}"
    won = {r.body["seatId"] for r in run.results if r.status == 202}
    assert len(won) == 1, f"202 는 이긴 좌석 하나에서만 와야 한다: {won}. {ev}"
    assert len({r.body["id"] for r in run.results if r.status == 202}) == 1, f"202 는 모두 같은 예매 id. {ev}"
    winner = won.pop()
    loser = seat2 if winner == seat1 else seat1
    assert bookings_of(dsn, loser) == [], f"진 좌석에 예매가 있으면 안 된다. {ev}"
    assert seat_status(server, loser) == "HELD" and seat_status(server, winner) == "HELD", ev
