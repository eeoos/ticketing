# US-05 모의 결제사 콜백

**모의 결제사로서** 결제 결과(성공·실패)를 콜백으로 알리고 싶다. **그래서** 성공이면 좌석이 팔리고, 실패나 늦은 성공이면 좌석이 팔리지 않으며, 늦은 성공은 환불할 수 있게 기록된다.

계약: `POST /payments/callback` (`docs/openapi.yaml`), 상태 전이: `docs/backend-interface.md`. 콜백은 `X-Mock-Pg-Signature`(본문의 HMAC-SHA256)로 검증한다. 만료 판단은 서버 `get_now()`로만 한다(콜백 본문에 시각이 있어도 쓰지 않는다).

## 인수 조건
1. **Given** A가 시각 T에 홀드하고 결제 요청해 `PENDING`(paymentId P)일 때
   **When** T+4분59초에 P의 `SUCCEEDED` 콜백이 오면
   **Then** 200 `applied: true`, 예매 `CONFIRMED`, 좌석 `SOLD`.
2. **Given** 1과 같을 때
   **When** `SUCCEEDED` 콜백이 T+5분 정각 또는 T+5분1초에 오면
   **Then** 200 `applied: true`, 예매 `REFUND_REQUIRED`, 좌석은 팔리지 않는다(조회 시 `AVAILABLE`).
3. **Given** `PENDING`일 때 `FAILED` 콜백이 오면
   **Then** 200 `applied: true`, 예매 `FAILED`, 좌석은 A의 `HELD` 그대로(만료 시각 유지).
4. **Given** 같은 콜백(같은 paymentId·같은 result)이 이미 반영됐을 때
   **When** 같은 콜백이 다시 오면(동시에 여러 번 와도)
   **Then** 200 `applied: false`, 상태 변화 없음. 동시에 와도 `applied: true`는 정확히 한 번.
5. **Given** 예매가 `CONFIRMED`일 때 `FAILED` 콜백이 늦게 오면
   **Then** 409 `CALLBACK_CONFLICT`, 예매 `CONFIRMED`·좌석 `SOLD` 그대로.
6. **Given** 예매가 `FAILED`일 때 `SUCCEEDED` 콜백이 늦게 오면
   **Then** 200 `applied: true`, 예매 `REFUND_REQUIRED`, 좌석은 팔리지 않는다(홀드가 유효해도).
7. **Given** 예매가 `REFUND_REQUIRED`일 때 `FAILED` 콜백이 오면 → 409 `CALLBACK_CONFLICT`, 변화 없음.
8. **Given** A의 홀드가 만료된 뒤 B(또는 A 자신)가 같은 좌석을 새로 홀드했을 때
   **When** 이전 홀드에 묶인 결제 P의 `SUCCEEDED` 콜백이 오면
   **Then** 예매 P는 `REFUND_REQUIRED`, 새 홀드는 그대로(주인·`holdId`·만료 시각 불변, 좌석 `HELD`). `FAILED` 콜백이어도 새 홀드에 영향 없음.
9. **Given** 서명이 없거나 틀리면 401 `INVALID_SIGNATURE`, 본문이 잘못되면 400 `INVALID_REQUEST`, 모르는 paymentId면 404 `NOT_FOUND`. 모두 상태 변화 없음.
10. **Given** 같은 홀드에서 결제 실패 후 재시도한 결제가 성공하면
   **Then** 그 예매만 `CONFIRMED`, 좌석 `SOLD`. 좌석당 `CONFIRMED` 예매는 언제나 최대 하나다.

## 10장 추가: 확정 알림
11. **Given** `PENDING` 예매에 유효한 홀드로 `SUCCEEDED` 콜백이 와서 처음 `CONFIRMED`가 될 때 **Then** 같은 DB 트랜잭션에서 outbox에 `BookingConfirmed` 이벤트가 정확히 1줄 생기고, 결국 그 사용자의 알림함(`notifications`)에 알림이 정확히 1건 생긴다(`docs/events.md`).
12. **Given** 같은 성공 콜백이 다시 오거나(동시 포함), 확정된 예매를 새 키로 다시 요청할 때 **Then** outbox와 알림은 각각 1건 그대로.
13. **Given** `FAILED` 또는 `REFUND_REQUIRED`가 될 때 **Then** 확정 이벤트·알림은 생기지 않는다.
14. **Given** Kafka가 멈춰 있을 때 성공 콜백이 오면 **Then** 콜백은 200 `CONFIRMED`(outbox 1줄), Kafka가 다시 켜지면 알림이 1건 생긴다.
15. **Given** notifier를 껐다 켜면 **Then** 그 사이 확정된 예매의 알림이 빠짐없이 생기고, 같은 이벤트를 다시 받아도 알림은 늘지 않는다.
