# Redis: 좌석맵 캐시와 홀드 관문 (8장)

세 앱이 같은 Redis를 쓴다. Redis는 **빠른 길**일 뿐이고 **기준은 PostgreSQL**이다. 좌석 상태 변경과 중복 홀드·판매 방지는 언제나 DB가 정한다. 예외: 관문이 DB 없이 거절한 409의 `code`(`SEAT_HELD`/`SEAT_SOLD`)는 Redis 표시를 근거로 한 **잠정 값**이다(아래 '알려진 한계'). API 형식은 `docs/openapi.yaml`(v0.4.0), 인프라는 `docs/infra.md`.

## 파일 소유
| 자리 | 파일 |
|---|---|
| lead | `docker-compose.yml`(redis), `pyproject.toml`/`uv.lock`(redis-py), `docs/**` |
| server | `backend/**`(캐시·관문·Lua 스크립트) |
| test | `tests/cache/**` |

## 설정
| 환경변수 | 뜻 | 기본 |
|---|---|---|
| `REDIS_URL` | 예: `redis://redis:6379/0`. **없으면 Redis 기능 전부 끔**(캐시·관문 없이 DB만, 지금까지와 같음) | 없음(compose 앱은 `redis://redis:6379/${APP_REDIS_DB:-0}`) |
| `REDIS_KEY_PREFIX` | 모든 키 앞에 붙임. 테스트는 실행마다 다른 접두사를 쓴다 | `tkt:` |
| `HOLD_GATE` | `on`/`off`. 관문만 끄기(캐시는 유지) — "관문 없는 경우" 비교용 | `on` |
| `REDIS_TIMEOUT_MS` | 연결·명령 시간 제한. Redis가 멈추거나 느리면 이 안에 포기하고 DB만으로 처리 | `200` |

- `REDIS_URL`이 없으면(기능 끔) 헤더는 `X-Cache: BYPASS`, `X-Hold-Gate: BYPASS`.
- Redis 값이 손상됐거나(UTF-8 아님, JSON 아님, 필드·형식이 위 값 형식과 다름) 캐시 `body`가 좌석맵 응답 형식이 아니면 **없는 것으로 보고 DB에서 계산**한다(MISS). 5xx·잘못된 200을 내면 안 된다.
- Redis 호출이 실패(연결 거부·시간 초과·스크립트 오류)하면 **그 요청은 Redis 없이** 처리한다: 캐시는 건너뛰고(`X-Cache: BYPASS`), 관문은 통과로 본다(`X-Hold-Gate: BYPASS`). Redis 오류가 5xx로 새면 안 된다.
- 개발 스택은 Redis DB 0, 접두사 `tkt:`. 테스트는 다른 DB 번호(1 이상)와 실행마다 다른 접두사를 쓰고, 끝나면 자기 키만 지운다.

## 값 형식 (테스트가 읽는 형식)
- 모든 값은 **JSON 문자열**(`SET`), 필드 이름은 아래 그대로.
- 캐시 `{p}seatmap:{showId}` = `{"validUntilMs": <int>, "body": [<좌석맵 응답 배열>]}`
- 관문 `{p}hold:{seatId}` = `{"state": "PENDING|HELD|SOLD", "owner": <X-User-Id>, "token": <str>, "expiresAtMs": <int, HELD일 때 DB 만료, 그 밖 0>}`
- 버전 `{p}seatmap-ver:{showId}` = 정수(`INCR`).

## 시각 규칙 (중요)
테스트는 `get_now()`를 바꾸지만 Redis TTL은 실제 시계로 흐른다. 그래서 **정확성은 Redis TTL에 기대지 않는다.**
- 캐시 항목과 관문 표시는 `get_now()` 기준 유효 시각(epoch ms)을 값 안에 함께 저장하고, 읽을 때 `get_now()`와 다시 비교한다.
- Redis `PX` TTL은 청소용이다(같거나 더 긴 시간). TTL이 남아 있어도 저장된 유효 시각이 지났으면 없는 것으로 본다.
- **예외: `PENDING` 표시**는 진행 중 요청의 짧은 임대(lease)라 실제 시계 `PX = 2초`로만 사라진다. 가짜 시계를 몇 분 앞당겨도 `PENDING`은 실제 2초 동안 남을 수 있다.

> 9장부터 좌석맵·홀드는 입장권 검증(`docs/queue.md`)을 **먼저** 통과해야 캐시·관문에 닿는다. 입장권 검증은 Redis 장애 때 우회하지 않는다(503).

## ① 좌석맵 캐시 (캐시 어사이드) — `GET /shows/{showId}/seats`
- 키 `{p}seatmap:{showId}` = `{validUntilMs, body}`. 응답 헤더 `X-Cache: HIT | MISS | BYPASS`.
- **읽기**: 항목이 있고 `now < validUntil`이면 HIT(그대로 응답). 아니면 DB에서 계산(MISS) 후 저장. 공연이 없으면(404) 저장하지 않는다.
- **유효기간**: `validUntil = min(now + 5초, 이 응답에 HELD로 나간 좌석들의 hold_expires_at 중 가장 이른 값)`. 그래서 만료 시각이 되는 순간부터 그 좌석은 HIT에서도 AVAILABLE로 보인다(그 시각이 되면 항목이 무효). `validUntil ≤ now`면 저장하지 않는다.
- **무효화**: 좌석 상태를 바꾸는 트랜잭션이 **커밋된 뒤** 그 공연의 항목을 지운다 — 홀드 성공, 결제 성공 콜백으로 CONFIRMED(좌석 SOLD). (FAILED·REFUND_REQUIRED·결제 요청 PENDING은 좌석을 바꾸지 않으므로 무효화 없음.)
- **경쟁 방지(버전 키)**: 공연별 `{p}seatmap-ver:{showId}`. 무효화 = 버전 INCR + 항목 DEL. 읽기 쪽은 **DB를 읽기 전에** 버전을 읽어 두고, 저장은 버전이 그대로일 때만 한다(Lua 한 번). 그래서 "DB를 읽은 뒤 무효화가 끼고 늦게 낡은 값을 저장"하는 경우가 생기지 않는다.
- 무효화 자체가 실패하면(Redis 오류) 낡은 항목이 최대 5초(또는 가장 이른 홀드 만료까지) 남을 수 있다 — 알려진 한계.

## ② 홀드 관문 (Lua) — `POST /seats/{seatId}/hold`
입력 검증(헤더·NUL·UTF-8) 뒤, DB에 가기 전에 Lua 스크립트 **한 번**으로 확인과 표시를 같이 한다. 응답 헤더 `X-Hold-Gate: PASS | REJECT | BYPASS`(DB까지 갔는지 보이는 진단 헤더, 201·404·409에. 401은 관문 전에 끝나 헤더 없음).

키 `{p}hold:{seatId}`, 값에 `state`, `owner`(X-User-Id), `token`(요청마다 새 값), `expiresAtMs`(get_now 기준).

| 단계 | 동작 |
|---|---|
| 관문 (Lua `gate`) | 값이 `HELD`이고 `expiresAtMs > nowMs` → **REJECT 409 `SEAT_HELD`**(본인 포함). `SOLD` → **REJECT 409 `SEAT_SOLD`**. `PENDING`(진행 중 요청이 있음, Redis TTL 살아 있음) → **REJECT 409 `SEAT_HELD`**. 그 밖(없음, 만료된 HELD) → `PENDING`(내 token, `PX = 2초`)로 표시하고 **PASS** |
| DB 성공(201) | Lua `confirm`: 값의 token이 내 것일 때만 `HELD`(owner, token, `expiresAtMs = DB의 hold_expires_at`)로 바꾸고 `PX = expiresAt − now + 여유`. 그다음 좌석맵 무효화 |
| DB 실패(409/404) | Lua `release`: 값의 token이 **내 것일 때만** 지운다(남의 표시는 건드리지 않음) |
| 예외·프로세스 사망 | `PENDING`은 2초 뒤 Redis TTL로 사라진다 |
| 결제 성공 콜백으로 CONFIRMED(좌석 SOLD) 커밋 뒤 | 그 좌석 키를 `SOLD`(`PX = 60초`)로 바꾼다. 60초 뒤에는 다시 DB가 답한다(DB 초기화 등으로 Redis가 틀려도 오래 막지 않게) |

- **Redis 표시가 DB 만료 뒤 새 홀드를 막지 않는다**: `HELD` 표시의 `expiresAtMs`가 DB `hold_expires_at`과 같고, 관문은 `get_now`로 비교하므로 만료 시각이 되는 순간 표시는 무시된다.
- **DB가 기준**: 관문을 통과해도 DB의 조건부 UPDATE가 최종 판단이다. 오류 code 계약(404 `NOT_FOUND`, 409 `SEAT_HELD`/`SEAT_SOLD`)은 그대로다.
- **알려진 한계(성능)**: DB 처리가 2초를 넘으면 `PENDING`이 사라져 다른 요청도 PASS할 수 있고, 첫 요청의 `confirm`은 token이 달라 `HELD` 표시를 남기지 못한다. 관문 PASS 뒤 DB가 409/404를 주면 내 표시만 지우고 DB 상태로 표시를 다시 세우지 않는다. 둘 다 정확성(DB가 판단)은 지키지만 그 홀드가 끝날 때까지 관문 효과가 줄어든다.
- **알려진 한계(code)**: Redis 데이터가 사라진 직후, 한 요청이 `PENDING`을 잡고 DB에서 `SOLD`를 확인하는 짧은 사이에 겹친 다른 요청은 `SEAT_SOLD` 대신 `SEAT_HELD`를 받을 수 있다(둘 다 409, 상태 변화 없음). 같은 이유로 관문 REJECT의 409 code는 DB가 아니라 표시를 근거로 한다.
- 관문은 홀드에만 쓴다. 결제 요청·콜백은 지금처럼 DB만 본다.

## 테스트에서 쓰는 법 (`tests/cache`)
- Redis: 호스트 `localhost:6379`(compose `redis` 서비스). 테스트는 `REDIS_URL=redis://localhost:6379/1` 이상 + 실행별 `REDIS_KEY_PREFIX`. 개발 키(DB 0, `tkt:`)는 건드리지 않는다. 끝나면 자기 접두사 키만 지운다.
- DB: 개발 DB와 분리(`tests/api`·`tests/concurrency`처럼 별도 DB).
- 앱 3대 공유 확인은 `docs/infra.md`의 테스트 절차(`APP_DB_NAME`, 그리고 `APP_REDIS_DB`로 앱이 테스트용 Redis DB를 보게)로 띄운다. 끝나면 기본값으로 되돌린다.
- Redis를 멈추는 테스트는 `docker compose stop redis` 후 끝나면(실패해도) `docker compose start redis`, 복구 실패는 teardown error.
- DB까지 간 요청 수: `X-Hold-Gate: PASS` 개수와 DB 쪽 지표(예: 테스트 DB의 `pg_stat_database.xact_commit + xact_rollback` 증가분)를 함께 보고, `HOLD_GATE=off`로 띄운 같은 시나리오와 비교한다.
