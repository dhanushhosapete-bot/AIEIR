"""Retention purge. Run daily (cron or a scheduled job) with the admin database URL:

    RETENTION_DAYS=365 python -m app.retention

Deletes messages, reflections and zone events older than the retention period (default 12
months), except safety events still tied to an unresolved RED alert. Audit log rows are
kept for at least two years so staff access stays reviewable.
"""
from __future__ import annotations

import json
import os

import psycopg

DEFAULT_DAYS = 365


def purge(admin_url: str, days: int | None = None) -> dict:
    days = int(days or os.environ.get("RETENTION_DAYS", DEFAULT_DAYS))
    with psycopg.connect(admin_url) as conn:
        return conn.execute("SELECT purge_expired(%s)", (days,)).fetchone()[0]


if __name__ == "__main__":
    print(json.dumps(purge(os.environ["ADMIN_DATABASE_URL"])))
