import pytest

from cache_support import (APPS, ClockServer, RedisStopped, TEST_REDIS_DB, delete_prefix, new_prefix, redis_client,
                           reset_database)

EVIDENCE_KEY = pytest.StashKey[list]()


@pytest.fixture
def prefix():
    """실행(테스트)마다 다른 Redis 접두사. 끝나면 자기 키만 지운다."""
    p = new_prefix()
    yield p
    delete_prefix(p)


@pytest.fixture
def r():
    return redis_client(TEST_REDIS_DB)


@pytest.fixture
def make_server(prefix):
    """ClockServer 공장. 첫 호출 때 DB 를 시드한다(이전 테스트 데이터 제거). 끝나면 모든 서버를 종료."""
    servers = []

    def make(**kw):
        try:
            if not servers:
                reset_database()
            srv = ClockServer(kw.pop("prefix", prefix), **kw)
            servers.append(srv)
            srv.start()
        except Exception as e:  # noqa: BLE001  (구현 없음·서버 시작 실패도 테스트마다 개별 실패로)
            raise AssertionError(f"캐시 테스트 환경을 준비하지 못했다: {type(e).__name__}: {str(e)[-1500:]}") from e
        return srv

    try:
        yield make
    finally:
        for s in servers:
            s.stop()


@pytest.fixture
def server(make_server):
    return make_server()


@pytest.fixture
def redis_down():
    """docker compose stop redis, 끝나면(실패해도) start. 복구 실패는 teardown error."""
    rs = RedisStopped()
    try:
        yield rs
    finally:
        rs.restore()


@pytest.fixture
def report(request):
    def add(name, text):
        request.config.stash.setdefault(EVIDENCE_KEY, []).append(f"{name}: {text}")
    return add


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    lines = config.stash.get(EVIDENCE_KEY, [])
    if lines:
        terminalreporter.section("캐시·관문 측정값")
        for line in lines:
            terminalreporter.write_line(line)
