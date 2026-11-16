# 가상 대기열과 입장권 (9장)

공연마다 Redis 대기열을 두고, 차례가 된 사용자에게 2분짜리 입장권을 준다. **좌석맵 조회와 홀드는 입장권이 있어야 한다.** API 형식은 `docs/openapi.yaml`(v0.5.0), 정책은 `docs/PRD.md` '가상 대기열', 인수 조건은 US-06·US-07. 캐시·관문은 `docs/cache.md`.

## 파일 소유
| 자리 | 파일 |
|---|---|
| lead | `docs/**`, 루트 설정 |
| server | `backend/**`(대기열·입장권·Lua) |
| test | `tests/queue/**`, 그리고 새 입장 정책에 맞춘 `tests/api`·`tests/contract`·`tests/concurrency`·`tests/cache`·`tests/infra` 수정 |

## 정책 (lead 결정)
| 항목 | 값 | 환경변수 |
|---|---|---|
| 입장 간격 | 1초(구간 = `floor(get_now ms / 1000)`) | `QUEUE_TICK_MS=1000` |
| 구간당 입장 인원 | 200명 (`docs/capacity.md`의 입장 속도 μ [가정]) | `QUEUE_BATCH=200` |
| 입장권 유효시간 | 발급 시점부터 2분. 다시 조회해도 늘어나지 않음 | `QUEUE_TICKET_TTL_MS=120000` |
| 대기 중 다시 물어볼 간격(Retry-After) | `clamp(ceil(ceil(앞사람 수 / QUEUE_BATCH) × QUEUE_TICK_MS / 1000), 1, 5)` 초 (기본값이면 `ceil(앞사람 수 / 200)`) | `QUEUE_RETRY_AFTER_MAX_S=5` |
| 입장권 전달 | 요청 헤더 `X-Queue-Ticket: <token>` | |
| 만료 후 재입장 | 입장권이 만료되면 그 번호는 끝난다. 다시 줄 서면(POST) **맨 뒤 새 번호** | |

## 번호표 — `POST /shows/{showId}/queue`
- `X-User-Id` 필수(없으면 401). 공연이 없으면 404 `NOT_FOUND`.
- Lua **한 번**으로: 사용자 항목 확인 → 없거나 입장권이 만료됐으면 `INCR`로 번호 발급 → 항목 저장. Redis는 스크립트를 직렬로 실행하므로 세 앱·여러 워커가 동시에 불러도 사용자당 번호는 하나이고, 발급한 번호는 모두 사용자에게 저장된다(낭비 없음).
- 같은 사용자가 다시 부르거나 두 탭에서 불러도 같은 번호 → 처음 201, 이후 200(같은 본문 형식). 입장권이 만료된 뒤 부르면 새 번호로 201.
- 응답은 `QueueStatus`(아래). 번호를 줄 때도 전광판 올리기(아래)를 먼저 한다.

## 전광판 (serving) — 요청이 올 때 올린다
- 백그라운드 작업 없음. 대기열 요청(POST, `/me`)마다 Lua `advance(show, nowTick)`를 먼저 부른다(`nowTick = floor(get_now ms / QUEUE_TICK_MS)`).
- `advance`: 마지막 처리 구간 `lastTick`이 없으면 `nowTick`으로 정하고 끝. `nowTick > lastTick`이면 `serving = min(serving + QUEUE_BATCH × (nowTick − lastTick), 발급된 마지막 번호)`, `lastTick = nowTick`. 같은 구간에 여러 앱·워커가 불러도 `nowTick > lastTick`은 한 번만 참이라 **중복으로 올라가지 않는다.**
- **발급된 번호를 넘지 않는다**: 한참 아무도 없다가 사람이 몰려도 지난 구간이 한꺼번에 쌓여 모두 바로 입장하지 않는다. 첫 사람은 다음 구간(최대 1초)에 입장한다.
- 테스트는 `get_now`를 앞당겨 구간을 움직인다(실제 시계를 기다리지 않음).

## 내 순서 — `GET /shows/{showId}/queue/me`
- `X-User-Id` 필수. 줄을 선 적이 없으면 404 `QUEUE_ENTRY_NOT_FOUND`.
- 전광판 올리기 → 내 항목 판단(Lua 한 번):
  - `number > serving` → **WAITING**: `ahead = number − serving − 1`, `retryAfterSeconds`, 응답 헤더 `Retry-After`. 입장권 없음.
  - `number ≤ serving`이고 입장권이 없으면 → **입장권 발급**: 새 token, `expiresAt = now + 2분` → **ADMITTED**.
  - 입장권이 있고 `now < expiresAt` → **ADMITTED**, 같은 token·같은 만료 시각(늘어나지 않음).
  - 입장권이 있고 `now ≥ expiresAt` → **EXPIRED**(다시 POST하면 새 번호).
- 차례가 왔는데 `/me`를 부르지 않으면 입장권은 발급되지 않고, 처음 부른 때부터 2분이 시작된다.

## 입장권 검증 — 좌석맵 조회·홀드
- 입장권은 Redis `{p}ticket:{token}`에 `{userId, showId, number, expiresAtMs}`로 저장되고 세 앱이 같은 Redis에서 검증한다.
- 검증: token 있음 ∧ 저장된 `userId == X-User-Id` ∧ `showId == 요청한 공연` ∧ `get_now < expiresAtMs`. **검증은 읽기만 한다(만료 시각을 늘리지 않음).**
- 헤더가 없으면 Redis에 묻지 않고 바로 403 `QUEUE_TICKET_REQUIRED`(Redis 장애 중이어도 403). 헤더가 있는데 Redis를 쓸 수 없으면 503.
- 실패 code: 헤더 없음 → 403 `QUEUE_TICKET_REQUIRED` / 모르는 token·다른 사용자·다른 공연 → 403 `QUEUE_TICKET_INVALID` / 만료 → 403 `QUEUE_TICKET_EXPIRED`.
- **순서**
  - 좌석맵 `GET /shows/{showId}/seats`: 401(X-User-Id) → 입장권 검증 → 좌석 캐시(`docs/cache.md`) → DB. 없는 공연의 입장권은 발급될 수 없으므로(줄 서기가 404) 없는 공연 조회는 입장권 검증에서 403(`REQUIRED`/`INVALID`)으로 끝난다. 404는 입장권이 유효한데 공연이 지워진 경우뿐이다.
  - 홀드 `POST /seats/{seatId}/hold`: 401 → 입력 검증(NUL·UTF-8) → 좌석이 속한 공연 찾기(좌석→공연 목록; 없는 좌석 404) → **입장권 검증** → Redis 홀드 관문 → DB. 좌석→공연은 바뀌지 않는 정보라 프로세스 안에 기억해 둬도 되며, 좌석 캐시·관문이 아니다. 있는 좌석만 기억한다(없는 좌석은 기억하지 않음). 같은 좌석의 첫 DB 조회를 하나로 모으는 잠금은 **고정 개수(64개)의 줄무늬 잠금**(좌석 id 해시로 고름)이라 요청한 id 수만큼 메모리가 늘지 않는다.
- 결제 요청·콜백·예매 조회·공연 목록은 입장권이 필요 없다.

## Redis 장애
- 좌석 캐시·홀드 관문: 지금처럼 DB로 우회(BYPASS).
- **대기열(POST·`/me`)과 입장권 검증: 우회하지 않는다.** Redis를 쓰지 못하면(꺼짐·시간 초과·`REDIS_URL` 없음·설정 오류) 503 `QUEUE_UNAVAILABLE` + `Retry-After: 5`. 입장권 없이 좌석맵·홀드로 들어가지 못한다.
- 손상된 대기열·입장권 값은 입장권이면 `QUEUE_TICKET_INVALID`, 대기열 항목이면 없는 것으로 본다(5xx 금지). **손상**의 뜻(필드 하나라도 해당하면):
  - UTF-8 아님, JSON 아님, 객체 아님.
  - 사용자 항목: `number`가 1 이상의 정수가 아님(bool·소수·음수·0·문자열 포함), `ticket`이 문자열도 null도 아님, `ticketExpiresAtMs`가 정수도 null도 아님, `ticket`과 `ticketExpiresAtMs` 중 하나만 null.
  - 입장권: `userId`·`showId`가 문자열이 아님, `number`가 1 이상의 정수가 아님(없음 포함), `expiresAtMs`가 정수가 아님.

## 알려진 한계
- 대기열 키(`seq`·`serving`·`tick`·사용자 항목)에는 TTL이 없다. 대기 중 임의로 지우면 순번 공정성이 깨지므로 공연 종료 뒤 정리 정책(보존·삭제)은 운영 전에 따로 정한다.
- 입장권 키는 만료 뒤 `PX` 여유(60초)가 지나면 청소된다. 그 뒤 같은 token은 `EXPIRED`가 아니라 `INVALID`로 보인다(둘 다 403).
- 입장권은 차례가 된 뒤 **처음 조회한 때부터** 2분이다. 늦게 조회한 과거 순번이 한꺼번에 들어올 수 있으므로 전광판은 "동시에 좌석을 보는 인원 상한"이 아니라 입장 자격 순번이다.
- Lua(cjson)는 JSON `1.0`과 `1`을 구분하지 못해 사용자 항목의 `number: 1.0`을 정수로 받아들인다(파이썬 쪽 입장권 검사는 거절). 정상 경로는 항상 정수를 쓴다.
- 이미 기억한 공연 존재·좌석→공연은 런타임에 지워지지 않는다(관리 API 없음, 시드는 새 id를 만든다).

## Redis 키와 값 (JSON 문자열, 테스트가 읽을 수 있음)
| 키 | 값 |
|---|---|
| `{p}queue:{showId}:seq` | 마지막 발급 번호(정수, `INCR`) |
| `{p}queue:{showId}:serving` | 전광판(정수) |
| `{p}queue:{showId}:tick` | 마지막 처리 구간(정수) |
| `{p}queue:{showId}:user:{userId}` | `{"number":int, "ticket":str|null, "ticketExpiresAtMs":int|null}` |
| `{p}ticket:{token}` | `{"userId":str, "showId":str, "number":int, "expiresAtMs":int}` (`PX` = 남은 시간 + 여유, 청소용) |

`{p}` = `REDIS_KEY_PREFIX`. 시각 규칙은 `docs/cache.md`와 같다(값의 `get_now` ms로 판단, `PX`는 청소용).

## 테스트에서 쓰는 법
- 입장권 얻기: `POST /shows/{id}/queue` → `get_now`를 다음 구간 이후로 → `GET /shows/{id}/queue/me` → `ticket.token`. 실제 시계 서버(tests/concurrency 등)는 1초 안팎 기다린 뒤 `/me`.
- 테스트 DB와 Redis(DB 번호·접두사)는 개발용(`ticketing`, Redis DB 0, `tkt:`)과 분리. `REDIS_URL`이 없는 테스트 서버는 이제 좌석맵·홀드가 503이므로, 기존 tests/api·concurrency도 테스트용 Redis를 주고 입장권을 붙인다.
