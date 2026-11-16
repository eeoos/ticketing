# 예매 확정 알림: outbox → Kafka → notifier (10장)

예매가 확정되면 사용자 알림함에 알림을 넣는다. **알림이 늦거나 두 번 와도 예매는 틀리면 안 된다.** 그래서 API는 Kafka와 직접 말하지 않는다. 예매 확정은 DB의 outbox 기록까지만으로 성공하고, 전달은 뒤에서 릴레이와 notifier가 한다. 전달은 "최소 한 번"이고, 중복은 받는 쪽이 `eventId`로 없앤다.

```
POST /payments/callback (SUCCEEDED, 처음 CONFIRMED)
   └─ 같은 DB 트랜잭션: 예매 CONFIRMED + 좌석 SOLD + outbox 1줄
릴레이 (backend.relay)          outbox 미전송 줄 → Kafka booking-events (키 = showId) → 전송 확인 뒤 sent_at 표시
notifier (backend.notifier)    booking-events (소비자 그룹 notifier) → notifications 1줄 (eventId 중복 무시) → DB 커밋 → 오프셋 커밋
```

## 파일 소유
| 자리 | 파일 |
|---|---|
| lead | `docker-compose.yml`(events 프로필: kafka·relay·notifier), `pyproject.toml`/`uv.lock`(confluent-kafka 2.16), `docs/**` |
| server | `backend/**`: `backend/migrations/003_*.sql`(outbox·notifications), 콜백 트랜잭션, `backend/relay.py`, `backend/notifier.py` |
| test | `tests/events/**` |

## 언제 이벤트를 쓰나 (콜백)
- **쓴다**: 성공 콜백으로 예매가 **처음** `PENDING → CONFIRMED`가 되는 그 트랜잭션 안에서 outbox에 정확히 1줄.
- **안 쓴다**: 같은 성공 콜백이 다시 온 경우(`applied: false`), 이미 확정된 예매를 새 키로 다시 요청한 경우(`POST /bookings` 200), `FAILED`, `REFUND_REQUIRED`(만료·재홀드 뒤 성공, 실패 뒤 성공), 마이그레이션으로 확정 처리된 옛 예매.
- **DB 보장**: outbox에 `(booking_id, type)` UNIQUE — 예매당 확정 이벤트는 최대 하나. 콜백 트랜잭션이 롤백되면 outbox 줄도 없다.

## 이벤트 형식 (Kafka 메시지)
- 토픽: `EVENTS_TOPIC`(기본 `booking-events`), 파티션 3, 자동 생성.
- 키: `showId`(UTF-8). 같은 공연의 이벤트는 같은 파티션에 들어가므로 **Kafka에 도착한 순서대로** 읽힌다. 그 순서가 outbox 생성 순서와 같다는 보장은 **릴레이가 하나일 때만** 있다(compose는 릴레이 1개). 릴레이를 여럿 띄우면 같은 공연의 새 이벤트가 먼저 갈 수 있다. 알림은 이벤트마다 독립이라 순서는 정확성에 영향이 없다.
- 헤더: `eventId`(값과 같음, UTF-8).
- 값(JSON):
```json
{"eventId": "<uuid>", "type": "BookingConfirmed", "version": 1, "occurredAt": "<ISO 8601, 콜백 처리 시 get_now>",
 "bookingId": "...", "showId": "...", "seatId": "...", "userId": "...", "paymentId": "..."}
```
- `eventId`는 outbox 줄을 만들 때 한 번 정하고 다시 보내도 바뀌지 않는다.

## outbox 테이블 (server가 003 마이그레이션으로)
| 열 | 뜻 |
|---|---|
| `event_id` | 기본키, uuid 문자열 = 메시지의 `eventId` |
| `type` | `BookingConfirmed` |
| `booking_id`, `show_id`, `user_id` | 이벤트 대상 (`(booking_id, type)` UNIQUE) |
| `payload` | 위 JSON(jsonb) |
| `created_at` | 쓴 시각(get_now) |
| `sent_at` | Kafka 전송 확인 뒤 릴레이가 채움. NULL = 아직 안 보냄 |
| `attempts`, `last_error` | 릴레이 재시도 기록 |

## 릴레이 (`python -m backend.relay`)
- 반복: `sent_at IS NULL`인 줄을 오래된 순으로 최대 N개 읽음(`FOR UPDATE SKIP LOCKED` — 릴레이를 여러 개 띄워도 같은 줄을 동시에 잡지 않음) → 키 `showId`로 전송 → **브로커의 전송 확인(delivery report, acks=all, 멱등 프로듀서)을 받은 줄만** `sent_at = now()` 표시. 실패한 줄은 `attempts`·`last_error`만 갱신하고 다음에 다시(같은 eventId).
- Kafka가 멈추면 계속 재시도(백오프)하고, 예매 API는 영향 없음. 전송은 됐는데 표시 전에 죽으면 다시 보낸다(같은 eventId) — notifier가 걸러 낸다.
- 환경변수: `DATABASE_URL`, `KAFKA_BOOTSTRAP`(compose 안 `kafka:9092`, 호스트 `localhost:9094`), `EVENTS_TOPIC`, `RELAY_BATCH`(기본 100), `RELAY_POLL_MS`(기본 500).

## notifier (`python -m backend.notifier`)
- 소비자 그룹 `NOTIFIER_GROUP`(기본 `notifier`), `enable.auto.commit=false`, 처음 시작은 가장 오래된 것부터(`earliest`).
- 메시지마다: `notifications`에 1줄 `INSERT … ON CONFLICT (event_id) DO NOTHING` → **DB 커밋** → 그 메시지 **오프셋 커밋**(동기). 순서를 바꾸지 않는다. 커밋 사이에 죽으면 다시 받지만 `event_id` UNIQUE로 알림은 하나.
- **받아들이는 메시지**(하나라도 어기면 독이 든 메시지): UTF-8 JSON 객체이고 9개 필드가 모두 있다 — `eventId`·`bookingId`·`showId`·`seatId`·`userId`·`paymentId`·`occurredAt`은 비어 있지 않은 문자열(NUL 없음, 1~255자, `occurredAt`은 ISO 8601로 해석됨), `type == "BookingConfirmed"`, `version == 1`(정수). 메시지 키는 `showId`와 같아야 한다.
- **오류 나누기**:
  - **메시지 자체의 영구 오류** — 위 조건 위반, 또는 저장할 때 DB가 그 값 때문에 거절(`DataError`, `event_id` 충돌 외의 무결성 오류 등): 원인·eventId(있으면)·파티션·오프셋을 로그(ERROR)로 남기고 **오프셋을 커밋해 넘긴다**. 같은 파티션의 뒤 메시지가 막히면 안 된다.
  - **일시 오류** — DB 연결 끊김·시간 초과·`OperationalError`·직렬화 실패 등: 롤백하고 백오프한 뒤 **같은 메시지를 다시 처리**한다(오프셋 커밋 안 함, 알림 유실 금지). 재시도하는 동안에도 소비자 그룹에서 쫓겨나지 않게 한다(poll을 계속 부르거나 pause/seek).
  - 독이 든 메시지는 로그로만 남긴다(별도 보관 테이블은 이번 범위 밖).
- 환경변수: `DATABASE_URL`, `KAFKA_BOOTSTRAP`, `EVENTS_TOPIC`, `NOTIFIER_GROUP`.

## notifications 테이블 (앱 알림함)
| 열 | 뜻 |
|---|---|
| `id` | 기본키 |
| `event_id` | UNIQUE — 같은 이벤트로 알림은 하나 |
| `user_id` | 받는 사용자(이벤트의 `userId`) |
| `booking_id` | 예매 |
| `type` | `BOOKING_CONFIRMED` |
| `message` | 사람에게 보일 문장 |
| `created_at` | 넣은 시각 |
| `read_at` | 읽음(이번 범위에서는 항상 NULL) |
알림함 조회 API는 이번 범위에 없다.

## 실패 격리 (요구사항)
- Kafka·릴레이·notifier가 멈춰도 `POST /payments/callback`은 지금처럼 200 `CONFIRMED`(outbox 줄까지). 앱은 Kafka에 연결하지 않는다.
- Kafka가 다시 켜지면 릴레이가 밀린 줄을 보내고, notifier가 이어 받아 알림을 넣는다. notifier를 껐다 켜면 커밋된 오프셋 다음부터 이어 받는다.

## 실행
```bash
docker compose --profile events up -d --build --wait   # db·redis·app1~3·nginx + kafka·relay·notifier
```
기본 `docker compose up`에는 Kafka·릴레이·notifier가 없다(프로필). 릴레이·notifier는 HTTP 포트가 없어 컨테이너 헬스체크를 끄며(`healthcheck: disable`), 상태는 로그와 outbox `sent_at`·알림 수로 본다. 이미지는 마이그레이션을 자동 적용하지 않으므로 먼저 `uv run python -m backend.migrate`.

## 테스트에서 쓰는 법 (`tests/events`)
- DB: 개발 DB와 분리한 DB(예: `ticketing_events`), 마이그레이션·시드는 호스트에서.
- Kafka: 호스트 `localhost:9094`. 토픽·소비자 그룹은 실행마다 다른 이름(`EVENTS_TOPIC`, `NOTIFIER_GROUP`)으로. compose의 relay·notifier(개발 토픽·그룹)와 섞이지 않게, 테스트는 릴레이·notifier를 **호스트 하위 프로세스**(`uv run python -m backend.relay` 등)로 테스트 DB·토픽·그룹을 주고 띄운다.
- Kafka를 멈추는 테스트는 `docker compose stop kafka` 후 끝나면(실패해도) `docker compose start kafka` + 준비 확인, 복구 실패는 teardown error.
- 콜백까지 가려면 입장권이 필요하다(`docs/queue.md`, `tests/_support/ticket_support.py`).
