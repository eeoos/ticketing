# US-04 결제 요청(멱등)

**사용자로서** 잡아 둔 좌석의 결제를 요청하고 싶다. 네트워크가 끊겨 같은 요청을 다시 보내도 **결제가 두 번 생기지 않아야** 한다.

결제 요청은 예매를 `PENDING`으로 만들 뿐이고, 좌석은 결제사 성공 콜백(US-05)이 와야 `SOLD`가 된다. 계약: `POST /bookings`, `GET /bookings/{bookingId}` (`docs/openapi.yaml`). 시각은 서버 `get_now()` 기준.

## 인수 조건
1. **Given** 사용자 A가 시각 T에 좌석을 홀드했을 때
   **When** A가 `Idempotency-Key: k1`으로 T+4분59초에 결제를 요청하면
   **Then** 202, 예매 `status = PENDING`, `paymentId`·`holdId`·`holdExpiresAt`(= T+5분)이 응답에 있고, 좌석은 여전히 `HELD`다.
2. **Given** 1과 같을 때
   **When** A가 T+5분 정각 또는 T+5분1초에 처음 결제를 요청하면
   **Then** 409 `HOLD_EXPIRED`, 예매는 생기지 않는다.
3. **Given** 사용자 B의 유효한 홀드가 있을 때
   **When** A가 그 좌석의 결제를 요청하면
   **Then** 403 `HOLD_NOT_OWNED`, 예매는 생기지 않고 B의 홀드는 그대로다.
4. **Given** A가 `k1`, 본문 `{seatId: S}`로 202를 받았을 때
   **When** A가 같은 키·같은 본문으로 다시 요청하면(시각이 지나 홀드가 만료됐거나 예매가 확정된 뒤여도)
   **Then** 최초 응답과 같은 상태 코드·본문(같은 예매 id, `PENDING`)이 오고 헤더 `Idempotent-Replayed: true`가 붙는다. 예매는 하나뿐이다.
5. **Given** A가 `k1`로 좌석 S의 202를 받았을 때
   **When** A가 같은 키 `k1`으로 다른 본문(다른 좌석)을 보내면
   **Then** 409 `IDEMPOTENCY_KEY_MISMATCH`, 아무것도 바뀌지 않는다.
6. **Given** 사용자 B도 키 `k1`을 쓸 때
   **When** B가 자기 홀드로 `k1` 결제를 요청하면
   **Then** A의 키와 무관하게 처리된다(키 범위는 사용자별).
7. **Given** A의 같은 홀드에 `PENDING` 결제가 있을 때
   **When** A가 새 키로 다시 결제를 요청하면
   **Then** 409 `PAYMENT_IN_PROGRESS`, 새 예매는 생기지 않는다.
8. **Given** A의 결제가 실패(`FAILED`)했고 홀드가 아직 유효할 때
   **When** A가 새 키로 다시 결제를 요청하면
   **Then** 202, 새 `PENDING` 예매(새 `paymentId`)가 같은 `holdId`로 생긴다.
9. **Given** A의 예매가 `CONFIRMED`(좌석 `SOLD`)일 때
   **When** A가 새 키로 같은 좌석의 결제를 다시 요청하면
   **Then** 200과 그 확정 예매(같은 id, `CONFIRMED`)가 온다. 새 예매는 생기지 않는다.
10. **Given** 다른 사람의 확정 예매로 `SOLD`인 좌석일 때
   **When** A가 결제를 요청하면
   **Then** 409 `SEAT_SOLD`.
11. **Given** 홀드가 없는 `AVAILABLE` 좌석일 때 → 409 `NO_HOLD`.
12. **Given** `X-User-Id`가 없으면 401 `UNAUTHENTICATED`. `Idempotency-Key`가 없거나(빈 값·NUL 포함·255자 초과) 본문이 잘못되면 400 `INVALID_REQUEST`. 없는 좌석은 404 `NOT_FOUND`. 이 오류들은 키를 소비하지 않는다.
13. **Given** A가 같은 키·같은 본문으로 **동시에** 여러 번(여러 워커) 요청할 때
   **Then** 예매는 하나만 생기고 모든 응답은 같은 예매 id의 202다. 5xx 0건.
14. **Given** 예매 id가 있을 때
   **When** 그 예매의 주인이 `GET /bookings/{id}`를 하면 현재 상태를 받고, 다른 사용자는 404 `NOT_FOUND`를 받는다.
