from pathlib import Path

from backend.db import connect

MIGRATIONS = Path(__file__).parent / "migrations"


def run(dsn: str | None = None) -> None:
    """migrations/*.sql 을 이름 순으로 한 번씩 적용한다. 재실행 안전(동시 실행은 advisory lock 으로 직렬화)."""
    with connect(dsn) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(7301)")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY)")
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in done:
                continue
            conn.execute(path.read_text())
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))


if __name__ == "__main__":
    run()
    print("migrate: ok")
