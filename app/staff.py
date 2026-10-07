"""Internal dashboard data (Prompt 2), for trained EIR and safety staff only.

Access: three staff roles exist — eir, safety_reviewer, admin. Investors, cohort peers and
marketing have no accounts and no role, so there is nothing to grant them. Permissions are
checked here AND by row-level security on the aieir_staff database role.

Audit: every function that returns a founder's identity together with safety data, or any
founder text, writes an audit_log row in the same transaction. If the audit write fails,
the read fails.

Current zone: the highest zone in the founder's most recent session. Zones decay: a
founder with no activity for 14 days shows no current zone, and an open RED alert keeps
them pinned until staff acknowledge or resolve it. Zones describe moments, not people.

Trends are de-identified: no founder ids or text, weekly buckets, and category counts
under 5 are suppressed.
"""
from __future__ import annotations

import json
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import crypto
from .db import Staff, staff_session

ROLES = ("eir", "safety_reviewer", "admin")
PERMISSIONS = {
    "view_overview": {"eir", "safety_reviewer", "admin"},
    "view_founders": {"eir", "safety_reviewer", "admin"},
    "view_founder_detail": {"eir", "safety_reviewer", "admin"},
    "view_review_queue": {"safety_reviewer", "admin"},
    "submit_review": {"safety_reviewer", "admin"},
    "ack_alert": {"eir", "safety_reviewer", "admin"},
    "view_trends": {"eir", "safety_reviewer", "admin"},
    "view_audit_log": {"admin"},
}
ACTIVE_WINDOW = timedelta(hours=2)
DECAY_AFTER = timedelta(days=14)
MIN_CELL = 5
RANK = {"green": 0, "yellow": 1, "red": 2}


class Forbidden(Exception):
    pass


class NotFound(Exception):
    pass


def require(staff: Staff, perm: str) -> None:
    if staff.role not in PERMISSIONS[perm]:
        raise Forbidden(perm)


def _audit(conn, staff: Staff, action: str, user_id=None, **detail) -> None:
    conn.execute("INSERT INTO audit_log (staff_id, action, user_id, detail) VALUES (%s, %s, %s, %s)",
                 (str(staff.id), action, str(user_id) if user_id else None, json.dumps(detail, default=str)))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


# ------------------------------------------------------------------ 1. live overview
def overview(staff: Staff) -> dict:
    require(staff, "view_overview")
    with staff_session(staff) as conn:
        sessions = conn.execute(
            "SELECT s.id, s.user_id, max(CASE e.zone WHEN 'red' THEN 2 WHEN 'yellow' THEN 1 ELSE 0 END) AS z "
            "FROM sessions s LEFT JOIN zone_events e ON e.session_id = s.id "
            "WHERE s.last_activity_at >= %s GROUP BY s.id, s.user_id", (_now() - ACTIVE_WINDOW,)).fetchall()
        counts = Counter({0: "green", 1: "yellow", 2: "red"}[r["z"] or 0] for r in sessions)
        red = conn.execute(
            "SELECT a.id, a.user_id, a.created_at, a.status, a.acknowledged_at, a.escalation_level, a.first_notified_at, "
            "e.categories, e.response_mode, u.display_name "
            "FROM alerts a JOIN zone_events e ON e.event_id = a.event_id JOIN users u ON u.id = a.user_id "
            "WHERE a.status <> 'resolved' ORDER BY (a.status = 'open') DESC, a.created_at", ()).fetchall()
        _audit(conn, staff, "view_overview", red_alerts=len(red))
    return {
        "active_sessions": {z: counts.get(z, 0) for z in ("green", "yellow", "red")},
        "red_alerts": [{"alert_id": str(r["id"]), "founder_id": str(r["user_id"]), "founder": r["display_name"] or "Founder",
                        "created_at": _iso(r["created_at"]), "status": r["status"], "categories": r["categories"],
                        "mode": r["response_mode"], "minutes_open": int((_now() - r["created_at"]).total_seconds() // 60),
                        "escalated": r["escalation_level"] > 0, "emailed": r["first_notified_at"] is not None}
                       for r in red],
        "as_of": _iso(_now()),
    }


# ------------------------------------------------------------------ 2. founder list
def founder_list(staff: Staff) -> list[dict]:
    require(staff, "view_founders")
    now = _now()
    with staff_session(staff) as conn:
        users = conn.execute("SELECT id, display_name, email FROM users ORDER BY created_at").fetchall()
        last = {r["user_id"]: r for r in conn.execute(
            "SELECT DISTINCT ON (user_id) user_id, id, last_activity_at FROM sessions ORDER BY user_id, last_activity_at DESC").fetchall()}
        events = conn.execute("SELECT user_id, session_id, zone, created_at FROM zone_events WHERE created_at >= %s",
                              (now - timedelta(weeks=4),)).fetchall()
        open_red = {r["user_id"] for r in conn.execute("SELECT DISTINCT user_id FROM alerts WHERE status <> 'resolved'").fetchall()}
        kpis = conn.execute("SELECT user_id, type, status FROM weekly_kpis WHERE status <> 'open'").fetchall()
        _audit(conn, staff, "view_founder_list", founders=len(users))
    by_user = defaultdict(list)
    for e in events:
        by_user[e["user_id"]].append(e)
    rates = defaultdict(lambda: {"professional": [0, 0], "personal": [0, 0]})
    for k in kpis:
        r = rates[k["user_id"]][k["type"]]
        r[0] += k["status"] == "done"
        r[1] += 1
    out = []
    for u in users:
        uid, ses = u["id"], last.get(u["id"])
        evs = by_user.get(uid, [])
        current = None
        if ses and now - ses["last_activity_at"] < DECAY_AFTER:
            in_session = [e["zone"] for e in evs if e["session_id"] == ses["id"]]
            current = max(in_session, key=RANK.get) if in_session else "green"
        if uid in open_red:
            current = "red"
        trend = []
        for w in range(3, -1, -1):
            lo, hi = now - timedelta(weeks=w + 1), now - timedelta(weeks=w)
            zs = [e["zone"] for e in evs if lo <= e["created_at"] < hi]
            trend.append(max(zs, key=RANK.get) if zs else None)
        rr = rates[uid]
        out.append({"founder_id": str(uid), "name": u["display_name"] or u["email"].split("@")[0],
                    "current_zone": current, "open_alert": uid in open_red,
                    "last_activity": _iso(ses["last_activity_at"]) if ses else None, "zone_trend_4w": trend,
                    "professional_completion": round(rr["professional"][0] / rr["professional"][1], 2) if rr["professional"][1] else None,
                    "personal_completion": round(rr["personal"][0] / rr["personal"][1], 2) if rr["personal"][1] else None})
    out.sort(key=lambda f: (not f["open_alert"], -RANK.get(f["current_zone"] or "green", 0), f["name"].lower()))
    return out


# ------------------------------------------------------------------ 3. founder detail
def founder_detail(staff: Staff, founder_id: str) -> dict:
    require(staff, "view_founder_detail")
    fid = str(uuid.UUID(str(founder_id)))
    with staff_session(staff) as conn:
        u = conn.execute("SELECT id, display_name, email, country, consented_at FROM users WHERE id = %s", (fid,)).fetchone()
        if not u:
            raise NotFound()
        events = conn.execute("SELECT e.event_id, e.created_at, e.zone, e.model_zone, e.confidence, e.categories, e.rationale_enc, "
                              "e.escalation_reasons, e.response_mode, e.kpi_completion_snapshot, e.classifier_version, "
                              "e.prompt_version, m.content_enc AS message_enc "
                              "FROM zone_events e LEFT JOIN messages m ON m.id = e.message_id "
                              "WHERE e.user_id = %s ORDER BY e.created_at DESC LIMIT 100", (fid,)).fetchall()
        kpis = conn.execute("SELECT week_number, type, title, status, replaced_by_guardrail FROM weekly_kpis WHERE user_id = %s "
                            "ORDER BY week_number DESC, type DESC", (fid,)).fetchall()
        refl = conn.execute("SELECT r.week_number, r.text_enc, r.skipped, r.tags, r.created_at, k.title FROM reflections r "
                            "JOIN weekly_kpis k ON k.id = r.kpi_id WHERE r.user_id = %s ORDER BY r.created_at DESC", (fid,)).fetchall()
        alerts = conn.execute("SELECT id, created_at, status, acknowledged_at, escalation_level FROM alerts WHERE user_id = %s "
                              "ORDER BY created_at DESC", (fid,)).fetchall()
        state = conn.execute("SELECT paused, pause_reason, wellbeing_weeks_left, current_week FROM founder_state WHERE user_id = %s",
                             (fid,)).fetchone()
        _audit(conn, staff, "view_founder_detail", fid, events=len(events), reflections=len(refl))
    timeline, prev = [], None
    for e in reversed(events):
        timeline.append({"event_id": str(e["event_id"]), "at": _iso(e["created_at"]), "zone": e["zone"],
                         "changed": e["zone"] != prev, "model_zone": e["model_zone"], "confidence": e["confidence"],
                         "categories": e["categories"], "rationale": crypto.decrypt(e["rationale_enc"], fid),
                         "escalation_reasons": e["escalation_reasons"], "mode": e["response_mode"],
                         "message": crypto.decrypt(e["message_enc"], fid) if e["message_enc"] else None,
                         "kpi_snapshot": e["kpi_completion_snapshot"], "classifier_version": e["classifier_version"]})
        prev = e["zone"]
    return {"founder": {"id": fid, "name": u["display_name"], "email": u["email"], "country": u["country"],
                        "consented_at": _iso(u["consented_at"])},
            "state": dict(state) if state else None,
            "timeline": list(reversed(timeline)),
            "kpis": [dict(k) for k in kpis],
            "reflections": [{"week": r["week_number"], "kpi": r["title"], "skipped": r["skipped"], "tags": r["tags"],
                             "text": crypto.decrypt(r["text_enc"], fid), "at": _iso(r["created_at"])} for r in refl],
            "alerts": [{**{k: _iso(v) for k, v in dict(a).items()}, "id": str(a["id"])} for a in alerts]}


# ------------------------------------------------------------------ 4. review queue
def review_queue(staff: Staff) -> list[dict]:
    require(staff, "view_review_queue")
    with staff_session(staff) as conn:
        rows = conn.execute(
            "SELECT e.event_id, e.user_id, e.created_at, e.zone, e.confidence, e.categories, e.rationale_enc, "
            "e.escalation_reasons, e.response_mode, e.sampled_for_review, m.content_enc AS message_enc, u.display_name "
            "FROM zone_events e JOIN users u ON u.id = e.user_id LEFT JOIN messages m ON m.id = e.message_id "
            "WHERE (e.zone IN ('yellow', 'red') OR e.sampled_for_review) "
            "AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.event_id = e.event_id) "
            "ORDER BY (e.zone = 'red') DESC, e.created_at LIMIT 200").fetchall()
        _audit(conn, staff, "view_review_queue", items=[str(r["event_id"]) for r in rows])
    now = _now()
    return [{"event_id": str(r["event_id"]), "founder_id": str(r["user_id"]), "founder": r["display_name"] or "Founder",
             "at": _iso(r["created_at"]), "zone": r["zone"], "confidence": r["confidence"], "categories": r["categories"],
             "rationale": crypto.decrypt(r["rationale_enc"], str(r["user_id"])),
             "escalation_reasons": r["escalation_reasons"], "mode": r["response_mode"],
             "message": crypto.decrypt(r["message_enc"], str(r["user_id"])) if r["message_enc"] else None,
             "sampled_green": r["sampled_for_review"] and r["zone"] == "green",
             "review_due": _iso(r["created_at"] + (timedelta(minutes=15) if r["zone"] == "red" else timedelta(hours=48))),
             "overdue": now > r["created_at"] + (timedelta(minutes=15) if r["zone"] == "red" else timedelta(hours=48))}
            for r in rows]


def submit_review(staff: Staff, event_id: str, verdict: str, human_zone: str, note: str | None = None) -> dict:
    require(staff, "submit_review")
    if verdict not in ("agree", "disagree", "escalated", "resolved") or human_zone not in RANK:
        raise ValueError("verdict must be agree/disagree/escalated/resolved and human_zone green/yellow/red")
    with staff_session(staff) as conn:
        e = conn.execute("SELECT event_id, user_id, zone FROM zone_events WHERE event_id = %s", (str(event_id),)).fetchone()
        if not e:
            raise NotFound()
        uid = str(e["user_id"])
        conn.execute("INSERT INTO reviews (event_id, user_id, reviewer_id, verdict, human_zone, note_enc) VALUES (%s,%s,%s,%s,%s,%s)",
                     (e["event_id"], uid, str(staff.id), verdict, human_zone, crypto.encrypt(note, uid) if note else None))
        if verdict == "resolved":
            conn.execute("UPDATE alerts SET status = 'resolved', resolved_at = now() WHERE event_id = %s AND status <> 'resolved'",
                         (e["event_id"],))
        _audit(conn, staff, "submit_review", uid, event_id=str(event_id), verdict=verdict, human_zone=human_zone)
    return {"saved": True}


def ack_alert(staff: Staff, alert_id: str, resolve: bool = False) -> dict:
    require(staff, "ack_alert")
    with staff_session(staff) as conn:
        row = conn.execute(
            "UPDATE alerts SET status = CASE WHEN %s THEN 'resolved' ELSE 'acknowledged' END, "
            "acknowledged_by = coalesce(acknowledged_by, %s), acknowledged_at = coalesce(acknowledged_at, now()), "
            "resolved_at = CASE WHEN %s THEN now() ELSE resolved_at END WHERE id = %s RETURNING user_id, status",
            (resolve, str(staff.id), resolve, str(alert_id))).fetchone()
        if not row:
            raise NotFound()
        _audit(conn, staff, "resolve_alert" if resolve else "ack_alert", row["user_id"], alert_id=str(alert_id))
    return {"status": row["status"]}


# ------------------------------------------------------------------ 5. trends (de-identified)
def precision_recall(pairs: list[tuple[str, str]]) -> dict:
    """pairs: (human_zone, classifier_zone)."""
    out = {}
    for z in RANK:
        tp = sum(1 for h, c in pairs if h == z and c == z)
        fp = sum(1 for h, c in pairs if h != z and c == z)
        fn = sum(1 for h, c in pairs if h == z and c != z)
        out[z] = {"precision": round(tp / (tp + fp), 3) if tp + fp else None,
                  "recall": round(tp / (tp + fn), 3) if tp + fn else None, "reviewed": tp + fn}
    return out


def trends(staff: Staff, weeks: int = 8) -> dict:
    require(staff, "view_trends")
    since = _now() - timedelta(weeks=weeks)
    with staff_session(staff) as conn:
        dist = conn.execute("SELECT date_trunc('week', created_at) AS wk, zone, count(*) AS n FROM zone_events "
                            "WHERE created_at >= %s GROUP BY 1, 2 ORDER BY 1", (since,)).fetchall()
        cats = conn.execute("SELECT c AS category, count(*) AS n FROM zone_events, unnest(categories) AS c "
                            "WHERE created_at >= %s AND zone IN ('yellow', 'red') GROUP BY 1 ORDER BY 2 DESC", (since,)).fetchall()
        ack = conn.execute("SELECT avg(extract(epoch FROM acknowledged_at - created_at)) AS s, count(*) AS n, "
                           "count(*) FILTER (WHERE acknowledged_at - created_at > interval '15 minutes') AS late "
                           "FROM alerts WHERE created_at >= %s AND acknowledged_at IS NOT NULL", (since,)).fetchone()
        rev = conn.execute("SELECT r.verdict, r.human_zone, e.zone FROM reviews r JOIN zone_events e ON e.event_id = r.event_id "
                           "WHERE r.created_at >= %s", (since,)).fetchall()
        _audit(conn, staff, "view_trends")
    weekly = defaultdict(lambda: {"green": 0, "yellow": 0, "red": 0})
    for r in dist:
        weekly[r["wk"].date().isoformat()][r["zone"]] = r["n"]
    top = [{"category": c["category"], "count": c["n"]} for c in cats if c["n"] >= MIN_CELL]
    suppressed = sum(c["n"] for c in cats if c["n"] < MIN_CELL)
    pairs = [(r["human_zone"], r["zone"]) for r in rev]
    agree = sum(1 for h, c in pairs if h == c)
    return {"weekly_zone_counts": [{"week": k, **v} for k, v in sorted(weekly.items())],
            "top_categories": top, "categories_suppressed_under_5": suppressed,
            "red_time_to_ack_minutes": round(ack["s"] / 60, 1) if ack and ack["s"] is not None else None,
            "red_acknowledged": ack["n"] if ack else 0, "red_acknowledged_late": ack["late"] if ack else 0,
            "reviews": len(pairs), "agreement_rate": round(agree / len(pairs), 3) if pairs else None,
            "classifier_precision_recall": precision_recall(pairs),
            "note": "De-identified: no founder ids or text; weekly buckets; category counts under 5 suppressed."}


def audit_log(staff: Staff, limit: int = 200) -> list[dict]:
    require(staff, "view_audit_log")
    with staff_session(staff) as conn:
        rows = conn.execute("SELECT a.at, a.action, a.user_id, a.detail, s.email AS staff_email FROM audit_log a "
                            "LEFT JOIN staff s ON s.id = a.staff_id ORDER BY a.at DESC LIMIT %s", (limit,)).fetchall()
        _audit(conn, staff, "view_audit_log")
    return [{**{k: _iso(v) for k, v in dict(r).items()}, "user_id": str(r["user_id"]) if r["user_id"] else None} for r in rows]
