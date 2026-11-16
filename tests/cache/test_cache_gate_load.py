"""같은 좌석 동시 홀드 200건 (실제 uvicorn 워커 4 + PostgreSQL + Redis). HOLD_GATE on/off 비교.

DB 까지 간 요청 수를 두 지표로 본다: ① 응답 헤더 X-Hold-Gate: PASS 개수(DB 까지 감) ② 테스트 DB 의 pg_stat_database
xact_commit + xact_rollback 증가분.

기준과 근거
- on: 201 정확히 1·409(SEAT_HELD) 199·5xx 0. 관문은 Redis Lua 로 확인과 표시를 한 번에 하므로(단일 스레드 직렬 실행) 첫 요청이
  PENDING 으로 표시한 뒤 도착한 요청은 모두 REJECT 다. 그래서 PASS 는 이론상 1건 — 여유를 둬 PASS ≤ 10(5%)을 요구하고,
  REJECT ≥ 185 를 요구한다. BYPASS 는 0(Redis 정상).
- DB: off 는 200건이 모두 DB 트랜잭션을 만들어 증가분 ≥ 150 이어야(측정이 동작한다는 확인). on 은 off 의 25% 이하 —
  PASS ≈ 1 이므로 이론상 거의 0 이고, 증가분에 섞이는 서버 시작·공연/좌석 조회 몇 건(수 건~십여 건)과 통계 오차를 감안한 기준이다.
- 통계 반영: pg_stat_database 는 유휴 연결에서 최대 ~10초 늦게 반영되므로 기준값은 서버 시작 전에, 최종값은 서버 종료 뒤에 읽는다.
"""
import time
from collections import Counter

from cache_support import Server, delete_prefix, fire, new_prefix, reset_database, uid, xact_total

N = 200


def run_scenario(gate):
    """통계(pg_stat_database)는 유휴 연결에서 최대 ~10초 늦게 반영되므로, 서버를 띄우기 전에 기준값을 읽고
    서버를 종료(풀 연결이 닫히며 통계가 즉시 반영됨)한 뒤 최종값을 읽는다. 증가분에는 서버 시작·공연/좌석 조회 몇 건이 포함된다."""
    prefix = new_prefix()
    reset_database()  # 시드 연결도 닫혀 통계가 반영된다
    time.sleep(1.5)
    before = xact_total()
    srv = Server(prefix, gate).start()
    try:
        show = srv.client.get("/shows").json()[0]["id"]
        seat = srv.client.get(f"/shows/{show}/seats").json()[0]["id"]
        responses = fire(srv, [dict(method="POST", url=f"/seats/{seat}/hold", headers={"X-User-Id": uid("u")})
                               for _ in range(N)])
    finally:
        srv.stop()  # 워커 종료 → DB 연결 닫힘 → 통계 반영
        delete_prefix(prefix)
    time.sleep(2)
    after = xact_total()
    errors = [x for x in responses if isinstance(x, Exception)]
    ok = [x for x in responses if not isinstance(x, Exception)]
    return {
        "statuses": dict(Counter(x.status_code for x in ok)),
        "codes409": dict(Counter(x.json().get("code") for x in ok if x.status_code == 409)),
        "gate": dict(Counter(x.headers.get("X-Hold-Gate") for x in ok)),
        "errors": len(errors), "tx_delta": after - before,
    }


def test_cache_gate_200_concurrent_holds_one_winner_and_far_fewer_db_requests(report):
    try:
        off = run_scenario("off")
        on = run_scenario("on")
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"부하 시나리오를 실행하지 못했다: {type(e).__name__}: {str(e)[-1500:]}") from e
    summary = f"off={off} | on={on}"
    report("동시 홀드 200", summary)
    for name, res in (("off", off), ("on", on)):
        assert res["errors"] == 0, f"{name}: 응답 없는 요청 {res['errors']}건. {summary}"
        assert res["statuses"] == {201: 1, 409: N - 1}, f"{name}: 201 1건·409 {N - 1}건이어야 한다(5xx 0). {summary}"
        assert res["codes409"] == {"SEAT_HELD": N - 1}, f"{name}: 409 는 모두 SEAT_HELD. {summary}"
    assert set(on["gate"]) <= {"PASS", "REJECT", "BYPASS"} and None not in on["gate"], f"X-Hold-Gate 헤더가 모든 응답에 있어야 한다. {summary}"
    assert on["gate"].get("BYPASS", 0) == 0, f"Redis 정상인데 BYPASS 가 있다. {summary}"
    assert on["gate"].get("PASS", 0) <= 10, f"관문을 통과(DB 까지 감)한 요청은 ≤ 10건이어야 한다. {summary}"
    assert on["gate"].get("REJECT", 0) >= 185, f"REJECT ≥ 185 여야 한다. {summary}"
    assert off["tx_delta"] >= 150, f"off 에서 DB 트랜잭션 증가분이 ≥ 150 이어야 측정이 유효하다. {summary}"
    assert on["tx_delta"] <= 0.25 * off["tx_delta"], f"on 의 DB 트랜잭션 증가분이 off 의 25% 이하여야 한다. {summary}"
