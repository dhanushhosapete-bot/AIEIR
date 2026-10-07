"""RED alerting and escalation (Prompt 2).

* When a RED event is recorded, an alert row is created in the same transaction, the
  dashboard hears about it through Postgres NOTIFY, and an email goes to the primary
  on-call contact straight away (background thread, so the founder's reply isn't delayed).
* The escalation worker runs every 20 seconds. It re-sends any alert whose first email
  hasn't gone out, and emails the secondary contact for any alert still unacknowledged
  after 15 minutes.
* Emails never contain what the founder wrote, or their name: just the time, the
  categories and a dashboard link. The dashboard requires a staff login.

Configuration (environment):
  SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM
  ONCALL_PRIMARY_EMAIL, ONCALL_SECONDARY_EMAIL, STAFF_DASHBOARD_URL
If SMTP isn't configured, emails are written to ./outbox and /health reports alerting as
not configured, so this can't silently go unnoticed in production.
"""
from __future__ import annotations

import logging
import os
import smtplib
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from .db import SYSTEM_STAFF_ID, Staff, staff_session

log = logging.getLogger("aieir.alerts")
ESCALATE_AFTER = timedelta(minutes=15)
RETRY_FIRST_AFTER = timedelta(seconds=20)
WORKER_INTERVAL = 20
SYSTEM = Staff(SYSTEM_STAFF_ID, "admin")
RUN_INLINE = False   # tests set this to send synchronously


class SMTPSender:
    def __init__(self):
        self.host = os.environ["SMTP_HOST"]
        self.port = int(os.environ.get("SMTP_PORT", "587"))
        self.user, self.password = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
        self.sender = os.environ.get("SMTP_FROM", self.user or "alerts@localhost")

    def send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.sender, to, subject
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=20) as s:
            s.starttls()
            if self.user:
                s.login(self.user, self.password or "")
            s.send_message(msg)


class OutboxSender:
    """Development fallback: writes emails to ./outbox. Not delivery."""
    delivered = False

    def __init__(self, path: str = "outbox"):
        self.path = Path(path)

    def send(self, to: str, subject: str, body: str) -> None:
        self.path.mkdir(exist_ok=True)
        (self.path / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}.txt").write_text(
            f"To: {to}\nSubject: {subject}\n\n{body}")
        log.error("SMTP not configured: RED alert email written to outbox, NOT delivered (to %s)", to)


class MemorySender:
    """For tests."""
    delivered = True

    def __init__(self):
        self.sent: list[dict] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body, "at": datetime.now(timezone.utc)})


_sender = None


def sender():
    global _sender
    if _sender is None:
        _sender = SMTPSender() if os.environ.get("SMTP_HOST") else OutboxSender()
    return _sender


def set_sender(s) -> None:
    global _sender
    _sender = s


def configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("ONCALL_PRIMARY_EMAIL")
                and os.environ.get("ONCALL_SECONDARY_EMAIL"))


def _contact(level: int) -> str:
    key = "ONCALL_PRIMARY_EMAIL" if level == 0 else "ONCALL_SECONDARY_EMAIL"
    return os.environ.get(key, f"{key.lower()}@unset.invalid")


def _email(alert: dict, level: int) -> tuple[str, str]:
    url = os.environ.get("STAFF_DASHBOARD_URL", "http://localhost:8000/staff")
    cats = ", ".join(alert["categories"]) or "unspecified"
    when = alert["created_at"].strftime("%Y-%m-%d %H:%M UTC")
    if level == 0:
        subject = "[AI EIR] RED safety alert — please acknowledge"
        lead = "A founder conversation was flagged RED and needs a person now."
    else:
        subject = "[AI EIR] ESCALATED: RED safety alert unacknowledged for 15 minutes"
        lead = "This RED alert has not been acknowledged for 15 minutes. You are the second contact."
    body = (f"{lead}\n\nFlagged: {when}\nCategories: {cats}\nAlert: {alert['id']}\n\n"
            f"Open the dashboard to see who it is and acknowledge:\n{url}#alert={alert['id']}\n\n"
            "For privacy, this email does not include the founder's name or words.")
    return subject, body


def _send_and_record(conn, alert: dict, level: int) -> bool:
    to = _contact(level)
    subject, body = _email(alert, level)
    ok, err = True, None
    try:
        sender().send(to, subject, body)
    except Exception as e:  # recorded, retried by the worker
        ok, err = False, f"{type(e).__name__}: {e}"[:300]
        log.exception("alert email failed")
    delivered = ok and getattr(sender(), "delivered", True)
    note = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "channel": "email", "to_level": level,
            "ok": ok, "delivered": delivered, "error": err}
    conn.execute("UPDATE alerts SET notifications = notifications || %s::jsonb, "
                 "first_notified_at = CASE WHEN %s AND first_notified_at IS NULL AND %s = 0 THEN now() ELSE first_notified_at END "
                 "WHERE id = %s", (f"[{__import__('json').dumps(note)}]", ok, level, alert["id"]))
    return ok


def _load(conn, alert_id) -> dict | None:
    row = conn.execute("SELECT a.id, a.created_at, a.status, a.escalation_level, a.first_notified_at, e.categories "
                       "FROM alerts a JOIN zone_events e ON e.event_id = a.event_id WHERE a.id = %s FOR UPDATE OF a",
                       (str(alert_id),)).fetchone()
    return dict(row) if row else None


def notify_new(alert_id) -> None:
    with staff_session(SYSTEM) as conn:
        alert = _load(conn, alert_id)
        if alert and alert["status"] == "open" and alert["first_notified_at"] is None:
            _send_and_record(conn, alert, 0)


def notify_async(alert_id) -> None:
    if RUN_INLINE:
        notify_new(alert_id)
    else:
        threading.Thread(target=notify_new, args=(alert_id,), daemon=True, name="alert-notify").start()


def notify_deleted_with_open_alert(alert_ids: list) -> None:
    """A founder with an unresolved RED alert deleted their account. Their data is gone, as
    they asked; on-call still needs to know the alert closed without a person seeing it."""
    body = ("A founder deleted their account while a RED alert was still open, so the alert and their data were removed.\n\n"
            f"Alert id(s): {', '.join(map(str, alert_ids))}\n\nNo founder details are kept. If someone was already "
            "reaching out, follow your safety protocol for closed contact.")
    try:
        sender().send(_contact(0), "[AI EIR] Open RED alert closed by account deletion", body)
    except Exception:
        log.exception("deletion notice failed")


def run_escalations(now: datetime | None = None) -> dict:
    """One pass of the worker. Safe to run from several processes at once (SKIP LOCKED)."""
    now = now or datetime.now(timezone.utc)
    done = {"retried": 0, "escalated": 0}
    with staff_session(SYSTEM) as conn:
        retry = conn.execute("SELECT a.id, a.created_at, a.status, a.escalation_level, a.first_notified_at, e.categories "
                             "FROM alerts a JOIN zone_events e ON e.event_id = a.event_id "
                             "WHERE a.status = 'open' AND a.first_notified_at IS NULL AND a.created_at <= %s "
                             "FOR UPDATE OF a SKIP LOCKED", (now - RETRY_FIRST_AFTER,)).fetchall()
        for a in retry:
            done["retried"] += _send_and_record(conn, dict(a), 0)
        due = conn.execute("SELECT a.id, a.created_at, a.status, a.escalation_level, a.first_notified_at, e.categories "
                           "FROM alerts a JOIN zone_events e ON e.event_id = a.event_id "
                           "WHERE a.status = 'open' AND a.escalation_level = 0 AND a.created_at <= %s "
                           "FOR UPDATE OF a SKIP LOCKED", (now - ESCALATE_AFTER,)).fetchall()
        for a in due:
            _send_and_record(conn, dict(a), 1)
            conn.execute("UPDATE alerts SET escalation_level = 1, escalated_at = now() WHERE id = %s", (a["id"],))
            done["escalated"] += 1
    return done


def start_worker(stop: threading.Event | None = None) -> threading.Thread:
    stop = stop or threading.Event()

    def loop():
        while not stop.is_set():
            try:
                run_escalations()
            except Exception:
                log.exception("escalation pass failed")
            stop.wait(WORKER_INTERVAL)

    t = threading.Thread(target=loop, daemon=True, name="alert-escalation")
    t.start()
    return t


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if not configured():
        log.error("Alerting is NOT configured (SMTP_HOST, ONCALL_PRIMARY_EMAIL, ONCALL_SECONDARY_EMAIL).")
    while True:
        log.info("escalation pass: %s", run_escalations())
        time.sleep(WORKER_INTERVAL)
