"""Apply db/roles.sql and db/migrations/*.sql in order. Run as a database admin:

    python -m app.migrate postgresql://admin@host/aieir
"""
from __future__ import annotations

import sys
from pathlib import Path

import psycopg

DB_DIR = Path(__file__).resolve().parent.parent / "db"


def migrate(admin_url: str, app_password: str | None = None, staff_password: str | None = None) -> list[str]:
    applied: list[str] = []
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute((DB_DIR / "roles.sql").read_text())
        for role, pw in (("aieir_app", app_password), ("aieir_staff", staff_password)):
            if pw:
                conn.execute(psycopg.sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    psycopg.sql.Identifier(role), psycopg.sql.Literal(pw)))
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz DEFAULT now())")
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
        for f in sorted((DB_DIR / "migrations").glob("*.sql")):
            if f.name in done:
                continue
            with conn.transaction():
                conn.execute(f.read_text())
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
            applied.append(f.name)
    return applied


if __name__ == "__main__":
    import os
    url = sys.argv[1] if len(sys.argv) > 1 else os.environ["ADMIN_DATABASE_URL"]
    print("applied:", migrate(url, os.environ.get("AIEIR_APP_PASSWORD"), os.environ.get("AIEIR_STAFF_PASSWORD"))
          or "nothing new")
