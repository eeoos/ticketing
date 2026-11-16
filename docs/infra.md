# 인프라: 앱 3대 + Nginx (7장)

같은 이미지로 띄운 앱 `app1`·`app2`·`app3` 앞에 Nginx를 두고 **8080 포트 하나**로 받는다. 세 앱은 PostgreSQL 서비스 `db`를 공유한다. 이 문서는 test·server·lead가 함께 지킬 약속이다. API 형식은 `docs/openapi.yaml`.

## 파일 소유
| 자리 | 파일 |
|---|---|
| lead | `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `deploy/nginx/nginx.conf`, `docs/**` |
| server | `backend/**` (예: `X-App-Id` 응답 헤더) |
| test | `tests/infra/**` |

## 서비스
| 서비스 | 이미지 | 역할 |
|---|---|---|
| `db` | `postgres:17-alpine` | 기존 그대로. 호스트 `localhost:5432` |
| `app1`·`app2`·`app3` | 같은 이미지(`Dockerfile`, 비루트 사용자 `app` uid 10001로 실행), 각자 `APP_ID=app1|app2|app3` | `uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers ${WORKERS}`. 컨테이너 안에서만 8000, 호스트에 직접 열지 않음 |
| `redis` | `redis:7.4-alpine` | 좌석맵 캐시·홀드 관문(`docs/cache.md`). 영속 저장 없음. 호스트 `localhost:6379`(테스트용) |
| `nginx` | `nginx:1.28-alpine` | 호스트 `8080` → 앱들. 설정 `deploy/nginx/nginx.conf` |

### 앱 환경변수 (세 앱 공통, `APP_ID`만 다름)
| 변수 | 값 |
|---|---|
| `DATABASE_URL` | `postgresql://ticketing:ticketing@db:5432/${APP_DB_NAME:-ticketing}` — 호스트는 `db`(서비스 이름), `localhost` 아님 |
| `MOCK_PG_SECRET` | `${MOCK_PG_SECRET:-mock-pg-dev-secret}` — 세 앱 같은 값 |
| `WORKERS` | `2` |
| `DB_POOL_MAX_SIZE` | `10` |
| `APP_ID` | `app1` / `app2` / `app3` |
| `REDIS_URL` | `redis://redis:6379/${APP_REDIS_DB:-0}` — 세 앱 같은 Redis |
| `REDIS_KEY_PREFIX` | `${APP_REDIS_KEY_PREFIX:-tkt:}` |

### DB 연결 예산
PostgreSQL `max_connections = 100`. 앱 연결 최대치 = 앱 3 × 워커 2 × 풀 10 = **60**. 남는 40은 테스트 fixture·`migrate`/`seed`·psql·샘플러 몫이다. 앱·워커·풀 크기를 바꾸면 이 곱이 80을 넘지 않게 한다.

### 마이그레이션
앱은 시작할 때 마이그레이션을 하지 않는다. 호스트에서 `uv run python -m backend.migrate`(DSN은 `localhost:5432`)로 적용한 뒤 앱을 띄운다.

## Nginx 동작 (게이트웨이 약속)
- **분산**: 세 앱에 라운드 로빈. 앱이 응답 헤더 `X-App-Id: app1|app2|app3`를 붙이고 Nginx는 그대로 전달한다.
- **속도 제한**: `X-User-Id` 값별로 초당 5건, burst 10(지연 없이 즉시 처리, 넘치면 거절). `X-User-Id`가 없는 요청(공연 목록·좌석맵 익명 조회, 결제 콜백)은 제한하지 않는다. 넘으면 **429**, 헤더 `Retry-After: 1`, 본문 `{"code":"RATE_LIMITED","message":"..."}`(`application/json`). 이 응답은 앱까지 가지 않으므로 `X-App-Id`가 없다.
- **재시도**: 앱 연결 실패·시간 초과·502/503/504일 때 **GET·HEAD만** 다음 앱으로 다시 보낸다. **GET·HEAD 외 메서드(홀드·결제 요청·콜백 POST)는 Nginx가 다른 앱으로 넘기지 않는다** — 요청을 보내기 전 연결 단계에서 실패해도 마찬가지다(`@no_retry` 위치, `proxy_next_upstream off`). Nginx 기본값은 연결 실패 시 POST도 다음 앱으로 넘기므로(요청을 아직 보내지 않았기 때문) 일부러 막았다. 그 대가로 앱이 죽은 직후 그 앱에 배정된 POST 하나는 502를 받고, 클라이언트가 같은 키로 다시 보낸다. 실패한 앱은 10초 동안 후보에서 빠진다(`max_fails=1 fail_timeout=10s`). 앱 연결 시간 초과는 짧게(2초).
- **앱 전부 실패 / POST가 죽은 앱에 간 경우**: **502**, 본문 `{"code":"UPSTREAM_UNAVAILABLE","message":"..."}`. 클라이언트는 같은 `Idempotency-Key`로 다시 보내면 된다(아래).
- **게이트웨이 자체 헬스체크**: `GET /nginx-health`(Nginx가 직접 200 `ok`, 앱으로 보내지 않음). compose 헬스체크가 이것을 쓰므로 백그라운드 요청이 라운드 로빈 순번을 소비하거나 앱을 실패로 표시하지 않는다. 앱 컨테이너 헬스체크는 앱 안에서 `127.0.0.1:8000/shows`를 직접 부른다(Nginx를 거치지 않음).
- **고정 주소**: 앱과 Nginx는 `gateway` 네트워크(172.30.10.0/24)에서 고정 IP를 쓴다(app1 .11, app2 .12, app3 .13, nginx .2). Nginx upstream은 이름이 아니라 이 IP를 가리킨다. 멈춘 앱은 연결 실패로 10초 제외되고, 다시 켜면 같은 주소로 돌아오므로 Nginx 재시작이 필요 없다. (처음엔 DNS `resolve`를 썼으나, 멈춘 앱 이름이 사라지며 Nginx가 요청 도중 후보 목록을 다시 만들어 GET 재시도가 'no live upstreams' 502로 끝나는 경우가 있었다.) db는 기존 `default` 네트워크에 그대로 있고 앱은 두 네트워크에 모두 붙는다.

## 같은 키 재시도 (다른 앱이 받아도)
멱등 저장은 DB(`bookings`의 `(user_id, idempotency_key)`)에 있으므로 어느 앱이 받아도 같다. 같은 사용자·같은 본문·같은 `Idempotency-Key`로 다시 보내면 다른 앱이 받아도 최초 응답(202, 같은 예매 id, `Idempotent-Replayed: true`)을 돌려주고 예매는 하나다.

## 띄우기·테스트에서 쓰는 법
```bash
docker compose up -d --build --wait            # db + app1~3 + nginx (개발 DB ticketing)
curl -i localhost:8080/shows                    # X-App-Id 확인
```
테스트(`tests/infra`)는 개발 DB와 분리한 DB를 쓴다.
1. 호스트에서 `INFRA_DATABASE_URL`(기본 `postgresql://ticketing:ticketing@localhost:5432/ticketing_infra`) DB를 만들고(없으면), `backend.migrate.run` → `backend.seed.run`. 개발 DB 이름(`ticketing`)이면 거절.
2. `APP_DB_NAME=ticketing_infra docker compose up -d --build --wait app1 app2 app3 nginx` — 앱이 테스트 DB를 보게 다시 만든다.
3. 끝나면(실패해도) 멈춘 앱을 다시 켜고, `docker compose up -d --wait app1 app2 app3 nginx`(기본값)로 앱을 개발 DB 설정으로 되돌린다.
- 주소는 `http://localhost:8080`. 앱을 멈추고 켤 때는 `docker compose stop app2` / `docker compose start app2`.

## 알려진 환경 문제: 이미지 빌드가 `DeadlineExceeded`로 멈출 때
Docker Desktop 자격증명 도우미(`docker-credential-desktop`)가 응답하지 않으면 공개 이미지 메타데이터 조회도 막힌다(2026-11-13 확인: 도우미 15초 무응답, 네트워크는 정상). 사용자 설정(`~/.docker/config.json`)은 그대로 두고 자격증명 없는 임시 설정으로 기본 이미지만 받아 두면 이후 빌드는 로컬 이미지를 쓴다.
```bash
T=$(mktemp -d); cp -R ~/.docker/contexts "$T/"; echo '{"currentContext":"desktop-linux"}' > "$T/config.json"
DOCKER_CONFIG=$T docker pull python:3.14-slim
DOCKER_CONFIG=$T docker pull ghcr.io/astral-sh/uv:0.13.7
```
