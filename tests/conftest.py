"""Test fixtures. Database tests need a Postgres 16 superuser URL in ADMIN_DATABASE_URL
(locally: eval "$(scripts/local_postgres.sh start)"). Each session gets a fresh database;
each test starts from empty tables.

No test calls a real model. `fake` is a scripted stand-in that records every prompt, which
is what lets the privacy tests prove what did and didn't reach the model."""
from __future__ import annotations

import base64
import os
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

os.environ.setdefault("AIEIR_DATA_KEYS", "test1:" + base64.b64encode(os.urandom(32)).decode())
os.environ.setdefault("ENABLE_ALERT_WORKER", "0")
os.environ.setdefault("ONCALL_PRIMARY_EMAIL", "primary@oncall.test")
os.environ.setdefault("ONCALL_SECONDARY_EMAIL", "secondary@oncall.test")

from app import alerts, db, llm as llm_mod  # noqa: E402
from app.migrate import migrate  # noqa: E402

ADMIN = os.environ.get("ADMIN_DATABASE_URL")
TABLES = ["audit_log", "reviews", "alerts", "zone_events", "feedback", "messages", "reflections", "sessions", "weekly_kpis",
          "roadmaps", "intake_interviews", "startups", "founder_state", "auth_tokens", "staff_tokens", "staff", "users"]


@pytest.fixture(scope="session")
def database():
    if not ADMIN:
        pytest.skip("ADMIN_DATABASE_URL not set (see scripts/local_postgres.sh)")
    name = f"aieir_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    admin_url = ADMIN.split("?")[0].rsplit("/", 1)[0] + "/" + name
    migrate(admin_url, "app-test-pw", "staff-test-pw")
    host_part = admin_url.split("@", 1)[1]
    app_url = f"postgresql://aieir_app:app-test-pw@{host_part}"
    staff_url = f"postgresql://aieir_staff:staff-test-pw@{host_part}"
    os.environ["DATABASE_URL"], os.environ["STAFF_DATABASE_URL"] = app_url, staff_url
    app_pool = ConnectionPool(app_url, min_size=1, max_size=4, kwargs={"row_factory": dict_row}, open=True)
    staff_pool = ConnectionPool(staff_url, min_size=1, max_size=4, kwargs={"row_factory": dict_row}, open=True)
    db.set_pools(app_pool, staff_pool)
    yield {"admin": admin_url, "app": app_url, "staff": staff_url}
    app_pool.close()
    staff_pool.close()
    with psycopg.connect(ADMIN, autocommit=True) as c:
        c.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture
def clean(database):
    with psycopg.connect(database["admin"], autocommit=True) as c:
        c.execute("TRUNCATE " + ", ".join(TABLES) + " CASCADE")
    return database


@pytest.fixture
def admin_conn(clean):
    with psycopg.connect(clean["admin"], autocommit=True, row_factory=dict_row) as c:
        yield c


class Script:
    """Programmable fake model. Set .zone / .kpis / .reply to steer it."""

    def __init__(self):
        self.zone = {"zone": "GREEN", "confidence": 0.9, "categories": [], "rationale": "Routine update."}
        self.reply = "Thanks for telling me. What got in the way?\n<flow>proceed</flow>"
        self.kpis = {"professional": [{"title": "Interview 5 customers"}, {"title": "Ship the waitlist page"},
                                      {"title": "Email 10 suppliers"}],
                     "personal": [{"title": "Gym 2 days"}, {"title": "Sleep 7+ hours on 5 nights"}]}
        self.roadmap = {"milestone": "50 pre-orders", "weeks": [{"week": i, "focus": f"Focus {i}", "outcomes": ["x"]} for i in range(1, 5)]}
        self.fail_classifier = False

    def __call__(self, system, messages, model, tool):
        name = tool["name"] if tool else None
        if name == "record_zone":
            if self.fail_classifier:
                raise RuntimeError("classifier down")
            z = dict(self.zone)
            z.setdefault("explicit_danger", z.get("zone") == "RED" and z.get("confidence", 0) >= 0.9)
            return z
        if name == "save_kpis":
            return self.kpis
        if name == "save_roadmap":
            return self.roadmap
        return self.reply


@pytest.fixture
def fake():
    script = Script()
    f = llm_mod.FakeLLM(responder=script)
    f.script = script
    llm_mod.set_default_llm(f)
    yield f
    llm_mod.set_default_llm(None)


@pytest.fixture
def mailbox():
    box = alerts.MemorySender()
    alerts.set_sender(box)
    alerts.RUN_INLINE = True
    yield box
    alerts.set_sender(None)
    alerts.RUN_INLINE = False


@pytest.fixture
def client(clean, fake, mailbox):
    from fastapi.testclient import TestClient
    from app.api import app
    with TestClient(app) as c:
        yield c


def signup(client, email="sam@example.com", name="Sam", country="US") -> dict:
    from app.services import CONSENT_VERSION
    r = client.post("/signup", json={"email": email, "name": name, "country": country,
                                     "consent": {"version": CONSENT_VERSION, "accepted": True}})
    assert r.status_code == 200, r.text
    tok = r.json()["token"]
    return {"Authorization": f"Bearer {tok}", "_uid": str(db.user_for_token(tok))}


def hdr(h: dict) -> dict:
    return {k: v for k, v in h.items() if not k.startswith("_")}


def make_staff(clean, role="safety_reviewer", email=None) -> dict:
    from app.staff_admin import create
    tok = create(clean["admin"], email or f"{role}-{uuid.uuid4().hex[:4]}@eir.test", role.title(), role, "2026-09-01")
    return {"Authorization": f"Bearer {tok}"}


def finish_intake(client, h, answers=None):
    answers = answers or ["Thread & Thimble: made-to-order linen", "Busy professionals", "Launch in 100 days", "Prototype",
                          "Finding a tailor", "30", "Solo", "Sleep 7h, gym twice", "Client work", "direct"]
    for a in answers:
        r = client.post("/intake/answer", json={"answer": a}, headers=hdr(h))
        assert r.status_code == 200, r.text
    return r.json()
