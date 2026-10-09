"""Production entry point (Render, or any host that gives one admin database URL).

    python -m app.deploy

1. Reads ADMIN_DATABASE_URL (the database owner's connection string).
2. Checks the owner can create roles, then applies roles and migrations.
3. Derives DATABASE_URL (aieir_app) and STAFF_DATABASE_URL (aieir_staff) from it, using
   AIEIR_APP_PASSWORD / AIEIR_STAFF_PASSWORD.
4. Derives AIEIR_DATA_KEYS from AIEIR_DATA_KEY_SEED if keys aren't given directly.
5. Makes sure the first admin staff account exists (STAFF_BOOTSTRAP_EMAIL +
   STAFF_BOOTSTRAP_TOKEN), so someone can sign in to /staff.
6. Starts the API with uvicorn on $PORT. The app itself only ever connects as
   aieir_app / aieir_staff; the owner URL is removed from its environment.
"""
from __future__ import annotations

import base64
import hashlib
import os
import sys
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg

from .migrate import migrate


def fail(msg: str) -> None:
    print(f"[deploy] ERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def role_url(admin_url: str, user: str, password: str) -> str:
    u = urlsplit(admin_url)
    host = u.hostname or "localhost"
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}" + (f":{u.port}" if u.port else "")
    return urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment))


def data_keys() -> str:
    if os.environ.get("AIEIR_DATA_KEYS"):
        return os.environ["AIEIR_DATA_KEYS"]
    seed = os.environ.get("AIEIR_DATA_KEY_SEED")
    if not seed or len(seed) < 24:
        fail("Set AIEIR_DATA_KEYS or a long random AIEIR_DATA_KEY_SEED. Founder text is never stored unencrypted.")
    return "k1:" + base64.b64encode(hashlib.sha256(seed.encode()).digest()).decode()


def ensure_bootstrap_admin(admin_url: str) -> None:
    email, token = os.environ.get("STAFF_BOOTSTRAP_EMAIL", "").strip().lower(), os.environ.get("STAFF_BOOTSTRAP_TOKEN", "")
    if not email or not token:
        print("[deploy] No STAFF_BOOTSTRAP_EMAIL/TOKEN set; skipping first-admin setup.", flush=True)
        return
    if len(token) < 24:
        fail("STAFF_BOOTSTRAP_TOKEN must be at least 24 characters.")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with psycopg.connect(admin_url) as conn:
        sid = conn.execute(
            "INSERT INTO staff (email, name, role, safety_trained_at) VALUES (%s, %s, 'admin', now()) "
            "ON CONFLICT (email) DO UPDATE SET role = 'admin', active = true RETURNING id",
            (email, email.split("@")[0])).fetchone()[0]
        conn.execute("INSERT INTO staff_tokens (token_hash, staff_id) VALUES (%s, %s) ON CONFLICT (token_hash) DO NOTHING",
                     (token_hash, sid))
    print(f"[deploy] Admin staff account ready for {email} (sign in at /staff with STAFF_BOOTSTRAP_TOKEN).", flush=True)


def main() -> None:
    admin_url = os.environ.get("ADMIN_DATABASE_URL")
    if not admin_url:
        fail("ADMIN_DATABASE_URL is not set.")
    app_pw, staff_pw = os.environ.get("AIEIR_APP_PASSWORD"), os.environ.get("AIEIR_STAFF_PASSWORD")
    if not app_pw or not staff_pw:
        fail("Set AIEIR_APP_PASSWORD and AIEIR_STAFF_PASSWORD (long random values).")

    with psycopg.connect(admin_url) as conn:
        me = conn.execute("SELECT rolsuper, rolcreaterole FROM pg_roles WHERE rolname = current_user").fetchone()
    if not (me[0] or me[1]):
        fail("The database user can't create roles (needs CREATEROLE). Privacy depends on separate app roles.")

    applied = migrate(admin_url, app_pw, staff_pw)
    print(f"[deploy] Migrations applied: {applied or 'none new'}", flush=True)
    ensure_bootstrap_admin(admin_url)

    env = dict(os.environ)
    env["DATABASE_URL"] = role_url(admin_url, "aieir_app", app_pw)
    env["STAFF_DATABASE_URL"] = role_url(admin_url, "aieir_staff", staff_pw)
    env["AIEIR_DATA_KEYS"] = data_keys()
    for k in ("ADMIN_DATABASE_URL", "AIEIR_DATA_KEY_SEED", "STAFF_BOOTSTRAP_TOKEN", "AIEIR_APP_PASSWORD", "AIEIR_STAFF_PASSWORD"):
        env.pop(k, None)
    if not env.get("ANTHROPIC_API_KEY"):
        print("[deploy] WARNING: ANTHROPIC_API_KEY is not set; the coach and classifier can't reply.", flush=True)
    port = env.get("PORT", "8000")
    print(f"[deploy] Starting API on port {port}", flush=True)
    os.execvpe(sys.executable, [sys.executable, "-m", "uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", port,
                                "--proxy-headers", "--forwarded-allow-ips", "*"], env)


if __name__ == "__main__":
    main()
