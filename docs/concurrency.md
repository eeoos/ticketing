# 동시성 검증

같은 좌석에 요청이 몰릴 때 홀드와 예매 확정이 정확히 한 번만 성공하는지 **실제 서버(uvicorn 워커 여러 개) + PostgreSQL**로 확인한다.

## 판정 기준
| 시나리오 | 요청 | 통과 조건 |
|---|---|---|
| C-01 동시 홀드 | 같은 좌석에 서로 다른 사용자 500명이 동시에 `POST /seats/{id}/hold` | 201 정확히 1건, 나머지 499건 409, 5xx 0건, 좌석은 HELD 1명 |
| C-02 동시 예매 확정 | 좌석을 홀드한 본인이 같은 좌석에 `POST /bookings`를 동시에 20번 | DB `bookings`에 그 좌석 1행, 201 정확히 1건, 나머지 19건 409, 5xx 0건 |

## 테스트 방법 (test ↔ server 약속)
- 위치: `tests/concurrency/` (test 소유). `uv run pytest`에 포함된다.
- DB: `CONCURRENCY_DATABASE_URL`(기본 `postgresql://ticketing:ticketing@localhost:5432/ticketing_concurrency`). 개발 DB `ticketing`이면 거절한다. 없으면 만들고 `backend.migrate.run` → `backend.seed.run`으로 매 시나리오 전 초기화.
- 서버: 테스트가 `python -m uvicorn backend.main:app --workers 4 --port <빈 포트>`를 하위 프로세스로 띄운다(`DATABASE_URL` = 위 DB). 준비될 때까지 기다리고, 끝나면 종료한다. 시각 오버라이드 없음(실제 시계).
- 클라이언트: 시작 신호(배리어) 뒤 한꺼번에 보낸다. httpx 연결 수 제한이 요청 수보다 작지 않게 한다.
- **겹침 증거**(없으면 RED/GREEN 판정 무효):
  - HTTP: 요청별 보낸 시각·받은 시각으로 계산한 최대 동시 진행 수.
  - DB: 실행 중 `pg_stat_activity`에서 그 DB의 `state = 'active'` 세션 수를 짧은 간격으로 샘플링한 최댓값.
  - 여러 워커가 처리했는지: server는 DB 연결에 `application_name = 'ticketing-api-<워커 PID>'`를 붙인다. 테스트는 샘플링 중 본 서로 다른 `application_name` 수를 센다.
  - 테스트는 이 값들을 출력하고(`-s` 없이도 실패 메시지·로그에 남게), HTTP 최대 동시 진행 수가 2 미만이면 실패한다.

## RED 확인 계획 (일부러 깨뜨린 구현)
| 대상 | 깨뜨린 방식 | 예상 실패 |
|---|---|---|
| 홀드 | 상태를 SELECT로 읽고 검사한 뒤 조건 없는 UPDATE | 201이 2건 이상(중복 성공) |
| 예매 확정 | `SELECT … FOR UPDATE`의 `FOR UPDATE`만 제거, UNIQUE 제약 유지 | 중복 INSERT가 UNIQUE에 걸려 5xx (DB 행은 1건) |

실패가 실제로 재현되고 겹침 증거가 있을 때만 RED로 기록한다. 확인 후 원래 구현으로 되돌리고 GREEN을 다시 확인한다.

## 결과 (2026-11-13)

환경: macOS arm64 11코어 한 대에서 클라이언트·서버·DB 모두 실행, PostgreSQL 17.11, Python 3.14, uvicorn `--workers 4`, 워커당 DB 풀 최대 10(`DB_POOL_MAX_SIZE`).

### 1. 올바른 구현 전 발견: 연결 수 초과
요청마다 새 DB 연결을 열어 워커 4 × 스레드 40 = 최대 160 연결이 PostgreSQL `max_connections = 100`을 넘었다. C-01이 3회 중 2회 5xx(`too many clients`)로 실패 → 워커당 연결 풀(`psycopg_pool`) 도입 후 5회 연속 통과.

### 2. 테스트가 겹침을 잡는지 (RED → GREEN)
| 단계 | 구현 | C-01 동시 홀드 500 | C-02 동시 확정 20 | 재현 | 겹침 증거 |
|---|---|---|---|---|---|
| GREEN 기준 | 조건부 UPDATE / `FOR UPDATE` + UNIQUE | 201×1, 409×499, 5xx 0 | 201×1, 409×19, DB 1행 | 5/5 통과 | HTTP 동시 500 / 20, 워커 4 |
| RED | 잠금 없는 SELECT 후 조건 없는 UPDATE | **201×2~6** (중복 성공), 5xx 0 | — | 7/7 실패 (test 5, lead 2) | HTTP 동시 500, 워커 3~4 |
| RED | `FOR UPDATE` 제거, UNIQUE 유지 | — | **5xx×1~3** (UniqueViolation), DB 1행 | 7/7 실패 | HTTP 동시 20, 워커 3~4 |
| 되돌림 | 원래 구현 (sha256 일치) | 201×1, 409×499 | 201×1, 409×19 | 3/3 통과 | 동일 |

- UNIQUE 제약만으로도 DB 행은 1건으로 지켜지지만, 나머지 요청이 409가 아니라 500을 받는다. `FOR UPDATE`가 있어야 계약(409 `SEAT_SOLD`)을 지킨다.
- `bookings.seat_id` UNIQUE는 `001_init.sql`에 한 번만 있고, DB `ticketing`·`ticketing_test`·`ticketing_concurrency`·`ticketing_bench`에 모두 `bookings_seat_id_key`로 적용되어 있다. 같은 좌석으로 두 번째 INSERT 시 `duplicate key value violates unique constraint`로 거절됨을 확인했다.

### 3. 홀드 방식 비교: 조건부 UPDATE(A) vs SELECT FOR UPDATE → UPDATE(B)
같은 좌석 하나에 서로 다른 사용자 500명. 측정마다 DB 재초기화·서버 새로 기동, A·B 번갈아 7회, 첫 회 예열 제외 6회. `uv run python -m backend.tools.bench_hold --mode http|db`.

| 측정 | 방식 | p50 중앙값 (범위) | p95 중앙값 (범위) | 처리량 중앙값 | 201 / 409 / 5xx (6회 합) |
|---|---|---|---|---|---|
| HTTP (클라이언트 10프로세스 × 50) | A 조건부 UPDATE | 418.8 ms (373.3–446.6) | 709.0 ms (629.7–737.5) | 584.6 req/s | 6 / 2,994 / 0 |
| HTTP | B SELECT FOR UPDATE | 400.5 ms (360.2–480.5) | 675.8 ms (609.7–790.7) | 608.1 req/s | 6 / 2,994 / 0 |
| DB 단독 (연결 40, 트랜잭션 500) | A 조건부 UPDATE | 9.2 ms (8.5–10.2) | 17.4 ms (16.4–17.8) | 3,502.6 tx/s | 6 / 2,994 / 0 |
| DB 단독 | B SELECT FOR UPDATE | 12.2 ms (11.9–12.8) | 14.7 ms (13.7–16.7) | 3,769.9 tx/s | 6 / 2,994 / 0 |

lead 재측정(3회, 예열 1회 포함)도 같은 양상: DB p50 A 10.6–10.7 / B 12.3–12.6 ms, HTTP 두 방식 p50 371–396 ms.

읽는 법:
- **정확성**: 두 방식 모두 동시성 테스트 3/3 통과, 측정 전 회차에서 201 정확히 1건·5xx 0건.
- **HTTP 전체**: 범위가 겹쳐 이 환경·표본에서는 어느 쪽의 우위도 입증하지 못했다(동등하다는 증명은 아님). HTTP 지연(p95 ~700 ms)이 DB 단독(p95 ~17 ms)보다 훨씬 크지만, 두 측정은 구간이 다르다(HTTP는 연결·서버 큐·풀 대기 포함, DB 단독은 스레드 대기 제외). 병목이 어디인지는 같은 요청을 구간별로 계측해야 알 수 있고, 이번에는 하지 않았다.
- **DB 단독**: A가 p50이 약 3 ms 낮고(범위가 겹치지 않음), B가 p95가 약 3 ms 낮다(범위 일부 겹침). 처리량은 범위가 겹친다.
- **측정 주의**: 첫 측정은 한 프로세스의 httpx 클라이언트가 병목이라(500명 기준 p95 ~5.3 s, 404 응답도 같음) 버렸다. 클라이언트를 10프로세스로 나누자 p95 ~0.6–0.7 s.

### 결정
**A 조건부 UPDATE를 남긴다.** 정확성은 같고, 사용자에게 보이는 HTTP 지연·처리량에서 어느 쪽도 뚜렷한 우위를 보이지 않았다. A는 검사와 변경이 SQL 한 문장에 있어 "읽고 검사한 뒤 쓰기" 사이 틈이 없고 코드가 더 짧다. DB 단독에서도 중앙값은 A가 빠르다. B와 전환 스위치(`HOLD_STRATEGY`)는 제거한다.

### 리뷰 반영 (review, 2026-11-13)
| 지적 | 처리 |
|---|---|
| 풀 생성 뒤 `DATABASE_URL` 전환이 무시됨 | 풀을 (PID, DSN) 기준으로 재생성. 회귀 테스트 `tests/api/test_db_interface.py` RED → GREEN |
| 겹침 증거가 HTTP 동시 진행만 봄 | 샘플러 오류·샘플 0·DB 작업 워커 1개 이하이면 판정 무효로 실패. 요청 시나리오는 그대로 |
| 측정 결과로 병목을 단정 | 위 '읽는 법' 문구를 한정 |
| 서버 준비 실패 시 하위 프로세스·측정 도구 정리 누락 | fixture와 `bench_hold.py`에 finally 정리·대기 상한 추가 |

### 검증하지 않은 것
- DSN이 바뀌는 순간 다른 스레드가 이전 풀을 받아 `PoolClosed`를 볼 수 있는 좁은 경합(운영처럼 DSN이 고정이면 해당 없음).
- 서버·DB를 다른 기기에 둔 환경, 여러 좌석에 흩어진 부하, 장시간 부하.
- 풀이 30초 넘게 비면 `PoolTimeout`이 5xx로 나간다(이번 측정에서는 0건).
- HTTP 요청 한 건을 연결·풀 대기·SQL·응답 구간으로 나눈 계측(병목 분해).
- 연결 예산: API 워커 4 × 풀 10 = 40이지만, 서버 인스턴스를 늘리거나 `DB_POOL_MAX_SIZE`·워커 수를 키우면 테스트·도구 연결과 합쳐 `max_connections = 100`을 넘을 수 있다.
