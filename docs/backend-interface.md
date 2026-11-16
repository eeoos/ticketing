# 백엔드 ↔ 테스트 인터페이스 (MVP 서버)

API 동작은 `docs/openapi.yaml`이 기준이다. 이 문서는 그 밖에 **테스트(test)와 구현(server)이 함께 지켜야 할 약속**만 정한다. 바꿀 일이 생기면 직접 고치지 말고 lead에게 큐로 요청한다.

## 실행
```bash
docker compose up -d --wait   # PostgreSQL 17 (localhost:5432, user/password/db = ticketing)
uv sync
uv run pytest                 # tests/contract + tests/api 전체
```

## 파일 소유
| 자리 | 파일 |
|---|---|
| test | `tests/api/**` (conftest.py 포함). `tests/contract/`는 이번에 고치지 않는다. |
| server | `backend/**` (앱, 마이그레이션, 시드) |
| lead | `pyproject.toml`, `uv.lock`, `docker-compose.yml`, `docs/**`, `scripts/**` |

의존성 추가가 필요하면 lead에게 큐로 요청한다(`uv add`는 lead가 한다). 지금 들어 있는 것: fastapi, uvicorn, psycopg[binary] / 개발용 pytest, pyyaml, httpx.

## server가 제공할 진입점
| 위치 | 약속 |
|---|---|
| `backend/main.py` 의 `app` | FastAPI 앱. 테스트는 `fastapi.testclient.TestClient(app)`로 부른다. |
| `backend/main.py` 의 `get_now()` | 현재 시각(UTC, timezone-aware `datetime`)을 돌려주는 FastAPI 의존성. **홀드 만료 계산과 좌석맵 상태 계산은 모두 이 값을 쓴다**(DB의 `now()` 금지). 테스트는 `app.dependency_overrides[get_now]`로 시각을 바꿔 5분 경과를 흉내 낸다. |
| `backend/migrate.py` 의 `run(dsn: str \| None = None)` | `backend/migrations/*.sql`을 이름 순서로 적용해 테이블을 만든다. 여러 번 불러도 안전해야 한다. `uv run python -m backend.migrate`로도 실행된다. |
| `backend/seed.py` 의 `run(dsn: str \| None = None)` | 데이터를 **초기화**한다: 예매·홀드·좌석·공연을 모두 지우고 공연 1개와 그 공연의 좌석 100석(모두 AVAILABLE)을 넣는다. `uv run python -m backend.seed`로도 실행된다. |
| DB 연결 | `dsn`이 없으면 환경변수 `DATABASE_URL`, 그것도 없으면 `postgresql://ticketing:ticketing@localhost:5432/ticketing`. 앱은 `DATABASE_URL`을 import 시점이 아니라 **연결할 때** 읽는다. |

## 동작 약속 (계약 보충)
- `X-User-Id`가 없거나 빈 값이면 **401** `{code: UNAUTHENTICATED}`. FastAPI 기본 422를 내면 안 된다.
- `POST /bookings` 본문이나 `Idempotency-Key`가 잘못되면, 또는 콜백 본문이 잘못되면 **400** `{code: INVALID_REQUEST}`. 422 금지.
- 홀드 만료 시각 = 홀드 시각 + 5분. `now >= expiresAt`이면 만료.
- 같은 좌석에 동시에 홀드하면 정확히 하나만 201. 이 보장은 **DB에서** 해야 한다(조건부 UPDATE나 행 잠금). 테스트는 여러 스레드에서 동시에 요청할 수 있으므로 앱은 요청마다 별도 연결(또는 풀)을 써야 한다.
- 공연·좌석 id는 문자열이다. 테스트는 id를 하드코딩하지 않고 `GET /shows`, `GET /shows/{id}/seats`로 찾는다.

## 테스트 쪽 약속
- 테스트 DB는 `TEST_DATABASE_URL`(기본 `postgresql://ticketing:ticketing@localhost:5432/ticketing_test`). 개발용 `ticketing` DB를 지우지 않는다.
- 테스트는 `backend`를 import하기 전에 `DATABASE_URL`을 테스트 DB로 맞추고, DB가 없으면 만든다.
- 각 테스트 전에 `migrate.run()`과 `seed.run()`을 불러 시드 상태(공연 1개·좌석 100석)에서 시작한다.
- 테스트 이름은 `test_usNN_acM_…` 형식으로 스토리·인수 조건을 표시한다(`scripts/story_report.py`가 이 이름을 쓴다).

## 결제·만료 (6장) — 상태 전이와 DB 불변식
API 형식은 `docs/openapi.yaml`, 정책은 `docs/PRD.md` '홀드 정책'·'모의 결제', 인수 조건은 US-03~US-05.

### 시각
- 홀드 만료, 결제 요청 판정, 콜백의 "홀드 유효" 판정은 **모두 `get_now()` 값**으로 한다. DB `now()`, 클라이언트 헤더·본문, 콜백 본문의 시각은 쓰지 않는다.
- 테스트는 `app.dependency_overrides[get_now]`로 시각을 정한다(콜백 엔드포인트에도 적용되어야 한다). 실제 서버(tests/concurrency)는 실제 시계.

### 좌석 상태 (seats)
| 현재 | 사건 | 다음 |
|---|---|---|
| AVAILABLE, 또는 HELD이고 `now ≥ hold_expires_at` | 홀드 성공 | HELD, **새 `hold_id`**, `hold_user_id`, `hold_expires_at = now + 5분` |
| HELD이고 `now < hold_expires_at` | 홀드 (본인 포함) | 변화 없음 (409 `SEAT_HELD`) — 연장 없음 |
| HELD, `hold_id = 예매.hold_id`, `now < hold_expires_at` | 그 예매의 SUCCEEDED 콜백 | SOLD (홀드 정보는 남겨도 되지만 SOLD는 다시 잡히지 않는다) |
| 그 밖 | 콜백 | 좌석 변화 없음 |

### 예매 상태 (bookings = 결제 시도 1건)
```
            ┌── FAILED 콜백 ──▶ FAILED ── SUCCEEDED 콜백 ──▶ REFUND_REQUIRED
PENDING ────┤
            └── SUCCEEDED 콜백 ─┬─ 홀드 유효 ──▶ CONFIRMED (+ 좌석 SOLD)
                                └─ 아니면 ────▶ REFUND_REQUIRED
```
- 종료 상태에 같은 결과가 다시 오면 변화 없음(`applied: false`). CONFIRMED·REFUND_REQUIRED에 FAILED가 오면 409 `CALLBACK_CONFLICT`, 변화 없음.
- "홀드 유효"는 예매가 묶인 `hold_id`가 **좌석의 현재 `hold_id`**이고 아직 만료 전이라는 뜻이다. 만료 뒤 누가(본인 포함) 새로 홀드하면 `hold_id`가 바뀌므로 이전 결제의 콜백은 새 홀드에 영향을 주지 않는다.
- 결제 실패는 홀드를 풀지 않는다. 같은 홀드로 새 키를 써서 재시도할 수 있다.

### DB 불변식 (server가 DB 제약으로 보장)
1. **좌석당 CONFIRMED 예매는 최대 하나** — `bookings(seat_id) WHERE status = 'CONFIRMED'` 조건부 UNIQUE. 기존 `bookings_seat_id_key`(전체 UNIQUE)는 실패·환불 필요 기록과 재시도 때문에 이것으로 **대체**한다.
2. **홀드당 PENDING 예매는 최대 하나** — `bookings(hold_id) WHERE status = 'PENDING'` 조건부 UNIQUE.
3. **멱등 키는 사용자별로 하나** — `(user_id, idempotency_key)` UNIQUE. 같은 키로 저장된 최초 202 응답(상태 코드·본문)과 요청 지문(정규화한 `seatId`)을 함께 저장한다.
4. `payment_id`는 UNIQUE.
5. 좌석이 SOLD이면 그 좌석의 CONFIRMED 예매가 정확히 하나 있고, CONFIRMED 예매의 좌석은 SOLD다.

### 동시성 약속
- 같은 사용자·같은 키·같은 요청이 여러 워커에서 동시에 와도 예매는 하나, 응답은 모두 같은 예매의 202, 5xx 0건.
- 같은 홀드에 서로 다른 키가 동시에 오면 202는 하나, 나머지는 409 `PAYMENT_IN_PROGRESS`.
- 같은 콜백이 동시에 여러 번 와도 `applied: true`는 정확히 한 번. SUCCEEDED와 FAILED가 동시에 와도 위 표의 어느 한 순서로 처리한 결과와 같고, 불변식 5가 깨지지 않는다.
- 이 보장은 DB(행 잠금·조건부 UNIQUE·트랜잭션)로 한다. UNIQUE 위반이 5xx로 새어 나가면 안 된다(재조회해 계약 응답으로 바꾼다).

### 콜백 서명
- `X-Mock-Pg-Signature` = `hmac.new(MOCK_PG_SECRET.encode(), 원문 본문 바이트, sha256).hexdigest()`. `MOCK_PG_SECRET` 환경변수, 기본 `mock-pg-dev-secret`. 비교는 상수 시간(`hmac.compare_digest`). 서명 검사는 본문 해석보다 먼저.
- 테스트와 모의 결제사는 같은 방식으로 서명한다. 실제 서버 테스트는 하위 uvicorn 에 같은 값을 넘긴다.

### 마이그레이션
- `001_init.sql`은 **고치지 않는다.** 변경은 `backend/migrations/002_*.sql` 이후 새 파일로 추가하고, `migrate.run()`은 적용 안 된 것만 순서대로 적용한다(`schema_migrations`).
- 데이터가 있는 기존 DB(개발 DB `ticketing`)에도 적용되어야 한다: 기존 예매 행은 `CONFIRMED`로, 기존 홀드 중인 좌석은 새 `hold_id`를 받게 하고, 기존 UNIQUE를 지운 뒤 조건부 UNIQUE를 만든다.
- 기존 예매 행은 API 응답 필수 필드를 채울 수 있게 새 `payment_id`·`hold_id`(과거 홀드를 나타내는 새 값)·`updated_at`을 받는다. 기존 SOLD 좌석과 그 CONFIRMED 예매의 짝(불변식 5)이 유지되어야 한다.
- lead가 6장 시작 시점의 개발 DB(옛 구조 + 홀드 1건·확정 예매 1건)를 덤프해 두었다: `/private/tmp/claude-501/lead-notes/ticketing-pre-ch6.sql`. 이 덤프를 새 DB에 복원한 뒤 `migrate.run()`으로 올라가는지로 확인한다.
- `seed.run()`은 새 테이블·열까지 모두 비우고 시드 상태로 되돌린다.

### 진입점 추가
| 위치 | 약속 |
|---|---|
| `backend/main.py` `get_now()` | 콜백 판정에도 쓴다. |
| 환경변수 `MOCK_PG_SECRET` | 콜백 서명 키(기본 `mock-pg-dev-secret`). |

## 가상 대기열·입장권 (9장)
상세 계약은 `docs/queue.md`. 요점: 좌석맵 조회와 홀드는 `X-User-Id` + 유효한 입장권(`X-Queue-Ticket`)이 필요하고, 입장권 검증이 좌석 캐시·홀드 관문보다 먼저다. 대기열·입장권은 Redis에만 있고, Redis를 쓸 수 없으면(`REDIS_URL` 없음 포함) 503 `QUEUE_UNAVAILABLE`로 우회하지 않는다. 그래서 좌석맵·홀드를 부르는 테스트는 테스트용 Redis(개발 DB 0·`tkt:`와 분리)를 주고, `POST /shows/{id}/queue` → `get_now` 앞당김 → `GET /shows/{id}/queue/me`로 입장권을 얻어 붙인다.
