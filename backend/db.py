import atexit
import os
import threading

import psycopg
from psycopg_pool import ConnectionPool

DEFAULT_DSN = "postgresql://ticketing:ticketing@localhost:5432/ticketing"


def resolve_dsn(dsn: str | None = None) -> str:
    """dsn 인자 > DATABASE_URL > 기본값. 연결할 때마다 읽는다."""
    return dsn or os.environ.get("DATABASE_URL") or DEFAULT_DSN


def connect(dsn: str | None = None, application_name: str | None = None) -> psycopg.Connection:
    """앱 연결은 application_name='ticketing-api-<PID>' 를 달아 pg_stat_activity 에서 워커별로 구분한다.
    PID 는 연결할 때 읽는다(워커 fork 이후 값)."""
    return psycopg.connect(resolve_dsn(dsn), application_name=application_name or f"ticketing-api-{os.getpid()}")


_pool: ConnectionPool | None = None
_pool_key: tuple[int, str] | None = None  # (PID, DSN) — 이 키가 바뀌면 풀을 새로 만든다
_pool_lock = threading.Lock()


def _close_current_pool():
    if _pool is not None and _pool_key and _pool_key[0] == os.getpid():
        _pool.close()


atexit.register(_close_current_pool)


def _get_pool() -> ConnectionPool:
    """워커(프로세스)당 풀. 처음 쓸 때(fork 이후) 만들고, DATABASE_URL 은 연결할 때마다 읽는다.
    (PID, DSN) 이 바뀌면 새 풀로 교체한다. 같은 프로세스의 이전 풀은 close() 하는데, 빌려 간 연결은
    반납될 때 닫히므로 사용 중인 요청은 끝까지 간다. 다른 PID(fork 전 부모) 의 풀은 닫지 않고 버린다."""
    global _pool, _pool_key
    pid, dsn = os.getpid(), resolve_dsn()
    key = (pid, dsn)
    pool = _pool
    if pool is not None and _pool_key == key:
        return pool
    with _pool_lock:
        if _pool is None or _pool_key != key:
            old, old_key = _pool, _pool_key
            _pool = ConnectionPool(
                dsn,
                min_size=1,
                max_size=int(os.environ.get("DB_POOL_MAX_SIZE", "10")),
                kwargs={"application_name": f"ticketing-api-{pid}"},
                open=True,
            )
            _pool_key = key
            if old is not None and old_key and old_key[0] == pid:
                old.close()
        return _pool


def app_connection():
    """요청용 연결(풀에서 빌림). `with app_connection() as conn:` — 정상 종료 시 commit, 예외 시 rollback 후 반납."""
    return _get_pool().connection()
