"""API 계약(docs/openapi.yaml)이 docs/stories의 인수 조건을 담고 있는지 검사한다.

테스트 이름 규칙: test_usNN_acM_...  (NN=스토리 번호, M=인수 조건 번호)
scripts/story_report.py 가 이 이름으로 스토리별 통과표를 만든다.

경로는 계약에서 정하는 것이므로 고정하지 않고, 메서드 + 경로 끝 마디로 찾는다.
  US-01 목록      GET  .../shows
  US-02 좌석맵    GET  .../shows/{id}/(seats|seat-map|seatmap)
  US-03 홀드      POST .../hold
  US-01·US-02: 흔한 동의어(대소문자·_ 무시)를 허용한다(6장에서 바뀌지 않은 부분).
  US-03~US-05(6장 계약): 경로·헤더·필드·오류 code 를 openapi.yaml(v0.2.0)의 이름 그대로 단언한다.
행동(동시성·만료 경과)은 계약 파일로 검증할 수 없어 e2e/구현 테스트에서 다룬다.
"""
from pathlib import Path

import pytest
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


# ---------- 6장 계약: 이름 그대로 ----------
def path_op(spec, path, method):
    item = resolve(spec, spec["paths"].get(path))
    assert item is not None, f"경로 {path} 가 계약에 없다"
    assert method in item, f"{method.upper()} {path} 가 계약에 없다"
    return item, resolve(spec, item[method])


def header_param(spec, item, op, name):
    for p in params(spec, item, op):
        if p.get("in") == "header" and p.get("name") == name:
            return p
    return None


def schema_named(spec, name):
    node = spec["components"]["schemas"].get(name)
    assert node is not None, f"components.schemas.{name} 가 없다"
    return resolve(spec, node)


def error_enum(spec):
    return set(schema_named(spec, "Error")["properties"]["code"]["enum"])


def schema_of(spec, resp):
    return body_schema(spec, resp)


# ---------- US-03 홀드 ----------
def test_us03_ac1_hold_success_201_has_hold_id_seat_user_and_expires_at():
    spec = load_spec()
    item, op = path_op(spec, "/seats/{seatId}/hold", "post")
    ok = responses(spec, op)
    assert "201" in ok
    schema = schema_of(spec, ok["201"])
    assert {"holdId", "seatId", "userId", "expiresAt"} <= set(props(spec, schema)), sorted(props(spec, schema))
    assert set(schema.get("required", [])) >= {"holdId", "seatId", "userId", "expiresAt"}
    assert props(spec, schema)["expiresAt"].get("format") == "date-time"


def test_us03_ac2_ac3_ac6_seat_already_held_is_409_seat_held():
    spec = load_spec()
    _, op = path_op(spec, "/seats/{seatId}/hold", "post")
    r = responses(spec, op).get("409")
    assert r is not None, "이미 잡힌 좌석(409)이 정의돼 있지 않다"
    assert "SEAT_HELD" in str(r), "409 에 SEAT_HELD 설명이 없다"


def test_us03_ac5_sold_seat_is_409_seat_sold():
    spec = load_spec()
    _, op = path_op(spec, "/seats/{seatId}/hold", "post")
    assert "SEAT_SOLD" in str(responses(spec, op).get("409")), "409 에 SEAT_SOLD 설명이 없다"


def test_us03_ac7_hold_requires_x_user_id_and_has_401():
    spec = load_spec()
    item, op = path_op(spec, "/seats/{seatId}/hold", "post")
    header = header_param(spec, item, op, "X-User-Id")
    assert header and header.get("required") is True, "홀드에 required X-User-Id 헤더가 없다"
    assert "401" in responses(spec, op), "사용자 식별 필요(401)가 없다"


def test_us03_ac8_unknown_seat_is_404():
    spec = load_spec()
    _, op = path_op(spec, "/seats/{seatId}/hold", "post")
    assert "404" in responses(spec, op)


def test_us03_ac9_ac10_hold_description_states_new_hold_id_and_no_extension():
    spec = load_spec()
    _, op = path_op(spec, "/seats/{seatId}/hold", "post")
    text = op.get("description", "")
    assert "holdId" in text and "연장" in text, "홀드 설명에 새 holdId 발급과 연장 없음이 없다"


# ---------- US-04 결제 요청 ----------
def test_us04_ac1_post_bookings_requires_user_and_idempotency_key_headers():
    spec = load_spec()
    item, op = path_op(spec, "/bookings", "post")
    user = header_param(spec, item, op, "X-User-Id")
    key = header_param(spec, item, op, "Idempotency-Key")
    assert user and user.get("required") is True
    assert key and key.get("required") is True, "Idempotency-Key 헤더가 required 가 아니다"
    assert key["schema"].get("minLength") == 1 and key["schema"].get("maxLength") == 255


def test_us04_ac1_success_is_202_pending_booking_with_all_fields():
    spec = load_spec()
    _, op = path_op(spec, "/bookings", "post")
    ok = responses(spec, op)
    assert "202" in ok, "결제 요청 성공은 202 여야 한다"
    schema = schema_of(spec, ok["202"])
    required = {"id", "seatId", "userId", "status", "paymentId", "holdId", "holdExpiresAt", "createdAt", "updatedAt"}
    assert required <= set(props(spec, schema)), sorted(props(spec, schema))
    assert required <= set(schema.get("required", []))
    assert "Idempotent-Replayed" in (ok["202"].get("headers") or {}), "202 에 Idempotent-Replayed 헤더 정의가 없다"


def test_us04_ac1_booking_status_enum_is_exactly_the_four_states():
    spec = load_spec()
    assert set(schema_named(spec, "BookingStatus")["enum"]) == {"PENDING", "CONFIRMED", "FAILED", "REFUND_REQUIRED"}


def test_us04_ac9_confirmed_own_booking_is_200():
    spec = load_spec()
    _, op = path_op(spec, "/bookings", "post")
    assert "200" in responses(spec, op), "이미 확정된 본인 예매(200)가 정의돼 있지 않다"


def test_us04_ac3_not_my_hold_is_403():
    spec = load_spec()
    _, op = path_op(spec, "/bookings", "post")
    assert "403" in responses(spec, op) and "HOLD_NOT_OWNED" in str(responses(spec, op)["403"])


def test_us04_ac2_ac5_ac7_ac10_ac11_conflict_codes_are_documented():
    spec = load_spec()
    _, op = path_op(spec, "/bookings", "post")
    text = str(responses(spec, op).get("409"))
    for code in ("IDEMPOTENCY_KEY_MISMATCH", "SEAT_SOLD", "NO_HOLD", "HOLD_EXPIRED", "PAYMENT_IN_PROGRESS"):
        assert code in text, f"409 에 {code} 설명이 없다"


def test_us04_ac12_invalid_request_unauthenticated_and_not_found_are_defined():
    spec = load_spec()
    _, op = path_op(spec, "/bookings", "post")
    r = responses(spec, op)
    assert {"400", "401", "404"} <= set(r), sorted(r)
    assert "INVALID_REQUEST" in str(r["400"])


def test_us04_ac14_get_booking_defined_for_owner_with_404_and_401():
    spec = load_spec()
    item, op = path_op(spec, "/bookings/{bookingId}", "get")
    header = header_param(spec, item, op, "X-User-Id")
    assert header and header.get("required") is True
    r = responses(spec, op)
    assert {"200", "401", "404"} <= set(r), sorted(r)
    schema = schema_of(spec, r["200"])
    assert {"id", "status", "paymentId", "holdId"} <= set(props(spec, schema))


# ---------- US-05 콜백 ----------
def test_us05_ac9_callback_requires_signature_header_not_user_header():
    spec = load_spec()
    item, op = path_op(spec, "/payments/callback", "post")
    sig = header_param(spec, item, op, "X-Mock-Pg-Signature")
    assert sig and sig.get("required") is True, "X-Mock-Pg-Signature 가 required 헤더가 아니다"
    assert header_param(spec, item, op, "X-User-Id") is None, "콜백이 X-User-Id 를 요구하면 안 된다"


def test_us05_ac1_callback_request_body_is_payment_id_and_result_enum():
    spec = load_spec()
    _, op = path_op(spec, "/payments/callback", "post")
    schema = resolve(spec, op["requestBody"]["content"]["application/json"]["schema"])
    assert set(schema.get("required", [])) >= {"paymentId", "result"}
    assert set(props(spec, schema)["result"]["enum"]) == {"SUCCEEDED", "FAILED"}


def test_us05_ac4_callback_success_response_has_applied_and_booking():
    spec = load_spec()
    _, op = path_op(spec, "/payments/callback", "post")
    schema = schema_of(spec, responses(spec, op)["200"])
    assert {"applied", "booking"} <= set(props(spec, schema))
    assert props(spec, schema)["applied"].get("type") == "boolean"


def test_us05_ac5_ac7_ac9_callback_error_responses_are_defined():
    spec = load_spec()
    _, op = path_op(spec, "/payments/callback", "post")
    r = responses(spec, op)
    assert {"400", "401", "404", "409"} <= set(r), sorted(r)
    assert "INVALID_SIGNATURE" in str(r["401"])
    assert "CALLBACK_CONFLICT" in str(r["409"])


def test_us05_ac2_ac6_callback_description_states_state_table_and_server_clock():
    spec = load_spec()
    _, op = path_op(spec, "/payments/callback", "post")
    text = op.get("description", "")
    for word in ("REFUND_REQUIRED", "CONFIRMED", "get_now", "HMAC-SHA256", "MOCK_PG_SECRET"):
        assert word in text, f"콜백 설명에 {word} 가 없다"


# ---------- 공통: 오류 code enum ----------
ALL_ERROR_CODES = {"UNAUTHENTICATED", "INVALID_REQUEST", "NOT_FOUND", "SEAT_HELD", "SEAT_SOLD", "HOLD_NOT_OWNED",
                   "NO_HOLD", "HOLD_EXPIRED", "IDEMPOTENCY_KEY_MISMATCH", "PAYMENT_IN_PROGRESS",
                   "INVALID_SIGNATURE", "CALLBACK_CONFLICT", "RATE_LIMITED", "UPSTREAM_UNAVAILABLE",
                   "QUEUE_TICKET_REQUIRED", "QUEUE_TICKET_INVALID", "QUEUE_TICKET_EXPIRED", "QUEUE_ENTRY_NOT_FOUND",
                   "QUEUE_UNAVAILABLE"}  # v0.5.0: 새 code 5개 추가(총 19)


def test_us04_ac12_error_code_enum_has_all_codes_including_the_queue_ones():
    spec = load_spec()
    assert error_enum(spec) == ALL_ERROR_CODES, sorted(error_enum(spec) ^ ALL_ERROR_CODES)


def test_us04_ac1_old_confirm_flow_is_gone_bookings_201_not_defined():
    spec = load_spec()
    _, op = path_op(spec, "/bookings", "post")
    assert "201" not in responses(spec, op), "결제 요청이 옛 방식(201 바로 확정)으로 남아 있다"


# ---------- 7장 계약(v0.3.0): 게이트웨이 응답 429 · 502 ----------
RATE_LIMITED_PATHS = [("/shows", "get"), ("/shows/{showId}/seats", "get"), ("/seats/{seatId}/hold", "post"),
                      ("/bookings", "post"), ("/bookings/{bookingId}", "get"),
                      ("/shows/{showId}/queue", "post"), ("/shows/{showId}/queue/me", "get")]  # X-User-Id 를 받을 수 있는 경로
ALL_PATHS = RATE_LIMITED_PATHS + [("/payments/callback", "post")]


def raw_response_ref(spec, path, method, code):
    op = spec["paths"][path][method]
    return (op.get("responses") or {}).get(code)


def test_contract_version_is_0_5_0_and_mentions_gateway_app_id_header():
    spec = load_spec()
    assert spec["info"]["version"] == "0.5.0", spec["info"]["version"]
    assert "X-App-Id" in spec["info"].get("description", ""), "info 설명에 X-App-Id 가 없다"


@pytest.mark.parametrize("path,method", RATE_LIMITED_PATHS, ids=[f"{m}_{p}" for p, m in RATE_LIMITED_PATHS])
def test_contract_429_rate_limited_is_defined_on_every_path_that_accepts_user_id(path, method):
    spec = load_spec()
    r = raw_response_ref(spec, path, method, "429")
    assert r == {"$ref": "#/components/responses/RateLimited"}, f"{method.upper()} {path} 의 429 가 RateLimited 가 아니다: {r}"


def test_contract_callback_has_no_rate_limit_response():
    spec = load_spec()
    assert raw_response_ref(spec, "/payments/callback", "post", "429") is None, "콜백은 속도 제한이 없다(X-User-Id 없음)"


@pytest.mark.parametrize("path,method", ALL_PATHS, ids=[f"{m}_{p}" for p, m in ALL_PATHS])
def test_contract_502_upstream_unavailable_is_defined_on_every_path(path, method):
    spec = load_spec()
    r = raw_response_ref(spec, path, method, "502")
    assert r == {"$ref": "#/components/responses/UpstreamUnavailable"}, f"{method.upper()} {path} 의 502: {r}"


def test_contract_rate_limited_response_has_retry_after_header_and_error_body():
    spec = load_spec()
    r = resolve(spec, spec["components"]["responses"]["RateLimited"])
    retry = r["headers"]["Retry-After"]
    assert retry["schema"]["type"] == "integer" and retry["schema"].get("minimum") == 1, retry
    schema = resolve(spec, r["content"]["application/json"]["schema"])
    assert schema is schema_named(spec, "Error") or schema == schema_named(spec, "Error")
    text = r.get("description", "")
    assert "RATE_LIMITED" in text and "X-App-Id" in text, "설명에 RATE_LIMITED 와 X-App-Id 없음이 있어야 한다"


def test_contract_upstream_unavailable_response_has_error_body_and_idempotency_retry_hint():
    spec = load_spec()
    r = resolve(spec, spec["components"]["responses"]["UpstreamUnavailable"])
    schema = resolve(spec, r["content"]["application/json"]["schema"])
    assert schema == schema_named(spec, "Error")
    text = r.get("description", "")
    assert "UPSTREAM_UNAVAILABLE" in text and "Idempotency-Key" in text, "설명에 코드와 같은 키 재시도 안내가 있어야 한다"


def test_contract_gateway_error_codes_are_in_the_error_enum():
    spec = load_spec()
    assert {"RATE_LIMITED", "UPSTREAM_UNAVAILABLE"} <= error_enum(spec)


# ---------- 8장 계약(v0.4.0): Redis 좌석맵 캐시(X-Cache) · 홀드 관문(X-Hold-Gate) ----
def test_contract_info_describes_redis_cache_and_gate_headers():
    spec = load_spec()
    text = spec["info"].get("description", "")
    for word in ("Redis", "X-Cache", "X-Hold-Gate", "docs/cache.md"):
        assert word in text, f"info 설명에 {word} 가 없다"


def test_contract_seatmap_200_defines_x_cache_header_enum_hit_miss_bypass():
    spec = load_spec()
    r = resolve(spec, spec["paths"]["/shows/{showId}/seats"]["get"]["responses"]["200"])
    header = r["headers"]["X-Cache"]
    assert header["schema"]["type"] == "string"
    assert header["schema"]["enum"] == ["HIT", "MISS", "BYPASS"], header["schema"]
    desc = header.get("description", "")
    for word in ("HIT", "MISS", "BYPASS"):
        assert word in desc, f"X-Cache 설명에 {word} 가 없다"


def test_contract_seatmap_description_states_cache_validity_and_invalidation():
    spec = load_spec()
    text = spec["paths"]["/shows/{showId}/seats"]["get"].get("description", "")
    for word in ("최대 5초", "가장 이른 만료", "지워진다", "만료 시각부터는 AVAILABLE"):
        assert word in text, f"좌석맵 설명에 '{word}' 가 없다"


def test_contract_hold_description_states_x_hold_gate_values_409_kept_and_gate_codes_provisional():
    spec = load_spec()
    op = spec["paths"]["/seats/{seatId}/hold"]["post"]
    text = " ".join(op.get("description", "").split())  # 줄바꿈 정규화
    for word in ("X-Hold-Gate", "PASS", "REJECT", "BYPASS"):
        assert word in text, f"홀드 설명에 {word} 가 없다"
    assert "상태 코드(409)와 상태 불변은 같고" in text, "관문 거절도 409·상태 불변이라는 문구가 없다"
    assert "관문 거절의 code(SEAT_HELD/SEAT_SOLD)는 Redis 표시 근거의 잠정 값" in text, "관문 거절 code 가 잠정 값이라는 문구가 없다"
    assert "SEAT_SOLD 대신 SEAT_HELD" in text, "드문 SEAT_SOLD→SEAT_HELD 예외 설명이 없다"
    assert "201·404·409 응답에 진단 헤더 X-Hold-Gate" in text, "X-Hold-Gate 가 붙는 응답 범위(201·404·409)가 없다"
    assert "401(X-User-Id 없음)은 관문 전에 끝나므로 이 헤더가 없다" in text, "401 에는 헤더가 없다는 문구가 없다"
    assert "오류 code 계약은 같다" not in text, "옛 문구('오류 code 계약은 같다')가 남아 있다"
    assert {"201", "401", "404", "409"} <= set(op["responses"]), sorted(op["responses"])
    assert error_enum(spec) == ALL_ERROR_CODES


def test_contract_only_seatmap_defines_x_cache_header():
    spec = load_spec()
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method not in ("get", "post"):
                continue
            for code, resp in (op.get("responses") or {}).items():
                headers = (resolve(spec, resp) or {}).get("headers") or {}
                if "X-Cache" in headers:
                    assert (path, method, code) == ("/shows/{showId}/seats", "get", "200"), (path, method, code)


# ---------- 9장 계약(v0.5.0): 가상 대기열 · 입장권 ----------
def test_contract_info_describes_the_virtual_queue_and_ticket_header():
    spec = load_spec()
    text = " ".join(spec["info"].get("description", "").split())
    for word in ("가상 대기열", "docs/queue.md", "1초마다 200명씩", "2분짜리 입장권", "X-Queue-Ticket", "503 QUEUE_UNAVAILABLE"):
        assert word in text, f"info 설명에 '{word}' 가 없다"


def test_contract_join_queue_post_defines_201_200_and_errors_with_queue_status_body():
    spec = load_spec()
    item, op = path_op(spec, "/shows/{showId}/queue", "post")
    assert op["operationId"] == "joinQueue"
    assert header_param(spec, item, op, "X-User-Id")["required"] is True
    r = responses(spec, op)
    assert {"200", "201", "401", "404", "429", "502", "503"} <= set(r), sorted(r)
    for code in ("200", "201"):
        assert schema_of(spec, r[code]) == schema_named(spec, "QueueStatus"), code
    assert "201" in r and "200" in r
    text = " ".join(op.get("description", "").split())
    for word in ("같은 사용자는 다시 불러도", "200", "만료된 뒤 부르면 맨 뒤 새 번호(201)", "전광판 올리기"):
        assert word in text, f"번호표 설명에 '{word}' 가 없다"
    assert raw_response_ref(spec, "/shows/{showId}/queue", "post", "503") == {"$ref": "#/components/responses/QueueUnavailable"}


def test_contract_queue_me_get_defines_retry_after_header_and_entry_not_found():
    spec = load_spec()
    item, op = path_op(spec, "/shows/{showId}/queue/me", "get")
    assert op["operationId"] == "getMyQueueStatus"
    assert header_param(spec, item, op, "X-User-Id")["required"] is True
    r = responses(spec, op)
    assert {"200", "401", "404", "429", "502", "503"} <= set(r), sorted(r)
    retry = r["200"]["headers"]["Retry-After"]["schema"]
    assert retry["type"] == "integer" and retry["minimum"] == 1 and retry["maximum"] == 5, retry
    assert schema_of(spec, r["200"]) == schema_named(spec, "QueueStatus")
    assert "QUEUE_ENTRY_NOT_FOUND" in str(r["404"]), "줄 선 적 없음 404 의 code 가 QUEUE_ENTRY_NOT_FOUND 여야 한다"


def test_contract_queue_status_schema_fields_status_enum_and_nullable_ticket():
    spec = load_spec()
    status = schema_named(spec, "QueueStatus")
    required = {"showId", "number", "serving", "ahead", "status", "retryAfterSeconds", "ticket"}
    assert required <= set(status["required"]) and required <= set(status["properties"])
    assert status["properties"]["status"]["enum"] == ["WAITING", "ADMITTED", "EXPIRED"]
    assert status["properties"]["number"]["minimum"] == 1 and status["properties"]["serving"]["minimum"] == 0
    assert status["properties"]["ahead"]["minimum"] == 0
    ticket_one_of = status["properties"]["ticket"]["oneOf"]
    assert {"$ref": "#/components/schemas/QueueTicket"} in ticket_one_of and {"type": "null"} in ticket_one_of, ticket_one_of
    ticket = schema_named(spec, "QueueTicket")
    assert set(ticket["required"]) == {"token", "expiresAt"}
    assert ticket["properties"]["expiresAt"]["format"] == "date-time"


@pytest.mark.parametrize("path,method", [("/shows/{showId}/seats", "get"), ("/seats/{seatId}/hold", "post")],
                         ids=["seatmap", "hold"])
def test_contract_ticket_header_required_and_403_503_defined_where_a_ticket_is_needed(path, method):
    spec = load_spec()
    item, op = path_op(spec, path, method)
    ticket = header_param(spec, item, op, "X-Queue-Ticket")
    assert ticket and ticket["required"] is True, f"{path} 에 required X-Queue-Ticket 헤더가 없다"
    assert header_param(spec, item, op, "X-User-Id")["required"] is True, f"{path} 는 X-User-Id 도 필요하다"
    assert raw_response_ref(spec, path, method, "403") == {"$ref": "#/components/responses/QueueTicketRejected"}
    assert raw_response_ref(spec, path, method, "503") == {"$ref": "#/components/responses/QueueUnavailable"}
    assert "401" in responses(spec, op)


def test_contract_seatmap_description_states_ticket_check_before_cache_and_403_for_unknown_show():
    spec = load_spec()
    text = " ".join(spec["paths"]["/shows/{showId}/seats"]["get"]["description"].split())
    for word in ("X-User-Id와 이 공연의 유효한 입장권(X-Queue-Ticket)이 필요하다", "입장권 검증은 좌석 캐시보다 먼저 한다",
                 "없는 공연 조회는 403으로 끝난다"):
        assert word in text, f"좌석맵 설명에 '{word}' 가 없다"
    assert "X-User-Id는 필요 없다" not in text, "옛 문구('X-User-Id는 필요 없다')가 남아 있다"


def test_contract_hold_description_states_the_9th_chapter_check_order():
    spec = load_spec()
    text = " ".join(spec["paths"]["/seats/{seatId}/hold"]["post"]["description"].split())
    for word in ("순서: 401 → 입력 검증 → 좌석의 공연 찾기(없는 좌석 404)", "입장권 검증(403/503) → Redis 홀드 관문 → DB",
                 "입장권 거절은 관문 표시와 DB를 바꾸지 않는다"):
        assert word in text, f"홀드 설명에 '{word}' 가 없다"


def test_contract_queue_ticket_parameter_and_rejection_responses():
    spec = load_spec()
    param = spec["components"]["parameters"]["QueueTicket"]
    assert param["name"] == "X-Queue-Ticket" and param["in"] == "header" and param["required"] is True
    for code in ("QUEUE_TICKET_REQUIRED", "QUEUE_TICKET_INVALID", "QUEUE_TICKET_EXPIRED"):
        assert code in param["description"], f"입장권 헤더 설명에 {code} 가 없다"
    rejected = " ".join(spec["components"]["responses"]["QueueTicketRejected"]["description"].split())
    for word in ("QUEUE_TICKET_REQUIRED(헤더 없음)", "QUEUE_TICKET_INVALID(모르는 token·다른 사용자·다른 공연)",
                 "QUEUE_TICKET_EXPIRED(now ≥ expiresAt)", "좌석 캐시·홀드 관문·DB 모두 건드리지 않는다"):
        assert word in rejected, f"QueueTicketRejected 설명에 '{word}' 가 없다"
    unavailable = spec["components"]["responses"]["QueueUnavailable"]
    assert unavailable["headers"]["Retry-After"]["schema"] == {"type": "integer", "minimum": 1}
    assert "QUEUE_UNAVAILABLE" in unavailable["description"] and "우회 없음" in unavailable["description"]


def test_contract_only_ticketed_and_queue_paths_define_403_queue_rejection_and_queue_503():
    spec = load_spec()
    with_403 = {(p, m) for p, item in spec["paths"].items() for m, op in item.items()
                if m in ("get", "post") and "403" in (op.get("responses") or {})
                and (op["responses"]["403"] == {"$ref": "#/components/responses/QueueTicketRejected"})}
    assert with_403 == {("/shows/{showId}/seats", "get"), ("/seats/{seatId}/hold", "post")}, with_403
    with_503 = {(p, m) for p, item in spec["paths"].items() for m, op in item.items()
                if m in ("get", "post") and (op.get("responses") or {}).get("503") == {"$ref": "#/components/responses/QueueUnavailable"}}
    assert with_503 == {("/shows/{showId}/seats", "get"), ("/seats/{seatId}/hold", "post"),
                        ("/shows/{showId}/queue", "post"), ("/shows/{showId}/queue/me", "get")}, with_503
