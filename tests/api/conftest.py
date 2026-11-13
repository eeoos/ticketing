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
