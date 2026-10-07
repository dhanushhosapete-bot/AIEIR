"""Database access.

Founder requests use `user_session(user_id)`: role aieir_app, scoped by app.user_id, so
row-level security shows only that founder's rows. Staff requests use
`staff_session(staff)`: role aieir_staff, scoped by app.staff_id / app.staff_role.

Scopes are set with set_config(..., is_local => true), so they end with the transaction
and never leak into the next request that reuses a pooled connection. There is no
unscoped helper for founder tables.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

_pools: dict[str, ConnectionPool] = {}
SYSTEM_STAFF_ID = uuid.UUID("00000000-0000-0000-0000-00000000a1e7")  # background jobs (alert escalation)


def _url(kind: str) -> str:
    if kind == "staff":
        return os.environ.get("STAFF_DATABASE_URL", settings.database_url.replace("aieir_app", "aieir_staff"))
    return settings.database_url


def pool(kind: str = "app") -> ConnectionPool:
    if kind not in _pools:
        _pools[kind] = ConnectionPool(_url(kind), min_size=1, max_size=10,
                                      kwargs={"row_factory": dict_row}, open=True)
    return _pools[kind]


def set_pools(app: ConnectionPool | None, staff: ConnectionPool | None) -> None:
    """Used by tests to point the app at a throwaway database."""
    _pools.clear()
    if app:
        _pools["app"] = app
    if staff:
        _pools["staff"] = staff


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@contextmanager
def user_session(user_id: uuid.UUID | str) -> Iterator[psycopg.Connection]:
    """A transaction in which only `user_id`'s rows are visible or writable."""
    uid = str(uuid.UUID(str(user_id)))  # rejects anything that isn't a UUID
    with pool("app").connection() as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('app.user_id', %s, true)", (uid,))
            yield conn


@dataclass(frozen=True)
class Staff:
    id: uuid.UUID
    role: str  # eir | safety_reviewer | admin


@contextmanager
def staff_session(staff: Staff) -> Iterator[psycopg.Connection]:
    with pool("staff").connection() as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('app.staff_id', %s, true), set_config('app.staff_role', %s, true)",
                         (str(staff.id), staff.role))
            yield conn


@contextmanager
def anonymous_session(kind: str = "app") -> Iterator[psycopg.Connection]:
    """No founder or staff scope: protected tables return zero rows. Only for sign-up and
    token lookup, which go through SECURITY DEFINER functions."""
    with pool(kind).connection() as conn:
        with conn.transaction():
            yield conn


def user_for_token(token: str) -> uuid.UUID | None:
    with anonymous_session() as conn:
        row = conn.execute("SELECT auth_user_by_token(%s) AS id", (hash_token(token),)).fetchone()
        return row["id"] if row and row["id"] else None


def staff_for_token(token: str) -> Staff | None:
    with anonymous_session("staff") as conn:
        row = conn.execute("SELECT * FROM auth_staff_by_token(%s)", (hash_token(token),)).fetchone()
        return Staff(row["staff_id"], row["role"]) if row else None


def create_founder(email: str, name: str | None, country: str | None, token: str, consent_version: str) -> uuid.UUID:
    with anonymous_session() as conn:
        row = conn.execute("SELECT create_founder(%s, %s, %s, %s, %s) AS id",
                           (email, name, country, hash_token(token), consent_version)).fetchone()
        return row["id"]
