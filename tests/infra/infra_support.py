"""tests/infra 공용 도구. 기준: docs/infra.md (앱 3대 + Nginx, 게이트웨이 8080).

- 분리 DB(INFRA_DATABASE_URL, 기본 ticketing_infra)만 쓴다. 개발 DB 이름(ticketing)이면 거절.
- 앱은 'APP_DB_NAME=<분리 DB> docker compose up -d --build --wait app1 app2 app3 nginx' 로 다시 만든다.
- 끝나면(실패해도) 멈춘 앱을 켜고, 기본 설정(개발 DB)으로 'docker compose up -d --wait app1 app2 app3 nginx' 로 되돌린다.
- 준비는 처음 쓸 때(테스트 본문 안) 한 번 하고, 실패하면 같은 오류를 테스트마다 개별 실패로 보인다.
"""
import asyncio
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import psycopg
import redis
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

ROOT = Path(__file__).resolve().parents[2]
INFRA_DSN = os.environ.get("INFRA_DATABASE_URL", "postgresql://ticketing:ticketing@localhost:5432/ticketing_infra")
BASE_URL = os.environ.get("INFRA_BASE_URL", "http://localhost:8080")
DEV_DB_NAME = "ticketing"
APPS = ("app1", "app2", "app3")
INFRA_REDIS_DB = 3  # 개발 스택은 Redis DB 0·접두사 'tkt:', tests/cache 는 DB 1·2. infra 테스트는 DB 3 에 실행별 접두사
REDIS_HOST_URL = os.environ.get("INFRA_REDIS_HOST_URL", "redis://localhost:6379")


def uid(prefix="u") -> str:
    """속도 제한(X-User-Id별)을 피하려고 테스트·요청마다 다른 사용자 이름을 만든다."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def db_name(dsn=INFRA_DSN) -> str:
    return conninfo_to_dict(dsn)["dbname"]


def compose(*args, env_extra=None, drop_env=(), timeout=600, check=True):
    env = {k: v for k, v in os.environ.items() if k not in drop_env}
    env.update(env_extra or {})
    p = subprocess.run(["docker", "compose", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)} 실패(code {p.returncode}):\n{(p.stdout + p.stderr)[-1500:]}")
    return p


class Infra:
    """게이트웨이(8080) 클라이언트 + 앱 중지·복구 + 분리 DB. 첫 사용 시 한 번 준비한다."""

    def __init__(self):
        self._ready = False
        self.redis_prefix = f"infratest-{uuid.uuid4().hex[:8]}:"
        self._error = None
        self._touched = False
        self.stopped: set[str] = set()
        self.client = None

    # ---- 준비 ----
    def _ensure(self):
        if self._error:
            raise AssertionError(self._error)
        if self._ready:
            return
        try:
            if db_name() == DEV_DB_NAME:
                raise RuntimeError(f"INFRA_DATABASE_URL 이 개발 DB '{DEV_DB_NAME}' 를 가리킨다. 분리 DB(기본 ticketing_infra)를 써야 한다.")
            admin = make_conninfo(INFRA_DSN, dbname="postgres")
            with psycopg.connect(admin, autocommit=True) as conn:
                if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (db_name(),)).fetchone():
                    conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name())))
            os.environ["DATABASE_URL"] = INFRA_DSN
            from backend import migrate, seed  # noqa: PLC0415
            migrate.run(INFRA_DSN)
            seed.run(INFRA_DSN)
            self._touched = True  # 이 시점부터는 끝에서 기본 설정으로 되돌린다
            compose("up", "-d", "--build", "--wait", *APPS, "nginx",
                    env_extra={"APP_DB_NAME": db_name(), "APP_REDIS_DB": str(INFRA_REDIS_DB),
                               "APP_REDIS_KEY_PREFIX": self.redis_prefix})
            self.client = httpx.Client(base_url=BASE_URL, timeout=30)
            self.wait_all_apps_up()
            self._ready = True
        except Exception as e:  # noqa: BLE001
            self._error = f"인프라를 준비하지 못했다: {type(e).__name__}: {str(e)[-1200:]}"
            raise AssertionError(self._error) from e

    def begin_test(self):
        """테스트 시작: 준비 + 멈춘 앱 없음 + 시드 상태로 초기화."""
        self._ensure()
        if self.stopped:
            self.restore_apps()
        from backend import seed  # noqa: PLC0415
        seed.run(INFRA_DSN)
        return self

    # ---- 앱 중지·복구 ----
    def stop(self, app):
        self._ensure()
        compose("stop", app)
        self.stopped.add(app)

    def kill(self, app):
        """SIGKILL — stop 보다 빠르다(멈춘 직후 Nginx 가 아직 후보로 둘 때 POST 를 보내려는 테스트용)."""
        self._ensure()
        compose("kill", app)
        self.stopped.add(app)

    def start(self, app):
        compose("start", app)
        self.stopped.discard(app)

    def restore_apps(self):
        """멈춘 앱을 모두 켜 보고 세 앱이 다시 응답할 때까지 기다린다. 하나라도 실패하면 예외(어느 앱인지 포함).
        stopped 추적은 복구를 확인한 뒤에만 지운다."""
        errors = []
        for app in sorted(self.stopped):
            p = compose("start", app, check=False)
            if p.returncode:
                errors.append(f"{app}: docker compose start 실패 ({(p.stderr or p.stdout).strip()[-200:]})")
        try:
            self.wait_all_apps_up()
        except Exception as e:  # noqa: BLE001
            errors.append(f"앱 응답 확인 실패: {e}")
        if errors:
            raise RuntimeError("멈춘 앱 복구 실패 — " + " | ".join(errors))
        self.stopped.clear()

    def wait_all_apps_up(self, timeout=60):
        """세 앱이 모두 X-App-Id 로 관측될 때까지 GET 을 보낸다(실패한 앱은 10초 제외)."""
        seen, deadline = set(), time.monotonic() + timeout
        with httpx.Client(base_url=BASE_URL, timeout=10) as c:
            while time.monotonic() < deadline:
                try:
                    r = c.get("/shows", headers={"X-User-Id": uid("wait")})
                    if r.status_code == 200 and r.headers.get("X-App-Id"):
                        seen.add(r.headers["X-App-Id"])
                except httpx.HTTPError:
                    pass
                if seen >= set(APPS):
                    return
                time.sleep(0.3)
        raise RuntimeError(f"{timeout}s 안에 세 앱이 모두 응답하지 않았다(관측: {sorted(seen)})")

    def verify_default_config(self):
        """기본(개발 DB) 설정 복구를 확인한다. 복구 안 된 서비스·설정 문제 목록을 돌려준다(비어 있으면 정상)."""
        problems = []
        ps = compose("ps", "--format", "json", check=False)
        states = {}
        for line in ps.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                row = json.loads(line)
                states[row.get("Service")] = row
        for svc in (*APPS, "nginx"):
            row = states.get(svc)
            if row is None:
                problems.append(f"{svc}: 컨테이너가 없다")
            elif row.get("State") != "running" or row.get("Health") not in ("healthy", ""):
                problems.append(f"{svc}: 상태 {row.get('State')}/{row.get('Health')} (running/healthy 아님)")
        for app in APPS:
            p = compose("exec", "-T", app, "printenv", "DATABASE_URL", check=False)
            url = p.stdout.strip()
            if p.returncode or not url:
                problems.append(f"{app}: DATABASE_URL 을 읽지 못했다")
            elif db_name(url) != DEV_DB_NAME:
                problems.append(f"{app}: DATABASE_URL 이 개발 DB 가 아니다({db_name(url)})")
            r_url = compose("exec", "-T", app, "printenv", "REDIS_URL", check=False)
            if r_url.returncode or not r_url.stdout.strip().endswith("/0"):
                problems.append(f"{app}: REDIS_URL 이 개발 Redis DB 0 으로 돌아오지 않았다({r_url.stdout.strip() or '읽지 못함'})")
            r_prefix = compose("exec", "-T", app, "printenv", "REDIS_KEY_PREFIX", check=False)
            if r_prefix.returncode or r_prefix.stdout.strip() != "tkt:":
                problems.append(f"{app}: REDIS_KEY_PREFIX 가 'tkt:' 로 돌아오지 않았다({r_prefix.stdout.strip() or '읽지 못함'})")
        return problems

    def delete_redis_keys(self):
        """자기 접두사 키만 지운다(DB 3). Redis 가 없거나 꺼져 있으면 건너뛴다."""
        try:
            r = redis.Redis.from_url(f"{REDIS_HOST_URL}/{INFRA_REDIS_DB}", socket_timeout=3)
            keys = list(r.scan_iter(match=f"{self.redis_prefix}*", count=500))
            if keys:
                r.delete(*keys)
        except redis.RedisError:
            pass

    def finalize(self):
        """세션 끝: 가능한 복구를 모두 시도한 뒤, 하나라도 안 되면 예외로 드러낸다(어느 서비스·설정인지 포함).
        1) 멈춘 앱 켜기 2) 'docker compose up -d --wait app1 app2 app3 nginx'(기본값=개발 DB) 3) 상태·DATABASE_URL 확인."""
        if not self._touched:
            return
        errors = []
        try:
            self.restore_apps()
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
        self.delete_redis_keys()
        p = compose("up", "-d", "--wait", *APPS, "nginx",
                    drop_env=("APP_DB_NAME", "APP_REDIS_DB", "APP_REDIS_KEY_PREFIX"), check=False)
        missing_services = p.returncode and "no such service" in p.stderr  # 구현 전: 되돌릴 서비스 자체가 없다
        if p.returncode and not missing_services:
            errors.append(f"기본 설정 복구(up) 실패: {(p.stdout + p.stderr).strip()[-300:]}")
        if not missing_services:
            errors.extend(self.verify_default_config())
        if self.client:
            self.client.close()
        if errors:
            raise RuntimeError("복구되지 않은 항목: " + " | ".join(errors))

    # ---- 요청 ----
    def request(self, method, path, user="__anonymous__", client=None, **kw):
        self._ensure()
        headers = dict(kw.pop("headers", {}))
        if user is not None and user != "__anonymous__":
            headers["X-User-Id"] = user
        return (client or self.client).request(method, path, headers=headers, **kw)

    def get(self, path, user="__anonymous__", **kw):
        return self.request("GET", path, user=user, **kw)

    def ticket(self, user, show_id=None):
        """9장: 좌석맵·홀드는 입장권이 필요하다. Redis(DB 3·이 실행의 접두사)에 문서화된 형식으로 직접 발급(실제 시각 +2시간).
        대기열 동작 자체는 tests/queue 가 검증한다."""
        from ticket_support import grant_ticket  # noqa: PLC0415
        self._ensure()
        sid = show_id or self.get("/shows").json()[0]["id"]
        r = redis.Redis.from_url(f"{REDIS_HOST_URL}/{INFRA_REDIS_DB}", decode_responses=True, socket_timeout=3)
        return grant_ticket(r, self.redis_prefix, user, sid, time.time() * 1000 + 2 * 3_600_000)

    def seat_ids(self, n):
        shows = self.get("/shows").json()
        viewer = uid("viewer")
        seats = self.get(f"/shows/{shows[0]['id']}/seats", user=viewer,
                         headers={"X-Queue-Ticket": self.ticket(viewer, shows[0]["id"])}).json()
        return [s["id"] for s in seats[:n]]

    def hold(self, seat, user):
        return self.request("POST", f"/seats/{seat}/hold", user=user, headers={"X-Queue-Ticket": self.ticket(user)})

    def pay(self, seat, user, key):
        return self.request("POST", "/bookings", user=user, json={"seatId": seat}, headers={"Idempotency-Key": key})

    def bookings_in_db(self, seat=None):
        self._ensure()
        with psycopg.connect(INFRA_DSN) as conn:
            if seat:
                return conn.execute("SELECT id, user_id, status FROM bookings WHERE seat_id = %s", (seat,)).fetchall()
            return conn.execute("SELECT id, user_id, status FROM bookings").fetchall()

    def seat_status(self, seat):
        with psycopg.connect(INFRA_DSN) as conn:
            return conn.execute("SELECT status FROM seats WHERE id = %s", (seat,)).fetchone()[0]

    # ---- 동시 요청 ----
    def burst(self, n, make_request):
        """n 개 요청을 같은 시작 신호 뒤 한꺼번에 보낸다. make_request(i) → dict(method, url, headers...)."""
        self._ensure()

        async def run():
            gate = asyncio.Event()
            limits = httpx.Limits(max_connections=n + 10, max_keepalive_connections=n + 10)
            async with httpx.AsyncClient(base_url=BASE_URL, limits=limits, timeout=30) as c:
                async def one(i):
                    await gate.wait()
                    return await c.request(**make_request(i))
                tasks = [asyncio.create_task(one(i)) for i in range(n)]
                await asyncio.sleep(0.3)
                gate.set()
                return await asyncio.gather(*tasks)

        return asyncio.run(run())
