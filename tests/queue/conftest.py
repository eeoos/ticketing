import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cache"))  # ClockServer·Server·Stack 등을 tests/cache 와 공유

from cache_support import ClockServer, TEST_REDIS_DB, delete_prefix, new_prefix, redis_client, reset_database  # noqa: E402


@pytest.fixture
def prefix():
    p = new_prefix()
    yield p
    delete_prefix(p)


@pytest.fixture
def r():
    return redis_client(TEST_REDIS_DB)


@pytest.fixture
def make_server(prefix):
    """ClockServer 공장(첫 호출 때 DB 시드). 같은 clock_file·접두사를 넘기면 '다른 앱'이 된다. 끝나면 모두 종료."""
    servers = []

    def make(**kw):
        try:
            if not servers:
                reset_database()
            if servers and "clock_file" not in kw and "redis_url" not in kw:
                kw["clock_file"] = servers[0].clock_file
            srv = ClockServer(kw.pop("prefix", prefix), **kw)
            servers.append(srv)
            srv.start()
        except Exception as e:  # noqa: BLE001
            raise AssertionError(f"대기열 테스트 환경을 준비하지 못했다: {type(e).__name__}: {str(e)[-1500:]}") from e
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
def three_servers(make_server):
    """같은 시계·DB·Redis·접두사를 쓰는 서버 3개(= 앱 3대)."""
    return [make_server() for _ in range(3)]
