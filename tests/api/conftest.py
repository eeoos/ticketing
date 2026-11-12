import pytest

from api_support import Api


@pytest.fixture
def api():
    a = Api()
    yield a
    a.close()
