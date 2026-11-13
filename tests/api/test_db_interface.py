"""backend.db 인터페이스 약속 (docs/backend-interface.md): 앱은 DATABASE_URL 을 import 시점이 아니라 '연결할 때' 읽는다.

같은 프로세스에서 DATABASE_URL 을 바꾸면 이후 app_connection() 은 새 DB 로 가야 한다.
개발 DB(ticketing)는 쓰지 않는다. 환경변수는 테스트가 끝나면 원복된다.
"""
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

import api_support
from api_support import DEV_DB_NAME, TEST_DSN

ALT_DB = "ticketing_test_alt"


def _ensure_alt_db():
    admin = make_conninfo(TEST_DSN, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (ALT_DB,)).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(ALT_DB)))
    return make_conninfo(TEST_DSN, dbname=ALT_DB)


def _current_database():
    from backend import db  # noqa: PLC0415
    with db.app_connection() as conn:
        return conn.execute("SELECT current_database()").fetchone()[0]


def test_interface_database_url_switch_is_honored_by_next_app_connection(monkeypatch):
    test_name = conninfo_to_dict(TEST_DSN)["dbname"]
    assert test_name != DEV_DB_NAME and ALT_DB not in (test_name, DEV_DB_NAME)
    api_support._ensure_database()  # 테스트 DB 가 없으면 만들고 개발 DB 면 거절
    alt_dsn = _ensure_alt_db()
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)  # 끝나면 monkeypatch 가 원래 값으로 되돌린다
    first = _current_database()
    assert first == test_name, f"DATABASE_URL={test_name} 인데 {first} 로 연결됐다"
    monkeypatch.setenv("DATABASE_URL", alt_dsn)
    second = _current_database()
    assert second == ALT_DB, (
        f"DATABASE_URL 을 {ALT_DB} 로 바꿨는데 새 app_connection() 이 {second} 로 연결됐다 "
        "(연결할 때 DATABASE_URL 을 읽어야 한다)")
