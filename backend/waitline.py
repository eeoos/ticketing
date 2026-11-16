"""가상 대기열·전광판·입장권 (docs/queue.md). 대기열과 입장권 검증은 Redis 가 없으면 우회하지 않고 503.

Lua 는 모두 단일 스레드로 직렬 실행되므로(세 앱·여러 워커가 동시에 불러도) 스크립트 하나는 원자적이다.
시각은 호출한 쪽이 넘긴 get_now ms 만 쓴다(Redis 시계·TTL 로 판단하지 않음; PX 는 청소용).
"""
import json
import logging
import math
import os
from datetime import datetime, timezone

import redis

from backend import cache

log = logging.getLogger("backend.waitline")

# KEYS: 1=seq 2=serving 3=tick / ARGV: 1=nowTick 2=batch
ADVANCE_LUA = """
local last = redis.call('GET', KEYS[3])
local now = tonumber(ARGV[1])
if not last then
  redis.call('SET', KEYS[3], ARGV[1])
  return 0
end
last = tonumber(last)
if now > last then
  local serving = tonumber(redis.call('GET', KEYS[2]) or '0') or 0
  local seq = tonumber(redis.call('GET', KEYS[1]) or '0') or 0
  local n = serving + tonumber(ARGV[2]) * (now - last)
  if n > seq then n = seq end
  if n < serving then n = serving end
  redis.call('SET', KEYS[2], n)
  redis.call('SET', KEYS[3], ARGV[1])
  return n
end
return -1
"""

# 사용자 항목 해석. 손상(nil) = docs/queue.md '손상'의 뜻과 같은 규칙(파이썬 check_ticket 과 맞춤):
# number 는 1 이상의 정수(문자열·bool·소수·0·음수 거절), ticket 은 문자열|null, ticketExpiresAtMs 는 정수|null,
# 둘 중 하나만 null 이면 손상. (cjson 은 JSON 1.0 과 1 을 구분하지 못한다 — 한계)
_PARSE = """
local function isint(n) return type(n) == 'number' and n == math.floor(n) end
local function parse(v)
  if not v then return nil end
  local ok, d = pcall(cjson.decode, v)
  if not ok or type(d) ~= 'table' then return nil end
  if not isint(d.number) or d.number < 1 then return nil end
  local t, e = d.ticket, d.ticketExpiresAtMs
  if t == cjson.null then t = nil end
  if e == cjson.null then e = nil end
  if (t == nil) ~= (e == nil) then return nil end
  if t ~= nil and (type(t) ~= 'string' or not isint(e)) then return nil end
  return {number = d.number, ticket = t, exp = e}
end
local function entry_json(number, ticket, exp)
  return cjson.encode({number = number, ticket = ticket or cjson.null, ticketExpiresAtMs = exp or cjson.null})
end
"""

# KEYS: 1=seq 2=user 3=serving / ARGV: 1=nowMs
# 반환: {created(0|1), number, serving, token|'' , expMs|0}
JOIN_LUA = _PARSE + """
local now = tonumber(ARGV[1])
local e = parse(redis.call('GET', KEYS[2]))
local created = 0
if e and (e.ticket == nil or now < e.exp) then
  -- 이미 줄을 섰고 입장권이 없거나 아직 유효: 같은 번호
else
  local n = redis.call('INCR', KEYS[1])
  redis.call('SET', KEYS[2], entry_json(n, nil, nil))
  e = {number = n}
  created = 1
end
local serving = tonumber(redis.call('GET', KEYS[3]) or '0') or 0
return {created, e.number, serving, e.ticket or '', e.exp or 0}
"""

# KEYS: 1=user 2=serving / ARGV: 1=nowMs 2=ttlMs 3=새 token 4=입장권 키 접두사 5=showId 6=userId 7=PX 여유
# 반환: {status, number, serving, token, expMs}; status = NONE|WAITING|ADMITTED|EXPIRED
ME_LUA = _PARSE + """
local now = tonumber(ARGV[1])
local e = parse(redis.call('GET', KEYS[1]))
if not e then return {'NONE', 0, 0, '', 0} end
local serving = tonumber(redis.call('GET', KEYS[2]) or '0') or 0
if e.ticket ~= nil then
  if now < e.exp then return {'ADMITTED', e.number, serving, e.ticket, e.exp} end
  return {'EXPIRED', e.number, serving, e.ticket, e.exp}
end
if e.number <= serving then
  local exp = now + tonumber(ARGV[2])
  redis.call('SET', ARGV[4] .. ARGV[3], cjson.encode({userId = ARGV[6], showId = ARGV[5], number = e.number, expiresAtMs = exp}),
             'PX', tonumber(ARGV[2]) + tonumber(ARGV[7]))
  redis.call('SET', KEYS[1], entry_json(e.number, ARGV[3], exp))
  return {'ADMITTED', e.number, serving, ARGV[3], exp}
end
return {'WAITING', e.number, serving, '', 0}
"""

_scripts: dict = {}
_scripts_for = None


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError
        return value
    except ValueError:
        log.error("%s=%r 는 양의 정수여야 한다. 기본값 %d 를 쓴다", name, raw, default)
        return default


def tick_ms() -> int:
    return _int_env("QUEUE_TICK_MS", 1000)


def batch() -> int:
    return _int_env("QUEUE_BATCH", 200)


def ticket_ttl_ms() -> int:
    return _int_env("QUEUE_TICKET_TTL_MS", 120_000)


def retry_after_max_s() -> int:
    return _int_env("QUEUE_RETRY_AFTER_MAX_S", 5)


class QueueUnavailable(Exception):
    """Redis 를 쓸 수 없다(꺼짐·시간 초과·REDIS_URL 없음·설정 오류). 호출한 쪽이 503 QUEUE_UNAVAILABLE 로 바꾼다."""


def _handle():
    h = cache._redis()
    if h is None:
        raise QueueUnavailable("Redis 사용 불가")
    global _scripts, _scripts_for
    if _scripts_for is not h:
        _scripts = {"advance": h.client.register_script(ADVANCE_LUA), "join": h.client.register_script(JOIN_LUA),
                    "me": h.client.register_script(ME_LUA)}
        _scripts_for = h
    return h


def _k(show_id: str, name: str) -> str:
    return f"{cache.prefix()}queue:{show_id}:{name}"


def _text(v) -> str:
    return v.decode("utf-8") if isinstance(v, (bytes, bytearray)) else str(v)


def advance(show_id: str, now_ms: int) -> None:
    h = _handle()
    try:
        _scripts["advance"](keys=[_k(show_id, "seq"), _k(show_id, "serving"), _k(show_id, "tick")],
                            args=[now_ms // tick_ms(), batch()])
    except (redis.RedisError, OSError) as e:
        raise QueueUnavailable(str(e)) from e


def _status(number: int, serving: int, token: str, exp_ms: int, status: str | None = None) -> dict:
    ahead = max(0, number - serving - 1)
    if status is None:
        status = "ADMITTED" if token else "WAITING"
    retry = 0
    if status == "WAITING":
        # clamp(ceil(ceil(ahead / BATCH) × TICK_MS / 1000), 1, MAX)
        retry = min(retry_after_max_s(), max(1, math.ceil(math.ceil(ahead / batch()) * tick_ms() / 1000)))
    return {"number": number, "serving": serving, "ahead": ahead, "status": status, "retryAfterSeconds": retry,
            "token": token or None, "expMs": exp_ms or None}


def join(show_id: str, user: str, now: datetime):
    """(생성 여부, 상태 dict)."""
    now_ms = cache.ms(now)
    advance(show_id, now_ms)
    h = _handle()
    try:
        created, number, serving, token, exp = _scripts["join"](
            keys=[_k(show_id, "seq"), _k(show_id, f"user:{user}"), _k(show_id, "serving")], args=[now_ms])
    except (redis.RedisError, OSError) as e:
        raise QueueUnavailable(str(e)) from e
    token = _text(token)
    return bool(created), _status(int(number), int(serving), token, int(exp))


def me(show_id: str, user: str, now: datetime):
    """상태 dict, 줄 선 적이 없으면 None."""
    now_ms = cache.ms(now)
    advance(show_id, now_ms)
    h = _handle()
    import uuid
    try:
        status, number, serving, token, exp = _scripts["me"](
            keys=[_k(show_id, f"user:{user}"), _k(show_id, "serving")],
            args=[now_ms, ticket_ttl_ms(), uuid.uuid4().hex, f"{cache.prefix()}ticket:", show_id, user, 60_000])
    except (redis.RedisError, OSError) as e:
        raise QueueUnavailable(str(e)) from e
    status = _text(status)
    if status == "NONE":
        return None
    return _status(int(number), int(serving), _text(token), int(exp), status)


def iso_from_ms(ms_value: int) -> str:
    return datetime.fromtimestamp(ms_value / 1000, tz=timezone.utc).isoformat()


# ------------------------------ 입장권 검증(읽기만) ------------------------------
def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def check_ticket(token: str | None, user: str, show_id: str, now: datetime):
    """None=통과, 아니면 실패 code(QUEUE_TICKET_*). Redis 를 쓸 수 없으면 QueueUnavailable.
    헤더가 없으면 Redis 에 묻지 않고 REQUIRED. 만료 시각은 늘리지 않는다(GET 만)."""
    if not token:
        return "QUEUE_TICKET_REQUIRED"
    h = cache._redis()
    if h is None:
        raise QueueUnavailable("Redis 사용 불가")
    try:
        raw = h.client.get(f"{cache.prefix()}ticket:{token}")
    except (redis.RedisError, OSError) as e:
        raise QueueUnavailable(str(e)) from e
    if raw is None:
        return "QUEUE_TICKET_INVALID"
    try:  # 경계: UTF-8 → JSON → 형식. 하나라도 틀리면 INVALID.
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "QUEUE_TICKET_INVALID"
    if not isinstance(data, dict):
        return "QUEUE_TICKET_INVALID"
    number, expires = data.get("number"), data.get("expiresAtMs")
    if (not isinstance(data.get("userId"), str) or not isinstance(data.get("showId"), str)
            or not _is_int(number) or number < 1 or not _is_int(expires)):
        return "QUEUE_TICKET_INVALID"
    if data["userId"] != user or data["showId"] != show_id:
        return "QUEUE_TICKET_INVALID"
    if cache.ms(now) >= data["expiresAtMs"]:
        return "QUEUE_TICKET_EXPIRED"
    return None
