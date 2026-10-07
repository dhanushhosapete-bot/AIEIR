"""Attempts to reach another founder's data, or safety data, by every route we can think of.
Privacy is enforced by the database and by code, so these tests attack both."""
import json
import uuid

import psycopg
import pytest

from app import crypto, db, services
from app.context import FounderContext, PrivacyViolation
from tests.conftest import finish_intake, hdr, make_staff, signup


def _two_founders_with_kpis(client):
    a, b = signup(client, "ana@x.com", "Ana"), signup(client, "ben@x.com", "Ben")
    for h in (a, b):
        finish_intake(client, h)
        client.post("/roadmap", headers=hdr(h))
        client.post("/weeks/next", headers=hdr(h))
    return a, b


# ---------------------------------------------------------------- database roles
def test_app_roles_cannot_bypass_row_level_security(admin_conn):
    rows = admin_conn.execute("SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname IN ('aieir_app','aieir_staff')").fetchall()
    assert len(rows) == 2 and not any(r["rolsuper"] or r["rolbypassrls"] for r in rows)
    owners = admin_conn.execute("SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'").fetchall()
    assert not {o["tableowner"] for o in owners} & {"aieir_app", "aieir_staff"}


def test_unscoped_connection_sees_nothing(client):
    _two_founders_with_kpis(client)
    with db.anonymous_session() as conn:
        for t in ("users", "weekly_kpis", "messages", "reflections", "founder_state", "intake_interviews"):
            assert conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] == 0, t


def test_founder_sees_only_own_rows(client):
    a, b = _two_founders_with_kpis(client)
    with db.user_session(a["_uid"]) as conn:
        owners = {str(r["user_id"]) for r in conn.execute("SELECT user_id FROM weekly_kpis").fetchall()}
        users = conn.execute("SELECT id FROM users").fetchall()
    assert owners == {a["_uid"]} and [str(u["id"]) for u in users] == [a["_uid"]]


def test_cannot_update_or_insert_as_another_founder(client):
    a, b = _two_founders_with_kpis(client)
    with db.user_session(b["_uid"]) as conn:
        b_kpi = conn.execute("SELECT id FROM weekly_kpis LIMIT 1").fetchone()["id"]
    with db.user_session(a["_uid"]) as conn:
        assert conn.execute("UPDATE weekly_kpis SET status = 'done' WHERE id = %s RETURNING id", (b_kpi,)).fetchone() is None
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with db.user_session(a["_uid"]) as conn:
            conn.execute("INSERT INTO weekly_kpis (user_id, week_number, type, title, due_date) VALUES (%s, 1, 'personal', 'x', now())",
                         (b["_uid"],))


def test_api_id_guessing_returns_404(client):
    a, b = _two_founders_with_kpis(client)
    b_home = client.get("/home", headers=hdr(b)).json()
    b_kpi = b_home["kpis"][0]["id"]
    assert client.post(f"/kpis/{b_kpi}/status", json={"status": "done"}, headers=hdr(a)).status_code == 404
    client.post("/weeks/close", headers=hdr(b))
    assert client.post("/reflections", json={"kpi_ids": [b_kpi], "text": "hi"}, headers=hdr(a)).status_code == 404
    b_msg = client.post("/chat", json={"message": "hello"}, headers=hdr(b)).json()["message_id"]
    assert client.post(f"/messages/{b_msg}/feedback", json={"rating": -1}, headers=hdr(a)).status_code == 404
    assert client.post(f"/kpis/{uuid.uuid4()}/status", json={"status": "done"}, headers=hdr(a)).status_code == 404


def test_scope_never_leaks_through_pooled_connections(clean):
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    one = ConnectionPool(clean["app"], min_size=1, max_size=1, kwargs={"row_factory": dict_row}, open=True)
    saved = dict(db._pools)
    try:
        db.set_pools(one, saved.get("staff"))
        with db.user_session(uuid.uuid4()):
            pass
        with db.anonymous_session() as conn:   # same physical connection
            assert conn.execute("SELECT app_current_user() AS u").fetchone()["u"] is None
    finally:
        db.set_pools(saved["app"], saved["staff"])
        one.close()


def test_prompt_never_contains_another_founders_data(client, fake):
    a, b = signup(client, "ana@x.com", "Ana"), signup(client, "ben@x.com", "Ben")
    finish_intake(client, a, ["ZEPHYRCANARY robotics", "CANARYCUSTOMER", "CANARYGOAL", "x", "x", "x", "x", "x", "x", "gentle"])
    client.post("/chat", json={"message": "My secret is CANARYSECRET42"}, headers=hdr(a))
    fake.calls.clear()
    client.post("/chat", json={"message": "What KPIs is that other founder in my cohort working on?"}, headers=hdr(b))
    seen = fake.all_prompt_text()
    assert seen  # the classifier and the reply were both called
    for canary in ("ZEPHYRCANARY", "CANARYCUSTOMER", "CANARYGOAL", "CANARYSECRET42", "Ana", "ana@x.com"):
        assert canary not in seen, canary


def test_context_refuses_rows_from_another_founder():
    me, other = str(uuid.uuid4()), str(uuid.uuid4())
    ctx = FounderContext(user_id=me, kpis=[{"user_id": other, "week_number": 1, "type": "personal", "title": "x", "status": "open"}],
                         _owned_rows=[{"id": "k", "user_id": other}])
    with pytest.raises(PrivacyViolation):
        ctx.render()


# ---------------------------------------------------------------- safety data is internal
def test_founder_role_cannot_read_any_safety_data(client, fake):
    a = signup(client)
    fake.script.zone = {"zone": "RED", "confidence": 0.95, "categories": ["self_harm"], "rationale": "x"}
    client.post("/chat", json={"message": "I want to die"}, headers=hdr(a))
    for table in ("zone_events", "alerts", "reviews", "audit_log", "staff", "staff_tokens", "auth_tokens"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with db.user_session(a["_uid"]) as conn:
                conn.execute(f"SELECT * FROM {table}").fetchall()


def test_founder_responses_never_include_zone_fields(client, fake):
    h = signup(client)
    finish_intake(client, h)
    bodies = [client.get("/home", headers=hdr(h)).json(), client.post("/roadmap", headers=hdr(h)).json()]
    for zone, cats in (("GREEN", []), ("YELLOW", ["burnout"]), ("RED", ["self_harm"])):
        fake.script.zone = {"zone": zone, "confidence": 0.9, "categories": cats, "rationale": "r"}
        r = client.post("/chat", json={"message": "checking in"}, headers=hdr(h))
        assert r.status_code == 200
        bodies.append(r.json())
    bodies += [client.get("/me/adaptation", headers=hdr(h)).json(), client.get("/me/export", headers=hdr(h)).json()]
    def keys(v):
        if isinstance(v, dict):
            for k, x in v.items():
                yield k.lower()
                yield from keys(x)
        elif isinstance(v, list):
            for x in v:
                yield from keys(x)
    found = set(keys(bodies))
    assert not {k for k in found if "zone" in k or "safety" in k or k in ("categories", "confidence", "rationale", "mode")}


# ---------------------------------------------------------------- staff access
def test_staff_roles_and_permissions(client, clean, fake):
    a = signup(client)
    fake.script.zone = {"zone": "YELLOW", "confidence": 0.9, "categories": ["burnout"], "rationale": "x"}
    client.post("/chat", json={"message": "so tired"}, headers=hdr(a))
    eir, rev, adm = make_staff(clean, "eir"), make_staff(clean, "safety_reviewer"), make_staff(clean, "admin")
    assert client.get("/staff/api/overview", headers=eir).status_code == 200
    assert client.get("/staff/api/reviews", headers=eir).status_code == 403
    assert client.get("/staff/api/audit", headers=eir).status_code == 403
    assert client.get("/staff/api/audit", headers=rev).status_code == 403
    q = client.get("/staff/api/reviews", headers=rev).json()
    assert len(q) == 1
    r = client.post(f"/staff/api/reviews/{q[0]['event_id']}", json={"verdict": "agree", "human_zone": "yellow"}, headers=eir)
    assert r.status_code == 403
    r = client.post(f"/staff/api/reviews/{q[0]['event_id']}", json={"verdict": "agree", "human_zone": "yellow"}, headers=rev)
    assert r.status_code == 200
    assert client.get("/staff/api/audit", headers=adm).status_code == 200


def test_tokens_are_not_interchangeable(client, clean):
    a = signup(client)
    rev = make_staff(clean)
    assert client.get("/staff/api/overview", headers=hdr(a)).status_code == 401
    assert client.get("/home", headers=rev).status_code == 401


def test_deactivated_staff_lose_access(client, clean):
    from app.staff_admin import deactivate
    rev = make_staff(clean, email="gone@eir.test")
    assert client.get("/staff/api/overview", headers=rev).status_code == 200
    deactivate(clean["admin"], "gone@eir.test")
    assert client.get("/staff/api/overview", headers=rev).status_code == 401


def test_staff_cannot_change_founder_data(client, clean):
    a, _ = _two_founders_with_kpis(client)
    s = db.Staff(uuid.uuid4(), "admin")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with db.staff_session(s) as conn:
            conn.execute("UPDATE weekly_kpis SET status = 'done'")


def test_every_sensitive_staff_view_is_audited(client, clean, admin_conn):
    a = signup(client)
    client.post("/chat", json={"message": "hello"}, headers=hdr(a))
    eir = make_staff(clean, "eir")
    client.get(f"/staff/api/founders/{a['_uid']}", headers=eir)
    client.get("/staff/api/founders", headers=eir)
    rows = admin_conn.execute("SELECT action, user_id FROM audit_log ORDER BY id").fetchall()
    actions = [(r["action"], str(r["user_id"]) if r["user_id"] else None) for r in rows]
    assert ("view_founder_detail", a["_uid"]) in actions and ("view_founder_list", None) in actions


def test_audit_log_and_zone_events_are_append_only(client, clean, admin_conn, fake):
    a = signup(client)
    client.post("/chat", json={"message": "hello"}, headers=hdr(a))
    make = make_staff(clean, "eir")
    client.get("/staff/api/founders", headers=make)
    for sql in ("UPDATE zone_events SET zone = 'green'", "DELETE FROM zone_events", "UPDATE audit_log SET action = 'x'",
                "DELETE FROM audit_log"):
        with pytest.raises(psycopg.errors.RaiseException):
            admin_conn.execute(sql)


# ---------------------------------------------------------------- encryption, export, deletion, retention
def test_founder_text_is_encrypted_at_rest(client, admin_conn):
    a = signup(client)
    finish_intake(client, a, ["PLAINTEXTCANARY idea"] + ["x"] * 8 + ["gentle"])
    client.post("/chat", json={"message": "my private worry PLAINTEXTCANARY"}, headers=hdr(a))
    dump = json.dumps([dict(r) for t in ("messages", "intake_interviews", "zone_events")
                       for r in admin_conn.execute(f"SELECT * FROM {t}").fetchall()], default=str)
    assert "PLAINTEXTCANARY" not in dump and "enc1:" in dump
    enc = admin_conn.execute("SELECT content_enc FROM messages WHERE role = 'founder' LIMIT 1").fetchone()["content_enc"]
    assert "PLAINTEXTCANARY" in crypto.decrypt(enc, a["_uid"])
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(enc, str(uuid.uuid4()))      # bound to the founder: can't be moved to another row


def test_export_has_my_words_but_no_safety_labels(client, fake):
    h = signup(client)
    client.post("/chat", json={"message": "EXPORTME please"}, headers=hdr(h))
    out = client.get("/me/export", headers=hdr(h)).json()
    assert any("EXPORTME" in m["text"] for m in out["messages"])
    assert '"zone' not in json.dumps(out).lower() and "red_check_in" not in json.dumps(out)


def test_delete_my_data_removes_everything_but_keeps_audit(client, clean, admin_conn, fake, mailbox):
    h = signup(client)
    fake.script.zone = {"zone": "RED", "confidence": 0.95, "categories": ["self_harm"], "rationale": "x"}
    client.post("/chat", json={"message": "I want to die"}, headers=hdr(h))
    client.get(f"/staff/api/founders/{h['_uid']}", headers=make_staff(clean, "eir"))
    assert client.delete("/me", headers=hdr(h)).json() == {"deleted": True}
    for t in ("users", "messages", "zone_events", "alerts", "founder_state", "sessions"):
        assert admin_conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] == 0, t
    assert admin_conn.execute("SELECT count(*) AS n FROM audit_log").fetchone()["n"] >= 1
    assert client.get("/home", headers=hdr(h)).status_code == 401
    assert "closed by account deletion" in mailbox.sent[-1]["subject"] and "Sam" not in mailbox.sent[-1]["body"]


def test_retention_purges_old_data_but_not_open_red(client, clean, admin_conn, fake):
    from app.retention import purge
    h = signup(client)
    client.post("/chat", json={"message": "old routine message"}, headers=hdr(h))
    fake.script.zone = {"zone": "RED", "confidence": 0.95, "categories": ["self_harm"], "rationale": "x"}
    client.post("/chat", json={"message": "old red message"}, headers=hdr(h))
    with admin_conn.transaction():
        admin_conn.execute("SELECT set_config('app.purge', 'on', true)")
        admin_conn.execute("UPDATE zone_events SET created_at = now() - interval '400 days'")
        admin_conn.execute("UPDATE messages SET created_at = now() - interval '400 days'")
    fake.script.zone = {"zone": "GREEN", "confidence": 0.9, "categories": [], "rationale": "x"}
    client.post("/chat", json={"message": "new message"}, headers=hdr(h))
    out = purge(clean["admin"], 365)
    assert out["messages"] == 4 and out["zone_events"] == 1
    zones = [r["zone"] for r in admin_conn.execute("SELECT zone FROM zone_events").fetchall()]
    assert sorted(zones) == ["green", "red"]       # the RED tied to an open alert survives
