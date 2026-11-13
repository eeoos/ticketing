import pytest

from concurrency_support import Server, concurrency_dsn, reset_database

EVIDENCE_KEY = pytest.StashKey[list]()


@pytest.fixture
def dsn():
    return concurrency_dsn()


@pytest.fixture
def server(dsn):
    """시나리오마다 DB 를 초기화하고 uvicorn(워커 4)을 새로 띄운다."""
    srv = None
    try:  # 초기화·start 가 실패해도 하위 프로세스를 반드시 정리한다
        try:
            reset_database(dsn)
            srv = Server(dsn)
            srv.start()
        except Exception as e:  # noqa: BLE001
            raise AssertionError(f"동시성 테스트 환경을 준비하지 못했다: {type(e).__name__}: {e}") from e
        yield srv
    finally:
        if srv is not None:
            srv.stop()


@pytest.fixture
def report(request):
    """겹침 증거 한 줄을 남긴다. 통과해도 pytest 요약 끝에 출력된다."""
    def add(name, text):
        request.config.stash.setdefault(EVIDENCE_KEY, []).append(f"{name}: {text}")
    return add


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    lines = config.stash.get(EVIDENCE_KEY, [])
    if lines:
        terminalreporter.section("동시성 겹침 증거")
        for line in lines:
            terminalreporter.write_line(line)
