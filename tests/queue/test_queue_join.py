"""US-06 대기열 번호표 (docs/stories/US-06-queue-number.md, docs/queue.md). 시각은 파일 시계(get_now), 대기열은 실제 Redis."""
import json
from collections import Counter

import pytest

from cache_support import Server, delete_prefix, new_prefix, reset_database, uid, REDIS_HOST_URL, TEST_REDIS_DB, redis_client
from queue_support import err, fire_urls, join, join_many, join_request, me, read_int, tick


def keys(prefix, show):
    return {"seq": f"{prefix}queue:{show}:seq", "serving": f"{prefix}queue:{show}:serving", "tick": f"{prefix}queue:{show}:tick"}


# ---- AC1 ----
def test_us06_ac1_first_join_is_201_with_number_1_and_status_fields(server, r, prefix):
    show, user = server.show_id(), uid("A")
    resp = join(server, user)
    assert resp.status_code == 201, f"{resp.status_code} {resp.text[:300]}"
    body = resp.json()
    assert set(body) >= {"showId", "number", "serving", "ahead", "status", "retryAfterSeconds", "ticket"}, sorted(body)
    assert body["showId"] == show and body["number"] == 1
    assert body["serving"] == 0 and body["ahead"] == 0
    assert body["status"] == "WAITING" and body["ticket"] is None
    assert body["retryAfterSeconds"] == 1
    assert read_int(r, keys(prefix, show)["seq"]) == 1
    entry = json.loads(r.get(f"{prefix}queue:{show}:user:{user}"))
    assert entry == {"number": 1, "ticket": None, "ticketExpiresAtMs": None}, entry


def test_us06_ac1_second_user_gets_number_2_with_one_person_ahead(server):
    join(server, uid("A"))
    body = join(server, uid("B")).json()
    assert body["number"] == 2 and body["ahead"] == 1 and body["serving"] == 0


# ---- AC2 ----
def test_us06_ac2_same_user_calling_again_gets_200_and_the_same_number(server, r, prefix):
    show, user = server.show_id(), uid("A")
    first = join(server, user)
    assert first.status_code == 201
    for _ in range(3):
        again = join(server, user)
        assert again.status_code == 200, f"{again.status_code} {again.text[:200]}"
        assert again.json()["number"] == first.json()["number"] == 1
        assert again.json()["status"] == "WAITING"
    assert read_int(r, keys(prefix, show)["seq"]) == 1, "번호는 새로 발급되지 않는다"


def test_us06_ac2_two_tabs_same_user_concurrently_across_processes_get_one_number(three_servers, r, prefix):
    show, user = three_servers[0].show_id(), uid("A")
    responses = fire_urls([join_request(three_servers[i % 3], user, show) for i in range(30)])
    codes = Counter(x.status_code for x in responses)
    assert codes == {201: 1, 200: 29}, f"처음 201 한 번, 나머지 200: {dict(codes)}"
    assert {x.json()["number"] for x in responses} == {1}
    assert read_int(r, keys(prefix, show)["seq"]) == 1, "같은 사용자가 번호를 두 번 이상 받았다"


# ---- AC3 ----
def test_us06_ac3_1000_users_across_three_processes_get_numbers_1_to_1000_exactly_once(three_servers, r, prefix):
    show = three_servers[0].show_id()
    users, responses = join_many(three_servers, show, 1000)
    assert Counter(x.status_code for x in responses) == {201: 1000}, Counter(x.status_code for x in responses)
    numbers = sorted(x.json()["number"] for x in responses)
    assert numbers == list(range(1, 1001)), f"겹침·빠짐: 중복 {[n for n, c in Counter(numbers).items() if c > 1][:5]} / 최대 {numbers[-1]}"
    assert read_int(r, keys(prefix, show)["seq"]) == 1000
    stored = [json.loads(r.get(f"{prefix}queue:{show}:user:{u}"))["number"] for u in users]
    assert sorted(stored) == list(range(1, 1001)), "발급한 번호는 모두 사용자에게 저장돼야 한다(낭비 없음)"


def test_us06_ac3_1000_users_against_a_real_multi_worker_server_get_numbers_1_to_1000_exactly_once():
    """실제 uvicorn 워커 4 + Redis(실제 시계). 번호는 1~1000 이 정확히 한 번씩."""
    prefix = new_prefix()
    reset_database()
    srv = Server(prefix, gate="off", workers=4).start()
    try:
        show = srv.client.get("/shows").json()[0]["id"]
        users = [uid("u") for _ in range(1000)]
        responses = fire_urls([dict(method="POST", url=f"{srv.url}/shows/{show}/queue", headers={"X-User-Id": u}) for u in users])
        bad = [x for x in responses if isinstance(x, Exception)]
        assert not bad, bad[:2]
        assert Counter(x.status_code for x in responses) == {201: 1000}, Counter(x.status_code for x in responses)
        numbers = sorted(x.json()["number"] for x in responses)
        assert numbers == list(range(1, 1001)), f"겹침·빠짐 (최대 {numbers[-1]})"
        assert read_int(redis_client(TEST_REDIS_DB), keys(prefix, show)["seq"]) == 1000
    finally:
        srv.stop()
        delete_prefix(prefix)


# ---- AC4 ----
def test_us06_ac4_after_the_ticket_expires_joining_again_gets_a_new_number_at_the_back(server, r, prefix):
    show, a, b = server.show_id(), uid("A"), uid("B")
    assert join(server, a).json()["number"] == 1
    assert join(server, b).json()["number"] == 2
    tick(server)  # 전광판 1초 구간 → 2명 입장
    ticket = me(server, a).json()["ticket"]
    assert ticket is not None
    server.advance(minutes=2)  # 발급 시각 + 2:00 → 입장권 만료
    assert me(server, a).json()["status"] == "EXPIRED"
    again = join(server, a)
    assert again.status_code == 201, f"{again.status_code} {again.text[:200]}"
    assert again.json()["number"] == 3, "만료 뒤 다시 줄 서면 맨 뒤 새 번호"
    assert again.json()["status"] == "WAITING" and again.json()["ticket"] is None
    assert join(server, b).json()["number"] == 2, "다른 사용자의 번호는 그대로"
    assert read_int(r, keys(prefix, show)["seq"]) == 3
    assert me(server, a).json()["number"] == 3


def test_us06_ac4_joining_before_expiry_keeps_the_number_and_never_issues_a_new_one(server):
    a = uid("A")
    assert join(server, a).json()["number"] == 1
    tick(server)
    assert me(server, a).json()["status"] == "ADMITTED"
    server.advance(minutes=1, seconds=59)  # 만료 1초 전
    again = join(server, a)
    assert again.status_code == 200 and again.json()["number"] == 1 and again.json()["status"] == "ADMITTED"


# ---- AC5 ----
def test_us06_ac5_missing_user_header_is_401_and_unknown_show_is_404(server):
    show = server.show_id()
    err(join(server, None, show), 401, "UNAUTHENTICATED")
    err(join(server, uid("A"), "no-such-show"), 404, "NOT_FOUND")


@pytest.mark.parametrize("redis_url", ["redis://127.0.0.1:1/0", None], ids=["unreachable_redis", "no_redis_url"])
def test_us06_ac5_without_usable_redis_join_is_503_queue_unavailable_with_retry_after(make_server, redis_url):
    srv = make_server(redis_url=redis_url, timeout_ms=200)
    resp = join(srv, uid("A"))
    err(resp, 503, "QUEUE_UNAVAILABLE")
    assert resp.headers.get("Retry-After") == "5", resp.headers.get("Retry-After")


# ---- 손상된 대기열 값 (docs/queue.md 'Redis 장애': 손상 = UTF-8 아님·JSON 아님·객체 아님·필드 형식 불일치) ----
BASE = {"number": 1, "ticket": None, "ticketExpiresAtMs": None}


def _entry(**changes):
    d = dict(BASE)
    d.update(changes)
    return d


EXPIRES_OK = 1893499200000  # 정수(2030)
USER_ENTRY_CORRUPT = [
    ("invalid_utf8", b"\xff\xfe"), ("not_json", "not json"), ("json_array", "[1,2]"), ("json_scalar", "5"),
    ("json_string", '"x"'), ("empty_object", "{}"),
    ("number_missing", {"ticket": None, "ticketExpiresAtMs": None}), ("number_null", _entry(number=None)),
    ("number_zero", _entry(number=0)), ("number_negative_one", _entry(number=-1)), ("number_negative_five", _entry(number=-5)),
    ("number_fraction", _entry(number=1.5)), ("number_true", _entry(number=True)), ("number_string", _entry(number="1")),
    ("ticket_number", _entry(ticket=123, ticketExpiresAtMs=EXPIRES_OK)), ("ticket_list", _entry(ticket=[], ticketExpiresAtMs=EXPIRES_OK)),
    ("ticket_object", _entry(ticket={}, ticketExpiresAtMs=EXPIRES_OK)),
    ("expires_string_with_null_ticket", _entry(ticketExpiresAtMs="soon")),  # review 재현: 500
    ("expires_string_with_ticket", _entry(ticket="tok", ticketExpiresAtMs="soon")),
    ("expires_fraction", _entry(ticket="tok", ticketExpiresAtMs=1.5)), ("expires_true", _entry(ticket="tok", ticketExpiresAtMs=True)),
    ("expires_list", _entry(ticket="tok", ticketExpiresAtMs=[1])),
    ("only_ticket_is_null", _entry(ticket=None, ticketExpiresAtMs=EXPIRES_OK)),
    ("only_expires_is_null", _entry(ticket="tok", ticketExpiresAtMs=None)),
]


def _raw(value):
    return value if isinstance(value, (bytes, str)) else json.dumps(value)


@pytest.mark.parametrize("name,value", USER_ENTRY_CORRUPT, ids=[c[0] for c in USER_ENTRY_CORRUPT])
def test_us06_corrupt_user_entry_is_treated_as_missing_never_5xx_and_never_admits(server, r, prefix, name, value):
    show, user = server.show_id(), uid("A")
    assert join(server, user).json()["number"] == 1
    r.set(f"{prefix}queue:{show}:user:{user}", _raw(value))
    server.advance(hours=1)  # 손상된 항목의 번호가 serving 이하로 읽히더라도 입장시키면 안 된다
    mine = me(server, user)
    assert mine.status_code == 404 and mine.json().get("code") == "QUEUE_ENTRY_NOT_FOUND", \
        f"손상된 항목({name})은 없는 것으로 보고 404 여야 한다(5xx·ADMITTED 금지): {mine.status_code} {mine.text[:200]}"
    assert not list(r.scan_iter(match=f"{prefix}ticket:*")), "손상된 항목으로 입장권이 발급됐다"
    again = join(server, user)
    assert again.status_code == 201, f"손상된 항목은 없는 것으로 보고 새 번호(201): {again.status_code} {again.text[:200]}"
    assert again.json()["number"] == 2 and again.json()["ticket"] is None
