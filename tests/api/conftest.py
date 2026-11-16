import pytest

from api_support import Api


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


@pytest.fixture
def api(request):
    a = Api()
    yield a
    try:
        # 통과한 테스트는 끝에서 DB 불변식(SOLD ⇔ CONFIRMED 1건, CONFIRMED 좌석당 1건)도 지켜야 한다.
        rep = getattr(request.node, "rep_call", None)
        if rep is not None and rep.passed and a._client is not None:
            a.check_invariants()
    finally:
        a.close()


@pytest.fixture(scope="session", autouse=True)
def _api_redis_cleanup():
    yield
    try:  # 끝나면 자기 접두사 키만 지운다(개발 Redis·다른 테스트 키는 건드리지 않음)
        from api_support import API_REDIS_PREFIX
        from ticket_support import DB_API, delete_prefix, redis_for
        delete_prefix(redis_for(DB_API), API_REDIS_PREFIX)
    except Exception:  # noqa: BLE001
        pass
