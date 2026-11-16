"""세션 전체 프로세스 간 잠금: 한 번에 한 pytest 실행만 공유 자원을 쓴다.

여러 자리(또는 터미널)가 동시에 `uv run pytest` 를 돌리면 같은 테스트 DB(ticketing_api/_test, ticketing_concurrency,
ticketing_cache, ticketing_infra)·Redis·도커 스택을 서로 다시 시드·재생성해 서로의 테스트를 깨뜨린다(코드 결함이 아닌 실행 충돌).
그래서 세션 시작 때 fcntl.flock 을 잡고 끝날 때 놓는다. 이미 다른 실행이 잠금을 쥐고 있으면 '기다리는 중'을 출력하며 기다린다.

- 잠금 파일: /tmp/ticketing-tests.lock (환경변수 TICKETING_TESTS_LOCK 으로 변경). 저장소 밖이라 여러 작업 디렉터리가 공유한다.
- 프로세스가 죽으면 OS 가 잠금을 풀어 준다(낡은 잠금이 남지 않는다).
- 이 세션이 띄운 하위 pytest(예: tests/cache 의 Redis 정지 테스트가 tests/concurrency 를 하위로 실행)는 환경변수
  TICKETING_TESTS_LOCK_HELD 를 상속해 잠금을 다시 잡지 않는다(안 그러면 부모가 쥔 잠금에 자식이 영원히 막힌다).
- 비상구: TICKETING_TESTS_NO_LOCK=1 이면 잠금을 쓰지 않는다(공유 자원을 안 쓰는 빠른 실행을 확신할 때만).
"""
import fcntl
import os
import sys
import time

LOCK_PATH = os.environ.get("TICKETING_TESTS_LOCK", "/tmp/ticketing-tests.lock")
HELD_ENV = "TICKETING_TESTS_LOCK_HELD"
_lock_file = None


def _say(msg):
    sys.__stderr__.write(f"[tests] {msg}\n")
    sys.__stderr__.flush()


def _holder_info(f):
    try:
        f.seek(0)
        return f.read().strip() or "알 수 없음"
    except OSError:
        return "알 수 없음"


def pytest_sessionstart(session):
    global _lock_file
    if os.environ.get("TICKETING_TESTS_NO_LOCK") or os.environ.get(HELD_ENV):
        return  # 비상구이거나, 잠금을 쥔 부모 pytest 가 띄운 하위 실행
    f = open(LOCK_PATH, "a+")
    started = time.monotonic()
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _say(f"다른 pytest 실행을 기다리는 중 (잠금 {LOCK_PATH}; 현재 실행: {_holder_info(f)})")
        last_report = started
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.5)
                if time.monotonic() - last_report >= 30:
                    _say(f"여전히 다른 pytest 실행을 기다리는 중 ({int(time.monotonic() - started)}초 경과; 현재 실행: {_holder_info(f)})")
                    last_report = time.monotonic()
        _say(f"잠금을 얻었다 ({int(time.monotonic() - started)}초 기다림)")
    f.seek(0)
    f.truncate()
    f.write(f"pid {os.getpid()}, cwd {os.getcwd()}, 시작 {time.strftime('%H:%M:%S')}, 인자 {' '.join(sys.argv[1:]) or '(없음)'}")
    f.flush()
    os.environ[HELD_ENV] = str(os.getpid())
    _lock_file = f


def pytest_sessionfinish(session, exitstatus):
    global _lock_file
    if _lock_file is not None:
        try:
            _lock_file.seek(0)
            _lock_file.truncate()
            fcntl.flock(_lock_file, fcntl.LOCK_UN)
        finally:
            _lock_file.close()
            _lock_file = None
        os.environ.pop(HELD_ENV, None)
