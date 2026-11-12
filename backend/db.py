import os

import psycopg

DEFAULT_DSN = "postgresql://ticketing:ticketing@localhost:5432/ticketing"


def resolve_dsn(dsn: str | None = None) -> str:
    """dsn 인자 > DATABASE_URL > 기본값. 연결할 때마다 읽는다."""
    return dsn or os.environ.get("DATABASE_URL") or DEFAULT_DSN


def connect(dsn: str | None = None) -> psycopg.Connection:
    return psycopg.connect(resolve_dsn(dsn))
