"""입장권(X-Queue-Ticket) 시험용 공용 도구 (docs/queue.md '테스트에서 쓰는 법').

대기열 동작 자체를 보는 테스트(tests/queue)가 아니면 입장권은 Redis 에 문서화된 형식으로 직접 써 준다:
  {p}ticket:{token} = {"userId","showId","number","expiresAtMs"}   (PX 는 청소용)
이유: 실제로 얻으려면 번호표 → 전광판 구간 진행 → /me 가 필요한데, get_now 를 움직이는 시나리오(T+5:00 같은 정확한 시각)나
실제 시계 서버(구간당 200명 → 500명이면 3초 이상)에서는 시각·시간을 흐트러뜨리거나 느려진다. 형식은 계약(docs/queue.md)에 있다.
"""
import json
import os
import uuid

import redis

REDIS_HOST_URL = os.environ.get("TICKET_TEST_REDIS_HOST_URL", "redis://localhost:6379")
# Redis DB 번호 배정: 0 개발 / 1 tests/cache·tests/queue(ClockServer) / 2 cache 3대 스택 / 3 infra / 4 api / 5 concurrency / 6 queue 3대 스택
DB_API, DB_CONCURRENCY, DB_QUEUE_STACK = 4, 5, 6
HOUR_MS = 3_600_000


def redis_for(db):
    return redis.Redis.from_url(f"{REDIS_HOST_URL}/{db}", decode_responses=True, socket_timeout=3)


def grant_ticket(r, prefix, user, show_id, expires_at_ms, number=1, token=None):
    """user 에게 show_id 의 입장권을 직접 발급한다(만료 시각은 get_now 기준 epoch ms). token 을 돌려준다."""
    token = token or f"tk-{uuid.uuid4().hex}"
    r.set(f"{prefix}ticket:{token}",
          json.dumps({"userId": user, "showId": show_id, "number": number, "expiresAtMs": int(expires_at_ms)},
                     separators=(",", ":")), px=HOUR_MS)
    return token


def delete_prefix(r, prefix):
    keys = list(r.scan_iter(match=f"{prefix}*", count=500))
    if keys:
        r.delete(*keys)
