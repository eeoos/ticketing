"""게이트웨이(Nginx) + 앱 3대 확인 (docs/infra.md). 모든 테스트는 첫 줄에서 infra.begin_test() 로 준비한다.

해석·근거(요약)
- 분산: 같은 upstream 을 쓰는 다른 트래픽(다른 클라이언트·테스트)이 라운드 로빈 순번을 가져갈 수 있어 '정확히 10건씩'·
  '연속 3건 서로 다름'은 단언하지 않는다. 세 앱이 모두 등장하고 각 앱 몫이 6~14건(10±4)이어야 한다.
  근거: 라운드 로빈에서는 외부 요청 k 건이 끼면 전체 슬롯 30+k 가 세 앱에 ⌊(30+k)/3⌋~⌈(30+k)/3⌉ 씩 돌아가고 그중 외부 몫만
  빠지므로, 내 몫이 6 미만/14 초과가 되려면 외부 요청이 ~12건 이상 한 앱에 몰려야 한다. 한 앱만(30/0/0)·두 앱만(15/15/0)·
  치우친 가중치(예 20/5/5)는 이 범위 밖이라 실패한다.
- 속도 제한: X-User-Id 별 5r/s·burst 10(nodelay) → 같은 사용자 동시 30건 중 통과는 burst(11 = 1 + 10) 근처.
  전송에 걸리는 시간 동안 채워지는 토큰을 고려해 통과 10~13건만 허용한다. 나머지는 전부 429.
- 장애 넘김: 앱을 멈춘 직후 10초 안에 GET 30건은 전부 200 이고 멈춘 앱의 X-App-Id 는 없다.
"""
import time
from collections import Counter

import httpx
import pytest

from infra_support import APPS, uid


def app_of(resp):
    return resp.headers.get("X-App-Id")


# ---- 1. 분산 ----
def assert_even(counts, total=30, label=""):
    assert set(counts) == set(APPS), f"{label}세 앱이 모두 요청을 받아야 한다: {dict(counts)}"
    assert sum(counts.values()) == total, dict(counts)
    assert all(6 <= counts[a] <= 14 for a in APPS), f"{label}고르게(각 6~14건) 분산돼야 한다: {dict(counts)}"


def test_infra_distribution_sequential_keepalive_is_roughly_even(infra):
    infra.begin_test()
    apps = []
    for _ in range(30):
        r = infra.get("/shows", user=uid())  # 요청마다 다른 사용자라 속도 제한 없음
        assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
        apps.append(app_of(r))
    assert_even(Counter(apps), label=f"순서 {apps}: ")


def test_infra_distribution_parallel_new_connections_is_roughly_even(infra):
    infra.begin_test()
    users = [uid() for _ in range(30)]
    responses = infra.burst(30, lambda i: dict(method="GET", url="/shows", headers={"X-User-Id": users[i]}))
    assert [r.status_code for r in responses] == [200] * 30, Counter(r.status_code for r in responses)
    assert_even(Counter(app_of(r) for r in responses))


def test_infra_every_app_response_carries_x_app_id(infra):
    infra.begin_test()
    for path in ("/shows",):
        r = infra.get(path, user=uid())
        assert app_of(r) in APPS, f"X-App-Id 가 app1~3 이어야 한다: {app_of(r)!r}"


# ---- 2. 속도 제한 ----
def test_infra_rate_limit_same_user_30_concurrent_gets_429(infra):
    infra.begin_test()
    user = uid("limited")
    responses = infra.burst(30, lambda i: dict(method="GET", url="/shows", headers={"X-User-Id": user}))
    codes = Counter(r.status_code for r in responses)
    passed = [r for r in responses if r.status_code != 429]
    limited = [r for r in responses if r.status_code == 429]
    assert set(codes) <= {200, 429}, f"200/429 만 나와야 한다: {dict(codes)}"
    assert 10 <= len(passed) <= 13, f"통과가 burst(11) 근처여야 한다: {dict(codes)}"
    assert len(limited) == 30 - len(passed) and len(limited) >= 17, dict(codes)
    for r in limited:
        assert r.headers.get("Retry-After"), "429 에 Retry-After 헤더가 있어야 한다"
        assert r.headers["Retry-After"].isdigit() and int(r.headers["Retry-After"]) >= 1, r.headers["Retry-After"]
        assert r.headers.get("content-type", "").startswith("application/json"), r.headers.get("content-type")
        body = r.json()
        assert body.get("code") == "RATE_LIMITED" and body.get("message"), body
        assert "X-App-Id" not in r.headers, "429 는 앱까지 가지 않으므로 X-App-Id 가 없어야 한다"
    for r in passed:
        assert app_of(r) in APPS  # 통과한 요청은 앱이 처리


def test_infra_rate_limit_does_not_affect_other_users_or_anonymous(infra):
    infra.begin_test()
    user = uid("limited")
    infra.burst(30, lambda i: dict(method="GET", url="/shows", headers={"X-User-Id": user}))  # user 소진
    other = infra.get("/shows", user=uid("other"))
    assert other.status_code == 200, f"다른 사용자는 영향이 없어야 한다: {other.status_code}"
    anon = infra.burst(30, lambda i: dict(method="GET", url="/shows"))  # X-User-Id 없는 요청은 제한 없음
    assert Counter(r.status_code for r in anon) == {200: 30}, Counter(r.status_code for r in anon)
    many = infra.burst(30, lambda i: dict(method="GET", url="/shows", headers={"X-User-Id": uid("m")}))
    assert Counter(r.status_code for r in many) == {200: 30}, "사용자가 다르면 각자 제한"


def test_infra_rate_limit_recovers_after_waiting(infra):
    infra.begin_test()
    user = uid("limited")
    infra.burst(30, lambda i: dict(method="GET", url="/shows", headers={"X-User-Id": user}))
    time.sleep(3)  # 초당 5건 보충
    assert infra.get("/shows", user=user).status_code == 200


# ---- 3. 장애 넘김 ----
def test_infra_get_fails_over_when_one_app_is_stopped(infra):
    infra.begin_test()
    infra.stop("app2")
    started = time.monotonic()
    apps, codes = [], []
    for _ in range(30):  # 멈춘 직후부터 10초 안에
        r = infra.get("/shows", user=uid())
        codes.append(r.status_code)
        apps.append(app_of(r))
    elapsed = time.monotonic() - started
    assert codes == [200] * 30, f"502 없이 모두 200 이어야 한다: {Counter(codes)} ({[a for a, c in zip(apps, codes) if c != 200]})"
    assert "app2" not in apps, f"멈춘 app2 가 응답했다: {Counter(apps)}"
    assert set(apps) == {"app1", "app3"}, f"app1·app3 가 처리해야 한다: {Counter(apps)}"
    assert elapsed < 10, f"10초 안에 끝나야 한다({elapsed:.1f}s)"


def test_infra_stopped_app_rejoins_without_restarting_nginx(infra):
    infra.begin_test()
    infra.stop("app2")
    for _ in range(6):
        infra.get("/shows", user=uid())
    infra.start("app2")
    seen, deadline = set(), time.monotonic() + 60
    while time.monotonic() < deadline and "app2" not in seen:
        r = infra.get("/shows", user=uid())
        if r.status_code == 200:
            seen.add(app_of(r))
        time.sleep(0.3)
    assert "app2" in seen, f"60초 안에 app2 가 다시 요청을 받아야 한다(Nginx 재시작 없이): 관측 {sorted(seen)}"


def test_infra_all_apps_stopped_get_is_502_upstream_unavailable(infra):
    infra.begin_test()
    for app in APPS:
        infra.stop(app)
    r = infra.get("/shows", user=uid())
    assert r.status_code == 502, f"{r.status_code} {r.text[:200]}"
    assert r.headers.get("content-type", "").startswith("application/json"), r.headers.get("content-type")
    body = r.json()
    assert body.get("code") == "UPSTREAM_UNAVAILABLE" and body.get("message"), body
    assert "X-App-Id" not in r.headers


# ---- 4. POST 비재시도 + 같은 키 재시도 ----
def test_infra_same_idempotency_key_retry_on_another_app_replays_first_response(infra):
    infra.begin_test()
    seat, user, key = infra.seat_ids(1)[0], uid("payer"), "retry-key-1"
    assert infra.hold(seat, user).status_code == 201
    first = infra.pay(seat, user, key)
    assert first.status_code == 202, first.text
    first_app = app_of(first)
    assert first_app in APPS
    replays = []
    for _ in range(6):  # 라운드 로빈이라 몇 번 안에 다른 앱이 받는다
        r = infra.pay(seat, user, key)
        assert r.status_code == 202, f"{r.status_code} {r.text}"
        assert r.json() == first.json(), "최초 응답과 같은 본문(같은 예매 id)이어야 한다"
        assert r.headers.get("Idempotent-Replayed") == "true"
        replays.append(app_of(r))
    assert set(replays) - {first_app}, f"다른 앱이 받은 재요청이 있어야 한다(첫 앱 {first_app}, 재요청 {replays})"
    assert set(replays) | {first_app} == set(APPS), f"세 앱 모두가 같은 키 요청을 처리했다: {replays}"
    rows = infra.bookings_in_db(seat)
    assert len(rows) == 1, f"DB 예매는 1행이어야 한다: {rows}"


def test_infra_post_to_dead_app_is_not_retried_by_nginx_and_returns_502(infra):
    """죽은 앱에 간 POST 는 Nginx 가 다른 앱으로 다시 보내지 않는다 → 502 UPSTREAM_UNAVAILABLE, 상태 불변.

    가정(결정적이지 않은 부분): POST 가 죽은 앱에 가려면 Nginx 가 그 앱을 아직 후보로 둬야 한다. 두 가지가 후보에서 빼 버린다:
    (a) 다른 요청(GET 재시도 등)이 먼저 죽은 앱을 건드려 10초 제외(max_fails=1 fail_timeout=10s),
    (b) 컨테이너가 사라진 뒤 Docker DNS 재해석이 그 이름을 지움(이 경우 앱 복귀 전까지 POST 는 죽은 앱에 아예 가지 않는다).
    대응: 죽이기 전에 좌석 조회·홀드 준비를 모두 끝내고, SIGKILL(docker compose kill)로 죽인 직후 GET 없이 바로 POST 를
    보낸다((a) 방지). 502 가 안 나오면 제외 시간(10초)이 지나길 기다렸다가 최대 3라운드 다시 시도한다. (b)가 일어나면 502 를
    볼 수 없으므로 그때는 관측 결과(어느 앱이 처리했는지)를 메시지에 남기고 실패한다 — 'Nginx 가 POST 를 재시도했다'는 뜻이 아니다.
    홀드 POST 든 결제 POST 든 502 를 받으면 '재시도되지 않았다'는 증거다(재시도됐다면 성공했을 것)."""
    infra.begin_test()
    seats = infra.seat_ids(24)  # GET 은 죽이기 전에 모두 끝낸다
    users = [uid("p") for _ in seats]
    for seat, user in zip(seats[:8], users[:8]):  # 1라운드용 홀드를 앱이 모두 살아 있을 때 만들어 둔다
        r = infra.hold(seat, user)
        assert r.status_code == 201, r.text
    infra.kill("app2")
    observed = []
    found = None  # (kind, seat, user, key, response)
    for rnd in range(3):
        for i in range(8):
            idx = rnd * 8 + i
            seat, user, key = seats[idx], users[idx], f"k-{rnd}-{i}"
            if rnd > 0:  # 2라운드부터는 홀드도 POST 로 보낸다(502 가 여기서 나와도 증거)
                h = infra.hold(seat, user)
                observed.append(("hold", h.status_code, app_of(h)))
                if h.status_code == 502:
                    found = ("hold", seat, user, key, h)
                    break
                assert h.status_code == 201, f"502 가 아니면 201 이어야 한다: {h.status_code} {h.text}"
            p = infra.pay(seat, user, key)
            observed.append(("pay", p.status_code, app_of(p)))
            if p.status_code == 502:
                found = ("pay", seat, user, key, p)
                break
            assert p.status_code == 202, f"502 가 아니면 202 여야 한다: {p.status_code} {p.text}"
        if found:
            break
        time.sleep(11)  # 죽은 앱이 후보에서 제외됐다면 fail_timeout(10초)이 지나 복귀하도록
    assert found is not None, (
        "죽은 app2 로 간 POST 가 502 를 받아야 한다(재시도됐다면 성공). 3라운드 동안 app2 로 간 POST 가 없었다"
        f"(Nginx 가 죽은 앱을 후보에서 뺐을 수 있음: 제외 시간/DNS 재해석). 관측(kind, status, X-App-Id): {observed}")
    kind, seat, user, key, r = found
    body = r.json()
    assert body.get("code") == "UPSTREAM_UNAVAILABLE" and body.get("message"), body
    assert r.headers.get("content-type", "").startswith("application/json")
    assert "X-App-Id" not in r.headers
    if kind == "hold":
        assert infra.seat_status(seat) == "AVAILABLE", "502 로 끝난 홀드는 적용되지 않았다(다른 앱으로 재시도되지 않았다)"
        retry = infra.hold(seat, user)
        assert retry.status_code == 201, f"같은 요청을 다시 보내면 된다: {retry.status_code} {retry.text}"
    else:
        assert infra.bookings_in_db(seat) == [], "502 로 끝난 결제 요청은 예매를 만들지 않았다(재시도되지 않았다)"
        retry = infra.pay(seat, user, key)  # 같은 키로 다시 보내면 된다
        assert retry.status_code == 202, f"{retry.status_code} {retry.text}"
        assert len(infra.bookings_in_db(seat)) == 1
    assert app_of(retry) in ("app1", "app3"), app_of(retry)
