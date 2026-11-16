"""시각을 파일로 조종하는 실제 HTTP 서버(테스트 전용 실행기). tests/cache 의 ClockServer 가 하위 프로세스로 띄운다.

backend.main.app 을 그대로 쓰고 get_now 만 override 한다: 매 호출마다 CACHE_TEST_CLOCK_FILE 의 ISO 시각을 읽는다.
Redis TTL 은 실제 시계로 흐르고 get_now 는 테스트가 정하므로 'Redis TTL 이 아니라 get_now 기준'인지 검증할 수 있다.
"""
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import uvicorn  # noqa: E402

from backend.main import app, get_now  # noqa: E402

clock_file = Path(os.environ["CACHE_TEST_CLOCK_FILE"])
app.dependency_overrides[get_now] = lambda: datetime.fromisoformat(clock_file.read_text().strip())

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["CACHE_TEST_PORT"]), log_level="warning")
