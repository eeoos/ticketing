"""Redis 를 멈춘 상태 (docs/queue.md 'Redis 장애', docs/cache.md). 복구 실패는 teardown error.

9장 정책: 좌석 캐시·홀드 관문은 DB 로 우회(BYPASS)하지만, 대기열·입장권 검증은 우회하지 않는다.
→ Redis 가 꺼지면 좌석맵·홀드는 503 QUEUE_UNAVAILABLE(+Retry-After: 5). 결제 요청·콜백·예매 조회·공연 목록은 입장권이 필요 없으므로
DB 로 계속 동작한다. (이전 정책의 'Redis 를 멈춰도 tests/concurrency C-01~C-07 통과'는 이 정책과 충돌해 재정의했다:
C-01~C-07 의 홀드는 이제 입장권=Redis 가 필요하다.)
"""
import time

from cache_support import REDIS_HOST_URL, TEST_REDIS_DB, uid


def err(resp, status, code):
    assert resp.status_code == status, f"기대 {status}, 실제 {resp.status_code}: {resp.text[:300]}"
    assert resp.json().get("code") == code, resp.text


def test_cache_redis_stopped_seatmap_and_hold_are_503_but_payment_and_callback_keep_working(redis_down, make_server):
    srv = make_server(redis_url=f"{REDIS_HOST_URL}/{TEST_REDIS_DB}", timeout_ms=200)
    show, (s1, s2), user = srv.show_id(), srv.seat_ids(2), uid("A")
    assert srv.hold(s1, user).status_code == 201  # Redis 가 살아 있을 때 홀드·결제 요청까지
    pay = srv.pay(s1, user)
    assert pay.status_code == 202, pay.text
    ticket = srv.grant_ticket(uid("B"), show)  # 입장권도 미리 발급(꺼진 뒤에는 검증할 수 없다)
    redis_down.stop()
    started = time.monotonic()
    seatmap = srv.seatmap(show, user=uid("B"), ticket=ticket)
    err(seatmap, 503, "QUEUE_UNAVAILABLE")
    assert seatmap.headers.get("Retry-After") == "5", seatmap.headers.get("Retry-After")
    hold = srv.hold(s2, uid("C"), ticket=ticket)
    err(hold, 503, "QUEUE_UNAVAILABLE")
    assert hold.headers.get("Retry-After") == "5"
    assert srv.client.get("/shows").status_code == 200  # 공연 목록은 입장권이 필요 없다
    cb = srv.callback(pay.json()["paymentId"], "SUCCEEDED")  # 결제 콜백은 DB 만으로
    assert cb.status_code == 200 and cb.json()["booking"]["status"] == "CONFIRMED", cb.text
    again = srv.pay(s1, user)  # 이미 확정된 본인 예매 → 200
    assert again.status_code == 200 and again.json()["status"] == "CONFIRMED"
    assert srv.client.get(f"/bookings/{pay.json()['id']}", headers={"X-User-Id": user}).status_code == 200
    from cache_support import db_seat
    assert db_seat(s1)[0] == "SOLD" and db_seat(s2)[0] == "AVAILABLE", "503 이 DB 상태를 바꾸면 안 된다"
    assert time.monotonic() - started < 10, "Redis 가 꺼져 있어도 시간 제한 안에 응답해야 한다"


def test_cache_redis_stopped_without_any_ticket_header_is_still_not_admitted(redis_down, make_server):
    srv = make_server(redis_url=f"{REDIS_HOST_URL}/{TEST_REDIS_DB}", timeout_ms=200)
    show, seat = srv.show_id(), srv.seat_ids(1)[0]
    redis_down.stop()
    for resp in (srv.seatmap(show, user=uid("A"), ticket=None), srv.hold(seat, uid("A"), ticket=None)):
        assert resp.status_code in (403, 503), f"입장권 없이 들어가면 안 된다: {resp.status_code} {resp.text[:200]}"
