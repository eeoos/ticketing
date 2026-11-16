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
def test_cache_gate_db_failure_releases_my_mark(server, r):
    for _ in range(2):  # 표시가 남았다면 두 번째 요청이 409 SEAT_HELD 를 받는다
        resp = server.hold("no-such-seat", uid("A"))
        err(resp, 404, "NOT_FOUND")
        assert gate(resp) == "PASS", "관문은 통과(표시 후 DB 에서 404)"
    assert r.exists(marker_key(server, "no-such-seat")) == 0, "DB 실패 뒤 내 PENDING 표시는 지워진다"


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


# ---- Redis 오류는 5xx 로 새지 않는다 ----
def test_cache_unreachable_redis_requests_bypass_without_5xx(make_server):
    srv = make_server(redis_url="redis://127.0.0.1:1/0", timeout_ms=200)  # 아무도 듣지 않는 포트
    show, seat, user = srv.show_id(), srv.seat_ids(1)[0], uid("A")
    for _ in range(2):
        resp = srv.seatmap(show)
        assert resp.status_code == 200 and resp.headers.get("X-Cache") == "BYPASS", (resp.status_code, resp.headers.get("X-Cache"))
    ok = srv.hold(seat, user)
    assert ok.status_code == 201, ok.text
    assert gate(ok) == "BYPASS"
    dup = srv.hold(seat, uid("B"))
    err(dup, 409, "SEAT_HELD")
    assert gate(dup) == "BYPASS", "관문 없이 DB 가 판단"
    pay = srv.pay(seat, user)
    assert pay.status_code == 202, pay.text
    cb = srv.callback(pay.json()["paymentId"], "SUCCEEDED")
    assert cb.status_code == 200 and cb.json()["booking"]["status"] == "CONFIRMED", cb.text
    assert srv.statuses(srv.seatmap(show))[seat] == "SOLD"
