"""대기열 시험 도구. 기준: docs/queue.md. 서버는 tests/cache 의 ClockServer(파일 시계 get_now)를 쓴다."""
import asyncio
import json
from datetime import timedelta

import httpx

from cache_support import T0, uid


def join(srv, user, show=None):
    headers = {"X-User-Id": user} if user else {}
    return srv.client.post(f"/shows/{show or srv.show_id()}/queue", headers=headers)


def me(srv, user, show=None):
    headers = {"X-User-Id": user} if user else {}
    return srv.client.get(f"/shows/{show or srv.show_id()}/queue/me", headers=headers)


def tick(srv, seconds=1):
    srv.advance(seconds=seconds)


def err(resp, status, code):
    assert resp.status_code == status, f"기대 {status}, 실제 {resp.status_code}: {resp.text[:300]}"
    assert resp.json().get("code") == code, resp.text


def seats_with(srv, user, token, show=None):
    return srv.client.get(f"/shows/{show or srv.show_id()}/seats",
                          headers={k: v for k, v in (("X-User-Id", user), ("X-Queue-Ticket", token)) if v})


def hold_with(srv, seat, user, token):
    return srv.client.post(f"/seats/{seat}/hold",
                           headers={k: v for k, v in (("X-User-Id", user), ("X-Queue-Ticket", token)) if v})


def admitted_ticket(srv, user, show=None):
    """번호표를 받은 사용자가 /me 로 입장권을 얻는다(시계는 호출 전에 테스트가 앞당겨 둔다)."""
    resp = me(srv, user, show)
    assert resp.status_code == 200 and resp.json()["status"] == "ADMITTED", resp.text
    return resp.json()["ticket"]["token"]


def read_int(r, key):
    v = r.get(key)
    return None if v is None else int(v)


async def _fire_urls(requests):
    n = len(requests)
    gate = asyncio.Event()
    limits = httpx.Limits(max_connections=n + 50, max_keepalive_connections=n + 50)
    async with httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(120)) as c:
        async def one(spec):
            await gate.wait()
            try:
                return await c.request(**spec)
            except httpx.HTTPError as e:
                return e
        tasks = [asyncio.create_task(one(s)) for s in requests]
        await asyncio.sleep(0.5)
        gate.set()
        return await asyncio.gather(*tasks)


def fire_urls(requests):
    """요청 dict 의 url 이 절대 URL(여러 서버 섞기 가능). 시작 신호 뒤 한꺼번에 전송. 실패한 요청은 예외 객체."""
    return asyncio.run(_fire_urls(requests))


def join_request(srv, user, show):
    return dict(method="POST", url=f"{srv.url}/shows/{show}/queue", headers={"X-User-Id": user})


def me_request(srv, user, show):
    return dict(method="GET", url=f"{srv.url}/shows/{show}/queue/me", headers={"X-User-Id": user})


def join_many(servers, show, n, prefix="u"):
    """n 명이 서버들에 번갈아 동시에 줄을 선다. (users, responses) 를 돌려준다."""
    users = [uid(prefix) for _ in range(n)]
    responses = fire_urls([join_request(servers[i % len(servers)], u, show) for i, u in enumerate(users)])
    bad = [x for x in responses if isinstance(x, Exception)]
    assert not bad, f"응답 없는 요청 {len(bad)}건: {bad[:2]}"
    return users, responses
