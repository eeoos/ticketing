# US-07 내 순서 확인과 입장권

**사용자로서** 내 차례가 언제인지 알고, 차례가 되면 입장권으로 좌석을 보고 잡고 싶다.

계약: `GET /shows/{showId}/queue/me`, 그리고 좌석맵·홀드의 `X-Queue-Ticket` (`docs/openapi.yaml`). 시각은 서버 `get_now()` 기준, 1초 구간마다 최대 200명 입장.

## 인수 조건
1. **Given** A의 번호가 전광판보다 클 때 **When** `/queue/me` 하면 **Then** `WAITING`, `ahead = 번호 − 전광판 − 1`, 입장권 없음, `Retry-After`(1~5초).
2. **Given** 구간이 지나 전광판이 A의 번호에 닿았을 때 **When** `/queue/me` 하면 **Then** `ADMITTED`와 입장권(`token`, `expiresAt = 지금 + 2분`).
3. **Given** 입장권을 받은 뒤 **When** `/queue/me`나 좌석맵을 다시 부르면 **Then** 같은 token·같은 만료 시각(늘어나지 않음).
4. **Given** 한 구간에서 **When** 여러 앱·워커가 동시에 `/queue/me`를 불러도 **Then** 전광판은 그 구간에 최대 200만 오른다. 전광판은 발급된 번호를 넘지 않는다.
5. **Given** 입장권이 만료 시각에 이르면(`now ≥ expiresAt`) **Then** 좌석맵·홀드는 403 `QUEUE_TICKET_EXPIRED`, `/queue/me`는 `EXPIRED`.
6. **Given** A의 입장권을 B가 쓰거나, 공연 X의 입장권으로 공연 Y를 보면 **Then** 403 `QUEUE_TICKET_INVALID`.
7. **Given** 한 앱에서 받은 입장권을 **When** 다른 앱이 검증해도 **Then** 통한다.
8. **Given** 줄을 선 적 없는 사용자가 `/queue/me` 하면 **Then** 404 `QUEUE_ENTRY_NOT_FOUND`. Redis를 쓸 수 없으면 503 `QUEUE_UNAVAILABLE`.
