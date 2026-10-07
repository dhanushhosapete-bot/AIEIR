"""RED alerts reach on-call immediately and escalate after 15 unacknowledged minutes."""
from datetime import datetime, timedelta, timezone

import psycopg

from app import alerts
from tests.conftest import hdr, make_staff, signup


def _red(client, fake, h):
    fake.script.zone = {"zone": "RED", "confidence": 0.95, "categories": ["self_harm"], "rationale": "x"}
    client.post("/chat", json={"message": "I don't want to be here anymore"}, headers=hdr(h))


def test_red_emails_primary_immediately(client, fake, mailbox, admin_conn):
    _red(client, fake, signup(client))
    a = admin_conn.execute("SELECT created_at, first_notified_at FROM alerts").fetchone()
    assert (a["first_notified_at"] - a["created_at"]).total_seconds() < 60
    assert len(mailbox.sent) == 1 and mailbox.sent[0]["to"] == "primary@oncall.test"


def test_unacknowledged_red_escalates_after_15_minutes(client, fake, mailbox):
    _red(client, fake, signup(client))
    assert alerts.run_escalations()["escalated"] == 0
    out = alerts.run_escalations(datetime.now(timezone.utc) + timedelta(minutes=16))
    assert out["escalated"] == 1 and mailbox.sent[-1]["to"] == "secondary@oncall.test"
    assert "ESCALATED" in mailbox.sent[-1]["subject"]
    assert alerts.run_escalations(datetime.now(timezone.utc) + timedelta(minutes=30))["escalated"] == 0   # once only


def test_acknowledged_red_does_not_escalate(client, clean, fake, mailbox):
    _red(client, fake, signup(client))
    eir = make_staff(clean, "eir")
    alert_id = client.get("/staff/api/overview", headers=eir).json()["red_alerts"][0]["alert_id"]
    assert client.post(f"/staff/api/alerts/{alert_id}/ack", headers=eir).json()["status"] == "acknowledged"
    assert alerts.run_escalations(datetime.now(timezone.utc) + timedelta(minutes=20))["escalated"] == 0


def test_failed_first_email_is_retried(client, fake, mailbox, admin_conn):
    class Flaky(alerts.MemorySender):
        fail = True

        def send(self, to, subject, body):
            if self.fail:
                raise ConnectionError("smtp down")
            super().send(to, subject, body)
    flaky = Flaky()
    alerts.set_sender(flaky)
    _red(client, fake, signup(client))
    assert admin_conn.execute("SELECT first_notified_at FROM alerts").fetchone()["first_notified_at"] is None
    flaky.fail = False
    assert alerts.run_escalations(datetime.now(timezone.utc) + timedelta(seconds=30))["retried"] == 1
    assert flaky.sent and admin_conn.execute("SELECT first_notified_at FROM alerts").fetchone()["first_notified_at"]


def test_live_stream_notifies_on_new_zone_event(client, clean, fake):
    """The dashboard stream is Postgres LISTEN/NOTIFY; check a RED insert is announced, ids only."""
    with psycopg.connect(clean["staff"], autocommit=True) as listener:
        listener.execute("LISTEN aieir_live")
        _red(client, fake, signup(client))
        got = [n.payload for n in listener.notifies(timeout=2, stop_after=2)]
    assert any('"zone_event"' in p and '"red"' in p for p in got)
    assert any('"alert"' in p for p in got)
    assert not any("want to be here" in p for p in got)
