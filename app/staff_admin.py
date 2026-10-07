"""Create or deactivate staff accounts. Runs with the admin database URL because it mints
tokens, which no application role may do.

    python -m app.staff_admin create --email a@x.org --name "Asha" --role safety_reviewer --trained 2026-09-01
    python -m app.staff_admin deactivate --email a@x.org

Only people who have completed safety training get accounts (--trained is required).
"""
from __future__ import annotations

import argparse
import os
import secrets

import psycopg

from .db import hash_token


def create(admin_url: str, email: str, name: str, role: str, trained: str) -> str:
    token = "staff_" + secrets.token_urlsafe(32)
    with psycopg.connect(admin_url) as conn:
        sid = conn.execute("INSERT INTO staff (email, name, role, safety_trained_at) VALUES (%s, %s, %s, %s) RETURNING id",
                           (email.lower(), name, role, trained)).fetchone()[0]
        conn.execute("INSERT INTO staff_tokens (token_hash, staff_id) VALUES (%s, %s)", (hash_token(token), sid))
    return token


def deactivate(admin_url: str, email: str) -> None:
    with psycopg.connect(admin_url) as conn:
        conn.execute("UPDATE staff SET active = false WHERE email = %s", (email.lower(),))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--email", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--role", required=True, choices=["eir", "safety_reviewer", "admin"])
    c.add_argument("--trained", required=True, help="date safety training was completed, YYYY-MM-DD")
    d = sub.add_parser("deactivate")
    d.add_argument("--email", required=True)
    a = ap.parse_args()
    url = os.environ["ADMIN_DATABASE_URL"]
    if a.cmd == "create":
        print("Staff token (shown once, store it in a password manager):")
        print(create(url, a.email, a.name, a.role, a.trained))
    else:
        deactivate(url, a.email)
        print("deactivated")
