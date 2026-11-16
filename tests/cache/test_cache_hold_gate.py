"""홀드 관문 (docs/cache.md ②). 시각은 파일 시계(get_now), 관문 표시는 Redis 에서 직접 읽어 확인한다."""
from datetime import timedelta

from cache_support import T0, ms, read_value, uid


def gate(resp):
    return resp.headers.get("X-Hold-Gate")


def marker_key(srv, seat):
    return f"{srv.prefix}hold:{seat}"


def err(resp, status, code):
    assert resp.status_code == status, f"기대 {status}, 실제 {resp.status_code}: {resp.text}"
    assert resp.json().get("code") == code, resp.text


# ---- PASS / REJECT ----
def test_cache_gate_first_hold_passes_and_marks_held_with_db_expiry(server, r):
    seat, user = server.seat_ids(1)[0], uid("A")
    resp = server.hold(seat, user)
    assert resp.status_code == 201, resp.text
    assert gate(resp) == "PASS", "DB 까지 간 요청은 PASS"
    marker = read_value(r, marker_key(server, seat))
    assert marker is not None, "홀드 성공 뒤 관문 표시(HELD)가 Redis 에 있어야 한다"
    assert marker["state"] == "HELD" and marker["owner"] == user and marker.get("token")
    assert int(marker["expiresAtMs"]) == ms(T0 + timedelta(minutes=5)), "표시의 만료는 DB hold_expires_at(get_now+5분)"
    assert r.pttl(marker_key(server, seat)) > 0


def test_cache_gate_rejects_held_seat_without_touching_db_including_same_user(server, r):
    seat, a, b = server.seat_ids(1)[0], uid("A"), uid("B")
    assert server.hold(seat, a).status_code == 201
    before = read_value(r, marker_key(server, seat))
    other = server.hold(seat, b)
    err(other, 409, "SEAT_HELD")
    assert gate(other) == "REJECT"
    same = server.hold(seat, a)
    err(same, 409, "SEAT_HELD")
    assert gate(same) == "REJECT", "본인 재홀드도 관문에서 거절(연장 없음)"
    assert read_value(r, marker_key(server, seat)) == before, "REJECT 는 남의 표시를 건드리지 않는다"


def test_cache_gate_does_not_block_new_hold_after_db_expiry(server, r):
    seat, a, b = server.seat_ids(1)[0], uid("A"), uid("B")
    first = server.hold(seat, a)
    assert first.status_code == 201
    server.set_time(T0 + timedelta(minutes=4, seconds=59))
    early = server.hold(seat, b)
    err(early, 409, "SEAT_HELD")
    assert gate(early) == "REJECT"
    server.set_time(T0 + timedelta(minutes=5))  # now == expiresAt: 표시는 무시된다
    late = server.hold(seat, b)
    assert late.status_code == 201, late.text
    assert gate(late) == "PASS"
    assert late.json()["holdId"] != first.json()["holdId"]
    marker = read_value(r, marker_key(server, seat))
    assert marker["owner"] == b and int(marker["expiresAtMs"]) == ms(T0 + timedelta(minutes=10))


def test_cache_gate_rejects_sold_seat_with_seat_sold_and_marks_sold_for_60_seconds(server, r):
    seat = server.seat_ids(1)[0]
    server.sell(seat, uid("A"))
    marker = read_value(r, marker_key(server, seat))
    assert marker is not None and marker["state"] == "SOLD", marker
    assert 50_000 <= r.pttl(marker_key(server, seat)) <= 60_000, r.pttl(marker_key(server, seat))
    resp = server.hold(seat, uid("B"))
    err(resp, 409, "SEAT_SOLD")
    assert gate(resp) == "REJECT"
    server.advance(minutes=10)  # SOLD 는 만료되지 않는다
    err(server.hold(seat, uid("C")), 409, "SEAT_SOLD")


def test_cache_gate_db_decides_when_the_redis_mark_is_lost(server, r):
    """Redis 데이터가 사라져도(표시 삭제) 정확성은 DB: 관문은 PASS, DB 가 409 를 낸다."""
    held, sold = server.seat_ids(2)
    assert server.hold(held, uid("A")).status_code == 201
    server.sell(sold, uid("S"))
    r.delete(marker_key(server, held), marker_key(server, sold))
    resp = server.hold(held, uid("B"))
    err(resp, 409, "SEAT_HELD")
    assert gate(resp) == "PASS", "표시가 없으니 관문은 통과시키고 DB 가 판단"
    resp = server.hold(sold, uid("B"))
    err(resp, 409, "SEAT_SOLD")
    assert gate(resp) == "PASS"


# ---- DB 실패 시 내 표시만 되돌림 ----
def test_cache_gate_unknown_seat_is_404_before_the_gate_so_no_header_and_no_mark(server, r):
    """9장 검사 순서(docs/queue.md): 401 → 입력 검증 → 좌석의 공연 찾기(없는 좌석 404) → 입장권 → 관문 → DB.
    없는 좌석은 관문에 닿지 않는다: 404 NOT_FOUND·X-Hold-Gate 헤더 없음·그 좌석의 관문 표시 키가 생기지 않는다(두 번 불러도 같다).
    ('DB 가 실패한 뒤 내 표시만 되돌림'은 아래 test_cache_gate_db_conflict_after_pass_releases_pending_mark 가 계속 검증한다.)"""
    key = marker_key(server, "no-such-seat")
    for _ in range(2):  # 표시가 남았다면 두 번째 요청이 409 SEAT_HELD 를 받는다
        resp = server.hold("no-such-seat", uid("A"))
        err(resp, 404, "NOT_FOUND")
        assert gate(resp) is None, f"없는 좌석은 관문에 닿지 않으므로 X-Hold-Gate 헤더가 없어야 한다: {gate(resp)}"
        assert r.exists(key) == 0, "없는 좌석에 관문 표시 키가 생겼다"


def test_cache_gate_db_conflict_after_pass_releases_pending_mark(server, r):
    seat = server.seat_ids(1)[0]
    assert server.hold(seat, uid("A")).status_code == 201
    r.delete(marker_key(server, seat))  # Redis 만 비움(DB 는 HELD)
    resp = server.hold(seat, uid("B"))
    err(resp, 409, "SEAT_HELD")
    assert gate(resp) == "PASS"
    assert r.exists(marker_key(server, seat)) == 0, "DB 가 409 를 냈으니 내 PENDING 표시를 되돌려야 한다(2초 막힘 방지)"
    again = server.hold(seat, uid("C"))
    err(again, 409, "SEAT_HELD")
    assert gate(again) == "PASS", "표시가 남아 있지 않으니 다시 DB 까지 간다"


# ---- Redis 를 쓸 수 없을 때 (9장 정책): 좌석맵·홀드는 503, DB 만 쓰는 엔드포인트는 계속 ----
def test_cache_unreachable_redis_seatmap_and_hold_are_503_and_db_endpoints_keep_working(make_server):
    srv = make_server(redis_url="redis://127.0.0.1:1/0", timeout_ms=200)  # 아무도 듣지 않는 포트
    show, seat, user = srv.show_id(), srv.seat_ids(1)[0], uid("A")
    for resp in (srv.seatmap(show, user=user, ticket="any-token"), srv.hold(seat, user, ticket="any-token")):
        err(resp, 503, "QUEUE_UNAVAILABLE")
        assert resp.headers.get("Retry-After") == "5"
    assert srv.client.get("/shows").status_code == 200
    err(srv.pay(seat, user), 409, "NO_HOLD")  # 결제 요청은 DB 만으로 판단
    err(srv.callback("no-such-payment", "SUCCEEDED"), 404, "NOT_FOUND")
