import pytest

from events_support import Events


@pytest.fixture
def ev(tmp_path):
    """테스트마다 새 토픽·소비자 그룹·Redis 접두사. 끝나면(실패해도) 하위 프로세스 종료, Kafka 정지 상태 복구(복구 실패는 teardown error)."""
    e = Events(tmp_path)
    try:
        yield e
    finally:
        e.close()
