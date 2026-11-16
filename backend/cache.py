"""Redis: 좌석맵 캐시 + 홀드 관문 (docs/cache.md). Redis 는 빠른 길이고 기준은 PostgreSQL 이다.

- 모든 Redis 호출은 실패해도 예외를 밖으로 내지 않는다(None/BYPASS 로 돌려 DB 만으로 처리).
- 클라이언트·스크립트는 프로세스(PID)마다 하나, 처음 쓸 때(fork 이후) 만든다. 스크립트는 EVALSHA, NOSCRIPT 면 재적재(redis-py Script).
- 값은 JSON 문자열. 유효 시각은 get_now 기준 epoch ms 를 값에 넣고 읽을 때 get_now 와 다시 비교한다(PX 는 청소용).
"""
import json
import logging
import os
import threading
from datetime import datetime

import redis

log = logging.getLogger("backend.cache")

PENDING_PX_MS = 2000
SOLD_PX_MS = 60_000
HELD_PX_MARGIN_MS = 10_000
SEATMAP_TTL_S = 5

# 확인과 표시를 한 번에(Redis 는 스크립트를 단일 스레드로 직렬 실행). KEYS[1] = 관문 키.
# ARGV: 1=nowMs 2=내 PENDING 값(JSON) 3=PENDING PX
GATE_LUA = """
local v = redis.call('GET', KEYS[1])
if v then
  local ok, d = pcall(cjson.decode, v)
  if ok and type(d) == 'table' then
    local st = d.state
    if st == 'SOLD' then return {'REJECT', 'SEAT_SOLD'} end
    if st == 'HELD' and tonumber(d.expiresAtMs) > tonumber(ARGV[1]) then return {'REJECT', 'SEAT_HELD'} end
    if st == 'PENDING' then return {'REJECT', 'SEAT_HELD'} end
  end
end
redis.call('SET', KEYS[1], ARGV[2], 'PX', ARGV[3])
return {'PASS'}
"""

# 값의 token 이 내 것일 때만 HELD 로. ARGV: 1=token 2=HELD 값(JSON) 3=PX
CONFIRM_LUA = """
local v = redis.call('GET', KEYS[1])
if v then
  local ok, d = pcall(cjson.decode, v)
  if ok and type(d) == 'table' and d.token == ARGV[1] then
    redis.call('SET', KEYS[1], ARGV[2], 'PX', ARGV[3])
    return 1
  end
end
return 0
"""

# 값의 token 이 내 것일 때만 지움. ARGV: 1=token
RELEASE_LUA = """
local v = redis.call('GET', KEYS[1])
if v then
  local ok, d = pcall(cjson.decode, v)
  if ok and type(d) == 'table' and d.token == ARGV[1] then
    redis.call('DEL', KEYS[1])
    return 1
  end
end
return 0
"""

# DB 를 읽기 전에 본 버전과 같을 때만 저장. KEYS: 1=항목 2=버전. ARGV: 1=기대 버전 2=값 3=PX
PUT_LUA = """
local ver = redis.call('GET', KEYS[2]) or '0'
if ver == ARGV[1] then
  redis.call('SET', KEYS[1], ARGV[2], 'PX', ARGV[3])
  return 1
end
return 0
"""


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


SEAT_STATUSES = ("AVAILABLE", "HELD", "SOLD")


class _Handle:
    def __init__(self, url: str, timeout_ms: str):
        # 설정 오류(ValueError: 비정수/0 이하 timeout, 잘못된 URL)는 호출한 쪽(_redis)이 로그를 남기고 기능을 끈다.
        timeout = int(timeout_ms) / 1000
        if timeout <= 0:
            raise ValueError(f"REDIS_TIMEOUT_MS 는 양의 정수여야 한다: {timeout_ms!r}")
        # 값 디코딩은 우리가 경계에서 직접 한다(decode_responses=False): 손상된 UTF-8 이 예외로 새지 않게.
        self.client = redis.Redis.from_url(url, socket_timeout=timeout, socket_connect_timeout=timeout)
        self.gate = self.client.register_script(GATE_LUA)
        self.confirm = self.client.register_script(CONFIRM_LUA)
        self.release = self.client.register_script(RELEASE_LUA)
        self.put = self.client.register_script(PUT_LUA)


_handle: _Handle | None = None
_handle_key: tuple | None = None
_lock = threading.Lock()


def _redis() -> _Handle | None:
    """REDIS_URL 이 없으면 None(기능 끔). 설정 오류면 한 번 로그를 남기고 None(BYPASS) — 요청을 500 으로 만들지 않는다.
    프로세스·설정이 바뀌면 새로 만든다."""
    global _handle, _handle_key
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    key = (os.getpid(), url, os.environ.get("REDIS_TIMEOUT_MS", "200"))
    if _handle_key == key:
        return _handle
    with _lock:
        if _handle_key != key:
            try:
                _handle = _Handle(url, key[2])
            except (ValueError, redis.RedisError) as e:
                log.error("Redis 설정 오류로 Redis 기능을 끈다(BYPASS): %s: %s", type(e).__name__, e)
                _handle = None
            _handle_key = key
        return _handle


def check_config() -> None:
    """시작 시 설정을 한 번 검증해 오류를 로그로 남긴다(실패해도 앱은 BYPASS 로 동작)."""
    _redis()


def prefix() -> str:
    return os.environ.get("REDIS_KEY_PREFIX", "tkt:")


def enabled() -> bool:
    return bool(os.environ.get("REDIS_URL"))


def gate_enabled() -> bool:
    return enabled() and os.environ.get("HOLD_GATE", "on").lower() != "off"


_ERRORS = (redis.RedisError, OSError)


# ------------------------------ 좌석맵 캐시 ------------------------------
def _valid_seat(e) -> bool:
    return (isinstance(e, dict) and all(isinstance(e.get(k), str) for k in ("id", "section", "row"))
            and isinstance(e.get("number"), int) and not isinstance(e.get("number"), bool)
            and e.get("status") in SEAT_STATUSES)


def parse_seatmap_entry(raw: bytes | None):
    """캐시 값 → (validUntilMs, body). 디코딩·JSON·필드·형식이 하나라도 틀리면 None(없는 것으로 본다).
    경계: UTF-8 디코딩(UnicodeDecodeError), JSON 해석(ValueError), 구조 검증(좌석맵 형식)."""
    if not raw:
        return None
    try:
        entry = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(entry, dict):
        return None
    valid_until, body = entry.get("validUntilMs"), entry.get("body")
    if not isinstance(valid_until, int) or isinstance(valid_until, bool):
        return None
    if not isinstance(body, list) or not all(_valid_seat(e) for e in body):
        return None
    return valid_until, body


def seatmap_read(show_id: str, now: datetime):
    """(상태, 본문, 버전). 상태: HIT | MISS | BYPASS. MISS 면 버전을 들고 가서 DB 읽기 뒤 seatmap_put 에 쓴다."""
    h = _redis()
    if h is None:
        return "BYPASS", None, None
    try:
        entry_raw, ver = h.client.mget(f"{prefix()}seatmap:{show_id}", f"{prefix()}seatmap-ver:{show_id}")
    except _ERRORS:
        return "BYPASS", None, None
    parsed = parse_seatmap_entry(entry_raw)
    if parsed and ms(now) < parsed[0]:
        return "HIT", parsed[1], None
    return "MISS", None, ver or b"0"  # 버전은 바이트 그대로 Lua 에서 문자열 비교


def seatmap_put(show_id: str, body: list, valid_until: datetime, now: datetime, ver: str) -> None:
    valid_ms = ms(valid_until)
    if valid_ms <= ms(now):
        return
    h = _redis()
    if h is None:
        return
    try:
        h.put(keys=[f"{prefix()}seatmap:{show_id}", f"{prefix()}seatmap-ver:{show_id}"],
              args=[ver, json.dumps({"validUntilMs": valid_ms, "body": body}, separators=(",", ":")),
                    valid_ms - ms(now) + 5000])
    except _ERRORS:
        pass


def seatmap_invalidate(show_id: str) -> None:
    """DB 커밋 뒤에 부른다: 버전 INCR + 항목 DEL 을 한 트랜잭션(MULTI/EXEC)으로."""
    h = _redis()
    if h is None:
        return
    try:
        pipe = h.client.pipeline(transaction=True)
        pipe.incr(f"{prefix()}seatmap-ver:{show_id}")
        pipe.delete(f"{prefix()}seatmap:{show_id}")
        pipe.execute()
    except _ERRORS:
        pass


# ------------------------------ 홀드 관문 ------------------------------
def _hold_value(state: str, owner: str, token: str, expires_ms: int) -> str:
    return json.dumps({"state": state, "owner": owner, "token": token, "expiresAtMs": expires_ms},
                      separators=(",", ":"))


def gate(seat_id: str, user: str, token: str, now: datetime):
    """('PASS'|'REJECT'|'BYPASS', 409 code | None)."""
    if not gate_enabled():
        return "BYPASS", None
    h = _redis()
    if h is None:
        return "BYPASS", None
    try:
        res = h.gate(keys=[f"{prefix()}hold:{seat_id}"],
                     args=[ms(now), _hold_value("PENDING", user, token, 0), PENDING_PX_MS])
    except _ERRORS:
        return "BYPASS", None
    outcome = res[0].decode("ascii")
    return outcome, (res[1].decode("ascii") if len(res) > 1 else None)


def gate_confirm(seat_id: str, user: str, token: str, now: datetime, expires: datetime) -> None:
    h = _redis()
    if h is None:
        return
    try:
        h.confirm(keys=[f"{prefix()}hold:{seat_id}"],
                  args=[token, _hold_value("HELD", user, token, ms(expires)),
                        max(ms(expires) - ms(now), 0) + HELD_PX_MARGIN_MS])
    except _ERRORS:
        pass


def gate_release(seat_id: str, token: str) -> None:
    h = _redis()
    if h is None:
        return
    try:
        h.release(keys=[f"{prefix()}hold:{seat_id}"], args=[token])
    except _ERRORS:
        pass


def mark_sold(seat_id: str, user: str) -> None:
    """결제 성공으로 CONFIRMED 가 커밋된 뒤: 관문 표시를 SOLD(PX 60s)로. 60초 뒤에는 다시 DB 가 답한다."""
    if not gate_enabled():
        return
    h = _redis()
    if h is None:
        return
    try:
        h.client.set(f"{prefix()}hold:{seat_id}", _hold_value("SOLD", user, "", 0), px=SOLD_PX_MS)
    except _ERRORS:
        pass
