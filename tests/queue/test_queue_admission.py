"""US-07 내 순서 확인과 입장권 (docs/stories/US-07-queue-admission.md, docs/queue.md). 시각은 파일 시계(get_now)."""
import json
import math
from collections import Counter
from datetime import timedelta

import psycopg
import pytest

from cache_support import CACHE_DSN, T0, ms, uid
from queue_support import (admitted_ticket, err, fire_urls, hold_with, join, join_many, me, me_request, read_int,
                           seats_with, tick)


def key(prefix, show, name):
    return f"{prefix}queue:{show}:{name}"


# ---- AC1: WAITING ----
def test_us07_ac1_waiting_shows_ahead_count_no_ticket_and_retry_after(server):
    a, b = uid("A"), uid("B")
    join(server, a)
    join(server, b)
    resp = me(server, a)
    assert resp.status_code == 200, f"{resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert body["status"] == "WAITING" and body["number"] == 1 and body["serving"] == 0 and body["ahead"] == 0
    assert body["ticket"] is None and body["retryAfterSeconds"] == 1
    assert resp.headers.get("Retry-After") == "1", resp.headers.get("Retry-After")
    second = me(server, b).json()
    assert second["ahead"] == 1 and second["status"] == "WAITING"


def test_us07_ac1_retry_after_follows_clamp_ceil_ahead_over_200_between_1_and_5(three_servers):
    srv = three_servers[0]
    show = srv.show_id()
    users, responses = join_many(three_servers, show, 1001)
    by_number = {x.json()["number"]: u for u, x in zip(users, responses)}
    for number, expected in ((1, 1), (201, 1), (202, 2), (402, 3), (1001, 5)):
        ahead = number - 1  # 전광판 0
        assert expected == min(5, max(1, math.ceil(ahead / 200))), (number, ahead)  # 공식 자체 확인
        resp = me(srv, by_number[number])
        assert resp.status_code == 200, resp.text[:200]
        assert resp.json()["ahead"] == ahead and resp.json()["status"] == "WAITING"
        assert resp.json()["retryAfterSeconds"] == expected, f"번호 {number}: {resp.json()['retryAfterSeconds']} != {expected}"
        assert resp.headers.get("Retry-After") == str(expected), f"번호 {number}: 헤더 {resp.headers.get('Retry-After')}"


# ---- AC2: 입장권 발급 ----
def test_us07_ac2_serving_reaching_my_number_admits_me_with_a_two_minute_ticket(server, r, prefix):
    show, a = server.show_id(), uid("A")
    join(server, a)
    tick(server)  # 1초 구간 이후
    resp = me(server, a)
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["status"] == "ADMITTED" and body["number"] == 1 and body["serving"] >= 1 and body["ahead"] == 0
    assert body["retryAfterSeconds"] == 0 and "Retry-After" not in resp.headers
    ticket = body["ticket"]
    assert ticket["token"] and ticket["expiresAt"]
    assert server.now + timedelta(minutes=2) == __import__("datetime").datetime.fromisoformat(ticket["expiresAt"]), ticket
    stored = json.loads(r.get(f"{prefix}ticket:{ticket['token']}"))
    assert stored == {"userId": a, "showId": show, "number": 1, "expiresAtMs": ms(server.now + timedelta(minutes=2))}, stored


def test_us07_ac2_a_turn_without_calling_me_issues_no_ticket_and_the_two_minutes_start_at_the_first_call(server, r, prefix):
    show, a = server.show_id(), uid("A")
    join(server, a)
    server.advance(minutes=10)  # 차례는 한참 전에 왔지만 /me 를 부르지 않았다
    assert json.loads(r.get(f"{prefix}queue:{show}:user:{a}"))["ticket"] is None
    body = me(server, a).json()
    assert body["status"] == "ADMITTED"
    assert __import__("datetime").datetime.fromisoformat(body["ticket"]["expiresAt"]) == server.now + timedelta(minutes=2)


# ---- AC3: 늘어나지 않음 ----
def test_us07_ac3_calling_me_or_using_the_ticket_again_never_extends_the_ticket(server):
    a = uid("A")
    join(server, a)
    tick(server)
    first = me(server, a).json()["ticket"]
    for _ in range(3):
        server.advance(seconds=20)
        again = me(server, a).json()
        assert again["status"] == "ADMITTED"
        assert again["ticket"] == first, "같은 token·같은 만료 시각이어야 한다(늘어나지 않음)"
        assert seats_with(server, a, first["token"]).status_code == 200
    assert me(server, a).json()["ticket"] == first


# ---- AC4: 전광판 ----
def test_us07_ac4_serving_rises_200_per_tick_at_most_even_with_concurrent_me_across_processes(three_servers, r, prefix):
    srv = three_servers[0]
    show = srv.show_id()
    users, responses = join_many(three_servers, show, 1001)
    by_number = {x.json()["number"]: u for u, x in zip(users, responses)}
    assert read_int(r, key(prefix, show, "serving")) in (0, None)
    srv.advance(milliseconds=999)  # 같은 구간 안: 올라가면 안 된다
    waves = fire_urls([me_request_for(three_servers[i % 3], by_number[n], show) for i, n in enumerate(range(1, 61))])
    assert {x.json()["serving"] for x in waves if not isinstance(x, Exception)} == {0}, "같은 구간에서 전광판이 올랐다"
    srv.advance(milliseconds=1)  # 1초 구간 경계
    waves = fire_urls([me_request_for(three_servers[i % 3], by_number[n], show) for i, n in enumerate(range(1, 61))])
    assert all(not isinstance(x, Exception) and x.status_code == 200 for x in waves), [x for x in waves if isinstance(x, Exception)][:2]
    assert {x.json()["serving"] for x in waves} == {200}, f"한 구간에 최대 200: {Counter(x.json()['serving'] for x in waves)}"
    assert read_int(r, key(prefix, show, "serving")) == 200, "여러 앱이 동시에 불러도 한 구간에 한 번만 올라야 한다"
    assert {x.json()["status"] for x in waves} == {"ADMITTED"} and all(x.json()["ticket"] for x in waves)
    srv.advance(seconds=1)  # 다음 구간: serving 200 → 400
    numbers = list(range(190, 250)) + [399, 400, 401, 402]  # 경계(399/400/401)를 반드시 포함
    waves = fire_urls([me_request_for(three_servers[i % 3], by_number[n], show) for i, n in enumerate(numbers)])
    assert all(not isinstance(x, Exception) and x.status_code == 200 for x in waves), [x for x in waves if isinstance(x, Exception)][:2]
    bodies = [x.json() for x in waves]
    assert {b["serving"] for b in bodies} == {400}, Counter(b["serving"] for b in bodies)
    assert_admission_matches_serving(bodies, 400, must_include=(199, 200, 201, 399, 400, 401))
    assert read_int(r, key(prefix, show, "serving")) == 400


def assert_admission_matches_serving(bodies, serving, must_include=()):
    """각 응답이 번호와 전광판에 맞는지 가른다: number ≤ serving 이면 ADMITTED + 입장권(token·expiresAt), 아니면
    WAITING + ahead = number − serving − 1 + 입장권 없음. must_include 번호는 반드시 응답에 있어야 한다(경계가 빠지면 실패)."""
    seen = {b["number"]: b for b in bodies}
    missing = [n for n in must_include if n not in seen]
    assert not missing, f"경계 번호가 검증 대상에 없다: {missing}"
    admitted = 0
    for number, b in seen.items():
        if number <= serving:
            assert b["status"] == "ADMITTED", f"번호 {number} ≤ serving {serving} 인데 {b['status']}"
            assert b["ticket"] and b["ticket"]["token"] and b["ticket"]["expiresAt"], f"번호 {number}: 입장권이 없다"
            admitted += 1
        else:
            assert b["status"] == "WAITING", f"번호 {number} > serving {serving} 인데 {b['status']}"
            assert b["ticket"] is None, f"번호 {number}: 대기 중인데 입장권이 있다"
            assert b["ahead"] == number - serving - 1, f"번호 {number}: ahead {b['ahead']} != {number - serving - 1}"
    assert admitted > 0, "입장한 응답이 하나도 없다(모두 WAITING 이면 이 단언은 실패해야 한다)"


def _fake(number, status, ahead=0, ticket=True):
    return {"number": number, "status": status, "ahead": ahead,
            "ticket": {"token": "t", "expiresAt": "x"} if ticket else None}


def test_us07_boundary_assertion_really_discriminates_fake_inputs():
    """위 단언이 '항상 참'이 아님을 가짜 입력으로 확인한다(이 테스트는 서버 없이 실행)."""
    good = [_fake(399, "ADMITTED"), _fake(400, "ADMITTED"), _fake(401, "WAITING", 0, ticket=False)]
    assert_admission_matches_serving(good, 400, must_include=(399, 400, 401))  # 정상은 통과
    all_waiting = [_fake(399, "WAITING", -1, ticket=False), _fake(400, "WAITING", -1, ticket=False),
                   _fake(401, "WAITING", 0, ticket=False)]
    off_by_one_late = [_fake(399, "ADMITTED"), _fake(400, "WAITING", -1, ticket=False), _fake(401, "WAITING", 0, ticket=False)]
    off_by_one_early = [_fake(399, "ADMITTED"), _fake(400, "ADMITTED"), _fake(401, "ADMITTED")]
    no_ticket = [_fake(400, "ADMITTED", ticket=False), _fake(401, "WAITING", 0, ticket=False)]
    waiting_with_ticket = [_fake(400, "ADMITTED"), _fake(401, "WAITING", 0, ticket=True)]
    wrong_ahead = [_fake(400, "ADMITTED"), _fake(401, "WAITING", 5, ticket=False)]
    for name, fake in (("모두 WAITING", all_waiting), ("400 이 WAITING(경계 -1)", off_by_one_late),
                       ("401 이 ADMITTED(경계 +1)", off_by_one_early), ("ADMITTED 인데 입장권 없음", no_ticket),
                       ("WAITING 인데 입장권 있음", waiting_with_ticket), ("ahead 틀림", wrong_ahead)):
        try:
            assert_admission_matches_serving(fake, 400, must_include=(400, 401))
        except AssertionError:
            continue  # 가짜 입력을 올바르게 거절했다
        pytest.fail(f"가짜 입력({name})이 통과했다 — 단언이 항상 참이다")  # pytest.fail 은 AssertionError 가 아니라서 위에서 삼켜지지 않는다
    with pytest.raises(AssertionError):  # 경계 번호가 응답에 없으면 실패
        assert_admission_matches_serving([_fake(190, "ADMITTED")], 400, must_include=(399, 400, 401))


def me_request_for(srv, user, show):
    return me_request(srv, user, show)


def test_us07_ac4_serving_never_passes_the_last_issued_number(server, r, prefix):
    show = server.show_id()
    users = [uid("u") for _ in range(5)]
    for u in users:
        join(server, u)
    server.advance(hours=1)  # 한참 아무도 안 부르다 몰렸다: 지난 구간이 한꺼번에 쌓이면 안 된다
    bodies = [me(server, u).json() for u in users]
    assert {b["serving"] for b in bodies} == {5}, [b["serving"] for b in bodies]
    assert [b["status"] for b in bodies] == ["ADMITTED"] * 5
    late = join(server, uid("late"))
    assert late.status_code == 201 and late.json()["number"] == 6
    assert late.json()["status"] == "WAITING" and late.json()["serving"] == 5, "새로 온 사람은 바로 입장하지 못한다(다음 구간)"
    assert read_int(r, key(prefix, show, "serving")) == 5
    tick(server)
    assert me(server, users[0]).json()["serving"] == 6


def test_us07_ac4_joining_advances_the_scoreboard_before_issuing_the_new_number(server):
    a, b = uid("A"), uid("B")
    assert join(server, a).json()["number"] == 1  # 마지막 처리 구간이 정해진다
    tick(server)
    second = join(server, b)  # 줄 서기도 전광판을 먼저 올린다: serving = min(0+200, 발급된 마지막 번호 1)
    assert second.json()["number"] == 2
    assert second.json()["serving"] == 1, second.json()
    assert second.json()["ahead"] == 0 and second.json()["status"] == "WAITING"


# ---- AC5: 만료 ----
def test_us07_ac5_ticket_works_at_plus_1m59s_and_is_403_expired_from_plus_2m00s_for_seatmap_and_hold(server):
    a = uid("A")
    show, seat = server.show_id(), server.seat_ids(1)[0]
    join(server, a)
    tick(server)
    token = admitted_ticket(server, a)  # 발급 시각 = T0+1s → 만료 T0+1s+2:00
    issued = server.now
    server.set_time(issued + timedelta(minutes=1, seconds=59))
    assert seats_with(server, a, token).status_code == 200
    server.set_time(issued + timedelta(minutes=2))  # now == expiresAt
    err(seats_with(server, a, token), 403, "QUEUE_TICKET_EXPIRED")
    err(hold_with(server, seat, a, token), 403, "QUEUE_TICKET_EXPIRED")
    server.set_time(issued + timedelta(minutes=2, seconds=1))
    err(seats_with(server, a, token), 403, "QUEUE_TICKET_EXPIRED")
    state = me(server, a).json()
    assert state["status"] == "EXPIRED", state


def test_us07_ac5_repeated_me_calls_do_not_push_the_expiry_past_two_minutes(server):
    a = uid("A")
    join(server, a)
    tick(server)
    token = admitted_ticket(server, a)
    issued = server.now
    for offset in (30, 60, 90, 110):
        server.set_time(issued + timedelta(seconds=offset))
        assert me(server, a).json()["ticket"]["token"] == token
    server.set_time(issued + timedelta(minutes=2))
    err(seats_with(server, a, token), 403, "QUEUE_TICKET_EXPIRED")


# ---- AC6: 거절 (남의 입장권·다른 공연·없음) ----
def make_other_show(dsn=CACHE_DSN):
    """테스트 DB 에 공연 하나와 좌석 하나를 더 넣는다."""
    with psycopg.connect(dsn, autocommit=True) as conn:
        show = conn.execute("INSERT INTO shows (title, venue, starts_at) VALUES ('다른 공연', '다른 장소', now()) RETURNING id").fetchone()[0]
        seat = conn.execute("INSERT INTO seats (show_id, section, row_label, number) VALUES (%s, 'Z', '1', 1) RETURNING id", (show,)).fetchone()[0]
    return show, seat


def admitted(server, user, show=None):
    join(server, user, show)
    tick(server)
    return admitted_ticket(server, user, show)


def test_us07_ac6_ticket_of_another_user_is_403_invalid_and_nothing_changes(server, r, prefix):
    show, seat = server.show_id(), server.seat_ids(1)[0]
    a, b = uid("A"), uid("B")
    token = admitted(server, a)
    err(seats_with(server, b, token), 403, "QUEUE_TICKET_INVALID")
    err(hold_with(server, seat, b, token), 403, "QUEUE_TICKET_INVALID")
    assert r.exists(f"{prefix}seatmap:{show}") == 0, "거절이 좌석 캐시를 채웠다(입장권 검증이 캐시보다 먼저여야 한다)"
    assert r.exists(f"{prefix}hold:{seat}") == 0, "거절이 홀드 관문 표시를 남겼다(입장권 검증이 관문보다 먼저여야 한다)"
    from cache_support import db_seat
    assert db_seat(seat)[0] == "AVAILABLE"


def test_us07_ac6_ticket_of_another_show_is_403_invalid(server, r, prefix):
    show_x, (show_y, seat_y) = server.show_id(), make_other_show()
    a = uid("A")
    token = admitted(server, a, show_x)
    err(seats_with(server, a, token, show_y), 403, "QUEUE_TICKET_INVALID")
    err(hold_with(server, seat_y, a, token), 403, "QUEUE_TICKET_INVALID")
    assert r.exists(f"{prefix}seatmap:{show_y}") == 0 and r.exists(f"{prefix}hold:{seat_y}") == 0
    from cache_support import db_seat
    assert db_seat(seat_y)[0] == "AVAILABLE"
    assert seats_with(server, a, token, show_x).status_code == 200  # 자기 공연에서는 통한다
    token_y = admitted(server, a, show_y)  # 공연 Y 도 줄을 서면 Y 의 입장권이 생긴다
    assert seats_with(server, a, token_y, show_y).status_code == 200
    err(seats_with(server, a, token_y, show_x), 403, "QUEUE_TICKET_INVALID")


def test_us07_ac6_missing_or_unknown_ticket_is_required_or_invalid(server):
    show, seat = server.show_id(), server.seat_ids(1)[0]
    a = uid("A")
    err(seats_with(server, a, None), 403, "QUEUE_TICKET_REQUIRED")
    err(hold_with(server, seat, a, None), 403, "QUEUE_TICKET_REQUIRED")
    err(seats_with(server, a, "no-such-token"), 403, "QUEUE_TICKET_INVALID")
    err(hold_with(server, seat, a, "no-such-token"), 403, "QUEUE_TICKET_INVALID")


def test_us07_ac6_unknown_show_ends_in_403_at_the_ticket_check(server):
    a = uid("A")
    token = admitted(server, a)
    err(seats_with(server, a, None, "no-such-show"), 403, "QUEUE_TICKET_REQUIRED")
    err(seats_with(server, a, token, "no-such-show"), 403, "QUEUE_TICKET_INVALID")


def test_us07_ac6_check_order_401_then_input_validation_then_unknown_seat_then_ticket(server):
    show, seat = server.show_id(), server.seat_ids(1)[0]
    a = uid("A")
    err(seats_with(server, None, None), 401, "UNAUTHENTICATED")  # 좌석맵: 401 이 입장권보다 먼저
    err(hold_with(server, seat, None, None), 401, "UNAUTHENTICATED")
    err(hold_with(server, "no-such-seat", a, None), 404, "NOT_FOUND")  # 홀드: 없는 좌석은 입장권보다 먼저
    err(server.client.post("/seats/%00/hold", headers={"X-User-Id": a}), 404, "NOT_FOUND")  # 입력 검증이 먼저
    err(hold_with(server, seat, a, None), 403, "QUEUE_TICKET_REQUIRED")


def test_us07_ac6_a_valid_ticket_lets_hold_work_and_other_seatmap_state_is_correct(server):
    a, seat = uid("A"), server.seat_ids(1)[0]
    token = admitted(server, a)
    held = hold_with(server, seat, a, token)
    assert held.status_code == 201, held.text[:200]
    assert {s["id"]: s["status"] for s in seats_with(server, a, token).json()}[seat] == "HELD"


# ---- AC7: 다른 앱이 검증해도 통한다 ----
def test_us07_ac7_a_ticket_from_one_process_works_in_the_others(three_servers):
    s1, s2, s3 = three_servers
    show, seat = s1.show_id(), s1.seat_ids(1)[0]
    a = uid("A")
    join(s1, a)
    tick(s1)
    token = admitted_ticket(s2, a)  # 입장권을 s2 가 발급
    assert seats_with(s3, a, token).status_code == 200  # s3 가 검증
    assert seats_with(s1, a, token).status_code == 200
    held = hold_with(s3, seat, a, token)
    assert held.status_code == 201, held.text[:200]
    err(seats_with(s1, uid("B"), token), 403, "QUEUE_TICKET_INVALID")  # 다른 앱에서도 남의 것은 거절


# ---- AC8: 줄 선 적 없음·Redis 장애 ----
def test_us07_ac8_me_without_joining_is_404_entry_not_found_and_without_user_is_401(server):
    err(me(server, uid("A")), 404, "QUEUE_ENTRY_NOT_FOUND")
    err(me(server, None), 401, "UNAUTHENTICATED")


@pytest.mark.parametrize("redis_url", ["redis://127.0.0.1:1/0", None], ids=["unreachable_redis", "no_redis_url"])
def test_us07_ac8_without_usable_redis_everything_that_needs_a_queue_or_ticket_is_503(make_server, redis_url):
    srv = make_server(redis_url=redis_url, timeout_ms=200)
    show, seat, a = srv.show_id(), srv.seat_ids(1)[0], uid("A")
    for resp in (me(srv, a), seats_with(srv, a, "any-token"), hold_with(srv, seat, a, "any-token")):
        err(resp, 503, "QUEUE_UNAVAILABLE")
        assert resp.headers.get("Retry-After") == "5", resp.headers.get("Retry-After")
    # 입장권 헤더가 없어도 입장하면 안 된다(403 또는 503)
    for resp in (seats_with(srv, a, None), hold_with(srv, seat, a, None)):
        assert resp.status_code in (403, 503), f"입장권 없이 들어갔다: {resp.status_code}"
    assert srv.client.get("/shows").status_code == 200  # 공연 목록은 입장권이 필요 없다


def _mut(**changes):
    def apply(stored):
        d = dict(stored)
        for k, v in changes.items():
            if v is _DELETE:
                d.pop(k, None)
            else:
                d[k] = v
        return json.dumps(d)
    return apply


_DELETE = object()
TICKET_CORRUPT = [
    ("invalid_utf8", lambda d: b"\xff\xfe"), ("not_json", lambda d: "not json"), ("json_array", lambda d: "[1]"),
    ("json_scalar", lambda d: "5"), ("empty_object", lambda d: "{}"),
    ("user_id_number", _mut(userId=1)), ("user_id_null", _mut(userId=None)), ("user_id_missing", _mut(userId=_DELETE)),
    ("show_id_number", _mut(showId=1)), ("show_id_null", _mut(showId=None)), ("show_id_missing", _mut(showId=_DELETE)),
    ("number_missing", _mut(number=_DELETE)), ("number_null", _mut(number=None)), ("number_zero", _mut(number=0)),
    ("number_negative", _mut(number=-1)), ("number_fraction", _mut(number=1.5)), ("number_true", _mut(number=True)),
    ("number_string", _mut(number="1")),
    ("expires_missing", _mut(expiresAtMs=_DELETE)), ("expires_string", _mut(expiresAtMs="soon")),
    ("expires_fraction", _mut(expiresAtMs=1.5)), ("expires_true", _mut(expiresAtMs=True)), ("expires_null", _mut(expiresAtMs=None)),
]


@pytest.mark.parametrize("name,mutate", TICKET_CORRUPT, ids=[c[0] for c in TICKET_CORRUPT])
def test_us07_corrupt_ticket_value_is_403_invalid_never_5xx_for_seatmap_and_hold(server, r, prefix, name, mutate):
    a, seat = uid("A"), server.seat_ids(1)[0]
    token = admitted(server, a)
    stored = json.loads(r.get(f"{prefix}ticket:{token}"))
    r.set(f"{prefix}ticket:{token}", mutate(stored))
    err(seats_with(server, a, token), 403, "QUEUE_TICKET_INVALID")
    err(hold_with(server, seat, a, token), 403, "QUEUE_TICKET_INVALID")
    from cache_support import db_seat
    assert db_seat(seat)[0] == "AVAILABLE" and r.exists(f"{prefix}hold:{seat}") == 0, "손상된 입장권 거절이 상태를 바꿨다"
