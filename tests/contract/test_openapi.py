"""API 계약(docs/openapi.yaml)이 docs/stories의 인수 조건을 담고 있는지 검사한다.

테스트 이름 규칙: test_usNN_acM_...  (NN=스토리 번호, M=인수 조건 번호)
scripts/story_report.py 가 이 이름으로 스토리별 통과표를 만든다.

경로는 계약에서 정하는 것이므로 고정하지 않고, 메서드 + 경로 끝 마디로 찾는다.
  US-01 목록      GET  .../shows
  US-02 좌석맵    GET  .../shows/{id}/(seats|seat-map|seatmap)
  US-03 홀드      POST .../hold
  US-04 예매확정  POST .../(bookings|booking|confirm|purchase|pay|payment)
필드 이름은 흔한 동의어(대소문자·_ 무시)를 허용한다.
행동(동시성·만료 경과)은 계약 파일로 검증할 수 없어 e2e/구현 테스트에서 다룬다.
"""
from pathlib import Path

import yaml

SPEC_PATH = Path(__file__).resolve().parents[2] / "docs" / "openapi.yaml"
METHODS = ("get", "post", "put", "patch", "delete")


# ---------- helpers ----------
def load_spec() -> dict:
    assert SPEC_PATH.exists(), f"API 계약 파일이 없다: {SPEC_PATH}"
    spec = yaml.safe_load(SPEC_PATH.read_text())
    assert isinstance(spec, dict) and spec.get("paths"), "openapi.yaml 에 paths 가 없다"
    return spec


def resolve(spec, node):
    seen = 0
    while isinstance(node, dict) and "$ref" in node and seen < 20:
        cur = spec
        for part in node["$ref"].lstrip("#/").split("/"):
            cur = cur[part]
        node, seen = cur, seen + 1
    return node


def norm(name: str) -> str:
    return name.lower().replace("_", "").replace("-", "")


def find_ops(spec, method, last_segments, must_contain=None):
    found = []
    for path, item in spec["paths"].items():
        item = resolve(spec, item) or {}
        if method not in item:
            continue
        parts = [p for p in path.strip("/").split("/") if p]
        if not parts or norm(parts[-1]) not in {norm(s) for s in last_segments}:
            continue
        if must_contain and must_contain not in norm(path):
            continue
        found.append((path, item, resolve(spec, item[method])))
    return found


def get_op(spec, method, last_segments, must_contain=None):
    ops = find_ops(spec, method, last_segments, must_contain)
    assert ops, f"{method.upper()} …/{'|'.join(last_segments)} 경로가 계약에 없다"
    return ops[0]


def params(spec, path_item, op):
    out = []
    for p in (path_item.get("parameters") or []) + (op.get("parameters") or []):
        out.append(resolve(spec, p))
    return out


def user_header(spec, path_item, op):
    for p in params(spec, path_item, op):
        if p.get("in") == "header" and norm(p.get("name", "")) == "xuserid":
            return p
    return None


def responses(spec, op):
    return {str(k): resolve(spec, v) for k, v in (op.get("responses") or {}).items()}


def success(spec, op):
    ok = {c: r for c, r in responses(spec, op).items() if c.startswith("2")}
    assert ok, "2xx 성공 응답이 정의돼 있지 않다"
    return ok


def body_schema(spec, response):
    content = (response or {}).get("content") or {}
    media = content.get("application/json")
    assert media is not None, "성공 응답에 application/json 본문이 없다"
    return resolve(spec, media.get("schema") or {})


def props(spec, schema) -> dict:
    schema = resolve(spec, schema) or {}
    out = {}
    for sub in schema.get("allOf", []):
        out.update(props(spec, sub))
    out.update({k: resolve(spec, v) for k, v in (schema.get("properties") or {}).items()})
    return out


def item_props(spec, schema) -> dict:
    """목록 응답: 배열이거나, 배열 필드를 가진 객체({items:[...]})에서 원소의 속성을 구한다."""
    schema = resolve(spec, schema)
    if schema.get("type") == "array":
        return props(spec, schema.get("items") or {})
    for v in props(spec, schema).values():
        if v.get("type") == "array":
            return props(spec, v.get("items") or {})
    raise AssertionError("응답이 배열(또는 배열 필드를 가진 객체)이 아니다")


def has_any(properties, *synonyms) -> bool:
    names = {norm(k) for k in properties}
    return any(norm(s) in names for s in synonyms)


def error_codes(spec, op):
    return {c for c in responses(spec, op) if c[:1] in "45"}


def require_fields(properties, what, groups):
    missing = [g[0] for g in groups if not has_any(properties, *g)]
    assert not missing, f"{what} 에 필드가 없다: {missing} (있는 필드: {sorted(properties)})"


LIST = ("shows",)
SEATMAP = ("seats", "seat-map", "seatmap")
HOLD = ("hold",)
CONFIRM = ("bookings", "booking", "confirm", "purchase", "pay", "payment")

SHOW_FIELDS = [("id", "showId"), ("title", "name"), ("venue", "location", "place"),
               ("startsAt", "startAt", "startTime", "startedAt", "startDateTime", "datetime")]
SEAT_FIELDS = [("id", "seatId"), ("section", "zone", "area"), ("row",),
               ("number", "seatNumber", "no"), ("status",)]
EXPIRES = ("expiresAt", "holdExpiresAt", "heldUntil", "holdUntil")


# ---------- US-01 공연 목록 ----------
def test_us01_ac1_list_path_success_and_show_fields():
    spec = load_spec()
    _, _, op = get_op(spec, "get", LIST)
    ok = success(spec, op)
    assert "200" in ok
    require_fields(item_props(spec, body_schema(spec, ok["200"])), "공연 항목", SHOW_FIELDS)


def test_us01_ac2_empty_list_is_success_array():
    spec = load_spec()
    _, _, op = get_op(spec, "get", LIST)
    schema = body_schema(spec, success(spec, op)["200"])
    item_props(spec, schema)  # 배열 형태(빈 배열 가능)여야 한다
    assert not any(c == "404" for c in responses(spec, op)), "빈 목록은 404 가 아니라 200 이어야 한다"


def test_us01_ac3_list_does_not_require_user_header():
    spec = load_spec()
    path, item, op = get_op(spec, "get", LIST)
    header = user_header(spec, item, op)
    assert header is None or not header.get("required", False), "목록 조회가 X-User-Id 를 요구한다"


# ---------- US-02 좌석맵 ----------
def test_us02_ac1_seatmap_path_and_seat_fields():
    spec = load_spec()
    _, _, op = get_op(spec, "get", SEATMAP, must_contain="shows")
    ok = success(spec, op)
    assert "200" in ok
    require_fields(item_props(spec, body_schema(spec, ok["200"])), "좌석 항목", SEAT_FIELDS)


def test_us02_ac2_ac3_ac4_status_enum_has_all_three_states():
    spec = load_spec()
    _, _, op = get_op(spec, "get", SEATMAP, must_contain="shows")
    seat = item_props(spec, body_schema(spec, success(spec, op)["200"]))
    key = next((k for k in seat if norm(k) == "status"), None)
    assert key, "좌석 항목에 status 가 없다"
    enum = set(seat[key].get("enum") or [])
    assert {"AVAILABLE", "HELD", "SOLD"} <= enum, f"status enum 이 부족하다: {sorted(enum)}"


def test_us02_ac5_unknown_show_is_404():
    spec = load_spec()
    _, _, op = get_op(spec, "get", SEATMAP, must_contain="shows")
    assert "404" in responses(spec, op), "없는 공연에 대한 404 가 정의돼 있지 않다"


# ---------- US-03 홀드 ----------
def test_us03_ac1_hold_success_has_expiry():
    spec = load_spec()
    _, _, op = get_op(spec, "post", HOLD)
    ok = success(spec, op)
    schema = body_schema(spec, next(iter(ok.values())))
    assert has_any(props(spec, schema), *EXPIRES), f"홀드 응답에 만료 시각이 없다: {sorted(props(spec, schema))}"


def test_us03_ac2_ac3_ac6_seat_already_held_is_409():
    spec = load_spec()
    _, _, op = get_op(spec, "post", HOLD)
    assert "409" in responses(spec, op), "이미 잡힌 좌석(409)이 정의돼 있지 않다"


def test_us03_ac5_sold_seat_is_conflict_documented():
    spec = load_spec()
    _, _, op = get_op(spec, "post", HOLD)
    r = responses(spec, op).get("409")
    assert r is not None, "이미 판매된 좌석(409)이 정의돼 있지 않다"
    text = (r.get("description") or "") + str(r.get("content") or "")
    assert any(w in text for w in ("SOLD", "판매", "sold")), \
        "409 응답에 '이미 잡힘'과 '이미 판매됨'을 구분할 설명/오류코드가 없다"


def test_us03_ac7_hold_requires_user_header_and_has_401_or_400():
    spec = load_spec()
    path, item, op = get_op(spec, "post", HOLD)
    header = user_header(spec, item, op)
    assert header and header.get("required") is True, "홀드에 required X-User-Id 헤더가 없다"
    assert error_codes(spec, op) & {"400", "401"}, "사용자 식별 필요 오류(400/401)가 없다"


def test_us03_ac8_unknown_seat_is_404():
    spec = load_spec()
    _, _, op = get_op(spec, "post", HOLD)
    assert "404" in responses(spec, op), "없는 좌석에 대한 404 가 정의돼 있지 않다"


# ---------- US-04 예매 확정 ----------
def test_us04_ac1_confirm_success_has_booking_seat_user():
    spec = load_spec()
    _, _, op = get_op(spec, "post", CONFIRM)
    ok = success(spec, op)
    schema = body_schema(spec, next(iter(ok.values())))
    require_fields(props(spec, schema), "예매 응답",
                   [("id", "bookingId"), ("seatId", "seat"), ("userId", "user")])


def test_us04_ac2_not_my_hold_is_403():
    spec = load_spec()
    _, _, op = get_op(spec, "post", CONFIRM)
    assert "403" in responses(spec, op), "본인 홀드 아님(403)이 정의돼 있지 않다"


def test_us04_ac3_ac4_expired_or_missing_hold_is_rejected():
    spec = load_spec()
    _, _, op = get_op(spec, "post", CONFIRM)
    assert error_codes(spec, op) & {"409", "410"}, "홀드 만료/홀드 없음 오류(409/410)가 없다"


def test_us04_ac5_already_sold_is_409():
    spec = load_spec()
    _, _, op = get_op(spec, "post", CONFIRM)
    assert "409" in responses(spec, op), "이미 판매된 좌석(409)이 정의돼 있지 않다"


def test_us04_ac6_hold_on_sold_seat_stays_rejected_in_contract():
    # SOLD 는 만료되지 않는다: 홀드 계약이 SOLD 거절(409)을 가진다 (행동 검증은 e2e).
    spec = load_spec()
    _, _, op = get_op(spec, "post", HOLD)
    assert "409" in responses(spec, op)


def test_us04_ac7_confirm_requires_user_header_and_has_401_or_400():
    spec = load_spec()
    path, item, op = get_op(spec, "post", CONFIRM)
    header = user_header(spec, item, op)
    assert header and header.get("required") is True, "예매 확정에 required X-User-Id 헤더가 없다"
    assert error_codes(spec, op) & {"400", "401"}, "사용자 식별 필요 오류(400/401)가 없다"
