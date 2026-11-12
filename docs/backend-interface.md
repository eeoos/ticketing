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
- `POST /bookings` 본문이 잘못되면(`seatId` 없음 등) **400** `{code: INVALID_REQUEST}`. 422 금지.
- 홀드 만료 시각 = 홀드 시각 + 5분. `now >= expiresAt`이면 만료.
- 같은 좌석에 동시에 홀드하면 정확히 하나만 201. 이 보장은 **DB에서** 해야 한다(조건부 UPDATE나 행 잠금). 테스트는 여러 스레드에서 동시에 요청할 수 있으므로 앱은 요청마다 별도 연결(또는 풀)을 써야 한다.
- 공연·좌석 id는 문자열이다. 테스트는 id를 하드코딩하지 않고 `GET /shows`, `GET /shows/{id}/seats`로 찾는다.

## 테스트 쪽 약속
- 테스트 DB는 `TEST_DATABASE_URL`(기본 `postgresql://ticketing:ticketing@localhost:5432/ticketing_test`). 개발용 `ticketing` DB를 지우지 않는다.
- 테스트는 `backend`를 import하기 전에 `DATABASE_URL`을 테스트 DB로 맞추고, DB가 없으면 만든다.
- 각 테스트 전에 `migrate.run()`과 `seed.run()`을 불러 시드 상태(공연 1개·좌석 100석)에서 시작한다.
- 테스트 이름은 `test_usNN_acM_…` 형식으로 스토리·인수 조건을 표시한다(`scripts/story_report.py`가 이 이름을 쓴다).
