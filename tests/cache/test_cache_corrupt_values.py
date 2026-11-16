"""손상·형식 불일치 Redis 값 (docs/cache.md 설정 표 아래 규칙): 없는 것으로 보고 DB 에서 계산한다. 5xx·잘못된 200 금지.

테스트가 자기 접두사 키에 직접 잘못된 값을 쓰고 실제 HTTP 로 조회한다(실제 Redis·PostgreSQL, get_now 는 파일 시계).
"""
import json
from datetime import timedelta

import pytest

from cache_support import T0, ms, uid

VALID_UNTIL = ms(T0 + timedelta(seconds=5))  # get_now 기준으로 아직 유효한 시각
GOOD_SEAT = {"id": "x", "section": "A", "row": "1", "number": 1, "status": "AVAILABLE"}

# (id, 키에 쓸 값). bytes 는 그대로, 나머지는 JSON 으로 직렬화한다.
SEATMAP_CASES = [
    ("invalid_utf8", b"\xff\xfe\xfa"),
    ("not_json", "this is not json"),
    ("json_but_not_object", "[1, 2, 3]"),
    ("valid_until_missing", {"body": [GOOD_SEAT]}),
    ("valid_until_string_non_numeric", {"validUntilMs": "soon", "body": [GOOD_SEAT]}),
    ("valid_until_string_numeric", {"validUntilMs": str(VALID_UNTIL), "body": [GOOD_SEAT]}),
    ("body_missing", {"validUntilMs": VALID_UNTIL}),
    ("body_is_object", {"validUntilMs": VALID_UNTIL, "body": {"bad": "shape"}}),
    ("body_is_string", {"validUntilMs": VALID_UNTIL, "body": "bad"}),
    ("body_is_null", {"validUntilMs": VALID_UNTIL, "body": None}),
    ("element_not_object", {"validUntilMs": VALID_UNTIL, "body": [1, 2]}),
    ("element_missing_status", {"validUntilMs": VALID_UNTIL, "body": [{k: v for k, v in GOOD_SEAT.items() if k != "status"}]}),
    ("element_missing_id", {"validUntilMs": VALID_UNTIL, "body": [{k: v for k, v in GOOD_SEAT.items() if k != "id"}]}),
    ("element_status_outside_enum", {"validUntilMs": VALID_UNTIL, "body": [{**GOOD_SEAT, "status": "BROKEN"}]}),
    ("element_number_is_string", {"validUntilMs": VALID_UNTIL, "body": [{**GOOD_SEAT, "number": "1"}]}),
]
IDS = [c[0] for c in SEATMAP_CASES]


def raw(value):
    return value if isinstance(value, (bytes, str)) else json.dumps(value)


def write(r, key, value):
    r.set(key, value if isinstance(value, bytes) else raw(value))


@pytest.mark.parametrize("name,value", SEATMAP_CASES, ids=IDS)
def test_cache_corrupt_seatmap_value_is_treated_as_missing_for_an_existing_show(server, r, name, value):
    show = server.show_id()
    key = f"{server.prefix}seatmap:{show}"
    first = server.seatmap(show)  # 정상 항목으로 기준 본문을 얻는다
    assert first.status_code == 200 and first.headers.get("X-Cache") == "MISS"
    truth = first.json()
    assert len(truth) == 100
    write(r, key, value)  # 정상 항목을 손상 값으로 덮어쓴다
    resp = server.seatmap(show)
    assert resp.status_code == 200, f"손상 값({name})은 5xx 가 아니라 DB 계산이어야 한다: {resp.status_code} {resp.text[:200]}"
    assert resp.headers.get("X-Cache") == "MISS", f"손상 값은 없는 것으로 보고 MISS: {resp.headers.get('X-Cache')}"
    assert resp.json() == truth, "본문은 DB 와 같은 정상 좌석 배열이어야 한다(손상 값의 body 를 그대로 주면 안 된다)"
    again = server.seatmap(show)
    assert again.status_code == 200 and again.headers.get("X-Cache") == "HIT" and again.json() == truth, \
        "MISS 가 정상 항목으로 다시 저장돼 다음 조회는 HIT"


@pytest.mark.parametrize("name,value", SEATMAP_CASES, ids=IDS)
def test_cache_corrupt_seatmap_value_never_makes_an_unknown_show_a_200(server, r, name, value):
    show = "no-such-show"
    write(r, f"{server.prefix}seatmap:{show}", value)
    resp = server.seatmap(show)
    assert resp.status_code == 404, f"없는 공연은 손상 값({name})이 있어도 404 여야 한다: {resp.status_code} {resp.text[:200]}"
    assert resp.json().get("code") == "NOT_FOUND"
    assert resp.headers.get("X-Cache") != "HIT"


# ---- 관문 키 손상: 없는 것으로 보고 DB 가 판단 ----
GATE_CASES = [("invalid_utf8", b"\xff\xfe\xfa"), ("not_json", "this is not json"), ("json_scalar", "123"),
              ("json_array", "[1,2]")]


@pytest.mark.parametrize("name,value", GATE_CASES, ids=[c[0] for c in GATE_CASES])
def test_cache_corrupt_gate_marker_on_available_seat_passes_and_holds_without_5xx(server, r, name, value):
    seat, user = server.seat_ids(1)[0], uid("A")
    key = f"{server.prefix}hold:{seat}"
    write(r, key, value)
    resp = server.hold(seat, user)
    assert resp.status_code == 201, f"손상된 표시({name})는 없는 것으로 보고 홀드가 성공해야 한다: {resp.status_code} {resp.text[:200]}"
    assert resp.headers.get("X-Hold-Gate") == "PASS", "손상 값을 없음으로 보고 덮어쓰면 DB 까지 간다(PASS)"
    marker = json.loads(r.get(key))
    assert marker["state"] == "HELD" and marker["owner"] == user, "정상 표시(HELD)로 복구돼야 한다"


@pytest.mark.parametrize("name,value", GATE_CASES, ids=[c[0] for c in GATE_CASES])
def test_cache_corrupt_gate_marker_on_held_seat_lets_the_db_decide_409(server, r, name, value):
    seat = server.seat_ids(1)[0]
    assert server.hold(seat, uid("A")).status_code == 201
    write(r, f"{server.prefix}hold:{seat}", value)  # Redis 만 손상, DB 는 HELD
    resp = server.hold(seat, uid("B"))
    assert resp.status_code == 409 and resp.json()["code"] == "SEAT_HELD", f"{resp.status_code} {resp.text[:200]}"
    assert resp.headers.get("X-Hold-Gate") == "PASS", "표시가 없는 것으로 보여 관문은 통과, DB 가 판단"
    assert r.get(f"{server.prefix}hold:{seat}") is None, "DB 409 뒤 내 PENDING 표시는 되돌려진다"
