# US-06 대기열 번호표 받기

**사용자로서** 티켓 오픈 때 공연 대기열에 줄을 서고 싶다. **그래서** 몰려도 공정한 순서로 좌석을 볼 수 있다.

계약: `POST /shows/{showId}/queue` (`docs/openapi.yaml`), 규칙: `docs/queue.md`.

## 인수 조건
1. **Given** 공연이 있을 때 **When** 사용자 A가 처음 줄을 서면 **Then** 201, 번호(1부터), 전광판, 앞 사람 수, `status`가 온다.
2. **Given** A가 이미 번호를 받았을 때 **When** A가 다시 부르거나 두 탭에서 동시에 부르면 **Then** 같은 번호(200). 번호는 새로 발급되지 않는다.
3. **Given** 1000명이 세 앱에 동시에 줄을 설 때 **Then** 번호는 1~1000이 정확히 한 번씩 발급된다(겹침·빠짐 없음).
4. **Given** A의 입장권이 만료됐을 때 **When** A가 다시 줄을 서면 **Then** 201, 맨 뒤 새 번호.
5. **Given** `X-User-Id`가 없으면 401, 없는 공연이면 404 `NOT_FOUND`, Redis를 쓸 수 없으면 503 `QUEUE_UNAVAILABLE`(+`Retry-After`).
