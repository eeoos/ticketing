# US-02 좌석맵 보기

**사용자로서** 공연의 좌석맵과 좌석별 상태를 보고 싶다. **그래서** 잡을 수 있는 좌석을 고를 수 있다.

## 인수 조건
1. **Given** 좌석이 있는 공연이 있을 때
   **When** 그 공연의 좌석맵을 요청하면
   **Then** 모든 좌석이 식별자, 위치(구역·열·번호), 상태(`AVAILABLE`/`HELD`/`SOLD`)와 함께 반환된다.
2. **Given** 좌석 하나가 홀드되어 있고 아직 만료 전일 때
   **When** 좌석맵을 요청하면
   **Then** 그 좌석은 `HELD`로 보인다.
3. **Given** 좌석 하나의 홀드가 만료되었을 때
   **When** 좌석맵을 요청하면
   **Then** 그 좌석은 `AVAILABLE`로 보인다.
4. **Given** 좌석 하나가 예매 확정되었을 때
   **When** 좌석맵을 요청하면
   **Then** 그 좌석은 `SOLD`로 보인다.
5. **Given** 존재하지 않는 공연 식별자로
   **When** 좌석맵을 요청하면
   **Then** "찾을 수 없음" 오류가 반환된다.

## 9장 변경: 입장권 필요
좌석맵은 이제 `X-User-Id`와 그 공연의 유효한 입장권(`X-Queue-Ticket`)이 있어야 한다(`docs/queue.md`). 위 인수 조건 1~4는 유효한 입장권을 붙인 요청 기준이다.
6. **Given** `X-User-Id`가 없을 때 **When** 좌석맵을 요청하면 **Then** 401 `UNAUTHENTICATED`.
7. **Given** 입장권이 없을 때 **When** 좌석맵을 요청하면 **Then** 403 `QUEUE_TICKET_REQUIRED`(좌석 캐시를 보지 않는다).
8. **Given** 다른 사용자의 입장권 또는 다른 공연의 입장권일 때 **Then** 403 `QUEUE_TICKET_INVALID`. 만료된 입장권이면 403 `QUEUE_TICKET_EXPIRED`.
9. **Given** Redis를 쓸 수 없을 때 **Then** 503 `QUEUE_UNAVAILABLE`(입장권 검증을 건너뛰지 않는다).
- 인수 조건 5(없는 공연 404)는 바뀐다: 없는 공연의 입장권은 발급될 수 없으므로 403으로 끝난다.
