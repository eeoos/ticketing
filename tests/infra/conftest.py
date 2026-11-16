import pytest

from infra_support import Infra

_INFRA = Infra()


@pytest.fixture(scope="session", autouse=True)
def _infra_final_restore():
    yield
    _INFRA.finalize()  # 복구 실패는 예외 → 세션 teardown error 로 드러난다(경고로 삼키지 않는다)


@pytest.fixture
def infra():
    """준비는 테스트 본문에서 첫 요청 때 일어난다(실패해도 테스트마다 개별 실패)."""
    try:
        yield _INFRA
    finally:
        if _INFRA.stopped:  # 이 테스트가 멈춘 앱은 다음 테스트 전에 반드시 켠다. 실패하면 teardown error.
            _INFRA.restore_apps()
