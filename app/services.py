"""Founder-side services: intake -> roadmap -> weekly KPI loop, with the safety pipeline on
every founder message. Everything runs inside `db.user_session`, so a founder can only
ever touch their own rows. Model calls happen between transactions, never inside one.

Nothing returned from this module contains a safety zone, category or confidence.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, timedelta, timezone

from . import adaptation, alerts, crypto, eir, guardrails
from .classifier import ZoneClassifier, compute_signals, snapshot
from .config import settings
from .context import load_founder_context
from .db import user_session
from .safety import Mode, SafetyResult, Zone

SESSION_IDLE = timedelta(minutes=30)
CRISIS_COOLDOWN = timedelta(hours=24)
WELLBEING_WEEKS = 2

CONSENT_VERSION = "safety-review-v1"
CONSENT_TEXT = (
    "AI EIR is a coach, not a therapist or a doctor. To keep founders safe, your messages and reflections are "
    "automatically checked for signs that you might be struggling or in danger, and a small team of trained EIR and "
    "safety staff can read them. If it looks like you might be in crisis, a person from that team may reach out to you. "
    "Your information is never shared with investors, other founders or marketing, and is never used for funding, "
    "ranking or selection decisions. Messages and reflections are encrypted, kept for 12 months, and you can export "
    "or delete your data at any time."
)

INTAKE_QUESTIONS = [
    ("what", "What's your startup called, and what are you building, in one sentence?"),
    ("who", "Who is it for? Who feels this problem most?"),
    ("goal", "What's your goal, and by when? For example: launch a clothing business in 100 days."),
    ("stage", "What exists today: an idea, a prototype, something live, or paying customers?"),
    ("obstacle", "What's the biggest thing standing between you and that goal?"),
    ("hours", "How many hours a week can you realistically give this?"),
    ("team", "Who's on your team, and where are the gaps?"),
    ("life", "What does a good week look like for you outside work: sleep, exercise, time off?"),
    ("misses", "When you've missed goals before, what usually got in the way?"),
    ("tone", "How do you like feedback: gentle, balanced, or very direct?"),
]
SKIP_PHRASES = {"i'd rather not say", "id rather not say", "i would rather not say", "rather not say",
                "prefer not to say", "i prefer not to say", "skip", "pass"}


class FlowBlocked(Exception):
    def __init__(self, reason: str, message: str, **extra):
        super().__init__(message)
        self.reason, self.message, self.extra = reason, message, extra


class NotFound(Exception):
    pass


def is_skip(text: str | None) -> bool:
    return text is None or re.sub(r"[^a-z' ]", "", text.strip().lower()).strip() in SKIP_PHRASES


# ------------------------------------------------------------------ helpers
def _session_id(conn, uid: str) -> uuid.UUID:
    row = conn.execute("SELECT id, last_activity_at FROM sessions WHERE user_id = %s "
                       "ORDER BY last_activity_at DESC LIMIT 1 FOR UPDATE", (uid,)).fetchone()
    now = datetime.now(timezone.utc)
    if row and now - row["last_activity_at"] < SESSION_IDLE:
        conn.execute("UPDATE sessions SET last_activity_at = now() WHERE id = %s", (row["id"],))
        return row["id"]
    sid = uuid.uuid4()
    conn.execute("INSERT INTO sessions (id, user_id) VALUES (%s, %s)", (sid, uid))
    return sid


def _history(conn, uid: str, sid: uuid.UUID) -> list[dict]:
    rows = conn.execute("SELECT role, content_enc FROM messages WHERE user_id = %s AND session_id = %s "
                        "ORDER BY created_at DESC LIMIT 12", (uid, sid)).fetchall()
    out = []
    for r in reversed(rows):
        if r["role"] in ("founder", "eir"):
            out.append({"role": "user" if r["role"] == "founder" else "assistant",
                        "content": crypto.decrypt(r["content_enc"], uid)})
    # the API expects alternating turns starting with the user
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def _insert_message(conn, uid: str, sid, role: str, text: str, channel: str, week: int | None,
                    prompt_version: str | None = None) -> uuid.UUID:
    mid = uuid.uuid4()
    conn.execute("INSERT INTO messages (id, user_id, session_id, role, content_enc, channel, prompt_version, week_number) "
                 "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                 (mid, uid, sid, role, crypto.encrypt(text, uid), channel, prompt_version, week))
    return mid


def _pause(conn, uid: str, reason: str) -> None:
    conn.execute("UPDATE founder_state SET paused = true, pause_reason = %s, paused_at = now(), updated_at = now() "
                 "WHERE user_id = %s AND (NOT paused OR pause_reason <> 'crisis')", (reason, uid))


def _record_event(conn, uid: str, sid, message_id, safety: SafetyResult, snap: dict, sampled: bool,
                  prompt_version: str | None) -> tuple[uuid.UUID, uuid.UUID | None]:
    """Append the zone event; open an alert for RED. Returns (event_id, alert_id)."""
    eid = uuid.uuid4()
    zone = safety.zone or Zone.YELLOW
    conn.execute(
        "INSERT INTO zone_events (event_id, user_id, session_id, message_id, zone, model_zone, confidence, categories, "
        "rationale_enc, signals, escalation_reasons, response_mode, kpi_completion_snapshot, classifier_version, "
        "prompt_version, sampled_for_review, explicit_danger) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (eid, uid, sid, message_id, zone.value, safety.model_zone.value if safety.model_zone else None,
         safety.confidence, safety.categories, crypto.encrypt(safety.rationale or "", uid),
         json.dumps(safety.signals or {}), safety.escalation_reasons, safety.mode.value, json.dumps(snap),
         safety.classifier_version, prompt_version, sampled, safety.explicit_danger))
    alert_id = None
    if zone == Zone.RED:
        alert_id = uuid.uuid4()
        conn.execute("INSERT INTO alerts (id, event_id, user_id) VALUES (%s, %s, %s)", (alert_id, eid, uid))
    return eid, alert_id


# ------------------------------------------------------------------ the safety pipeline
def founder_turn(user_id, text: str, *, channel: str = "chat", reply_when_green: bool = True,
                 classifier=None, llm=None) -> dict:
    """Every founder message goes through here: classify -> route -> reply -> log -> alert."""
    uid = str(user_id)
    with user_session(uid) as conn:
        ctx = load_founder_context(conn, uid)
        sid = _session_id(conn, uid)
        history = _history(conn, uid, sid)
        sig = compute_signals(ctx.kpis, [r["text"] or "" for r in ctx.reflections], ctx.current_week)
        snap = snapshot(ctx.kpis, sig)
        fm_id = _insert_message(conn, uid, sid, "founder", text, channel, ctx.current_week or None)

    clf = classifier or ZoneClassifier(llm=llm)
    safety = clf.classify(text, history, sig.as_dict())
    sampled = safety.zone == Zone.GREEN and getattr(clf, "sample_green", lambda: False)()

    reply = None
    if safety.zone != Zone.GREEN or reply_when_green:
        reply = eir.respond(ctx, text, channel=channel, history=history, llm=llm, precomputed=safety)

    with user_session(uid) as conn:
        em_id = None
        if reply:
            em_id = _insert_message(conn, uid, sid, "eir", reply.text, channel, ctx.current_week or None, reply.prompt_version)
        _, alert_id = _record_event(conn, uid, sid, fm_id, safety, snap, sampled,
                                    reply.prompt_version if reply else settings.eir_prompt_version)
        paused = False
        if safety.zone == Zone.RED:
            medical_only = "medical" in safety.categories and not {"self_harm", "harm_to_others", "abuse"} & set(safety.categories)
            _pause(conn, uid, "medical" if medical_only else "crisis")
            paused = True
        elif reply and reply.flow == "pause":
            _pause(conn, uid, "medical" if "medical" in safety.categories else "wellbeing")
            paused = True
    if alert_id:
        alerts.notify_async(alert_id)
    return {"reply": reply.text if reply else None, "message_id": str(em_id) if em_id else None, "kpis_paused": paused}


# ------------------------------------------------------------------ intake
def _intake_row(conn, uid: str, create: bool = False):
    row = conn.execute("SELECT id, status, answers_enc FROM intake_interviews WHERE user_id = %s "
                       "ORDER BY started_at DESC LIMIT 1 FOR UPDATE", (uid,)).fetchone()
    if not row and create:
        iid = uuid.uuid4()
        conn.execute("INSERT INTO intake_interviews (id, user_id, answers_enc) VALUES (%s, %s, %s)",
                     (iid, uid, crypto.encrypt("[]", uid)))
        return {"id": iid, "status": "in_progress", "answers": []}
    if not row:
        return None
    return {"id": row["id"], "status": row["status"], "answers": json.loads(crypto.decrypt(row["answers_enc"], uid) or "[]")}


def intake_status(user_id) -> dict:
    uid = str(user_id)
    with user_session(uid) as conn:
        row = _intake_row(conn, uid, create=True)
    n = len(row["answers"])
    done = row["status"] == "complete"
    return {"done": done, "answered": n, "total": len(INTAKE_QUESTIONS),
            "next_question": None if done else INTAKE_QUESTIONS[n][1],
            "note": "You can answer \"I'd rather not say\" to any question."}


def intake_answer(user_id, answer: str | None, *, classifier=None, llm=None) -> dict:
    uid = str(user_id)
    skipped = is_skip(answer)
    eir_reply = None
    if not skipped:
        eir_reply = founder_turn(uid, answer, channel="intake", reply_when_green=False, classifier=classifier, llm=llm)
    with user_session(uid) as conn:
        row = _intake_row(conn, uid, create=True)
        if row["status"] == "complete":
            raise FlowBlocked("intake_complete", "Your intake is already complete.")
        n = len(row["answers"])
        qid, q = INTAKE_QUESTIONS[n]
        row["answers"].append({"question_id": qid, "question": q, "answer": None if skipped else answer, "skipped": skipped})
        done = len(row["answers"]) == len(INTAKE_QUESTIONS)
        conn.execute("UPDATE intake_interviews SET answers_enc = %s, status = %s, completed_at = %s WHERE id = %s",
                     (crypto.encrypt(json.dumps(row["answers"]), uid), "complete" if done else "in_progress",
                      datetime.now(timezone.utc) if done else None, row["id"]))
        if done:
            a = {x["question_id"]: x["answer"] for x in row["answers"]}
            conn.execute("INSERT INTO startups (user_id, one_liner, goal, stage) VALUES (%s, %s, %s, %s)",
                         (uid, (a.get("what") or "")[:300], (a.get("goal") or "")[:300], (a.get("stage") or "")[:100]))
            tone = (a.get("tone") or "").lower()
            tone = "gentle" if "gentle" in tone else "direct" if "direct" in tone else "balanced"
            conn.execute("UPDATE founder_state SET tone = %s, updated_at = now() WHERE user_id = %s", (tone, uid))
    out = {"done": done, "answered": len(row["answers"]), "total": len(INTAKE_QUESTIONS),
           "next_question": None if done else INTAKE_QUESTIONS[len(row["answers"])][1]}
    if eir_reply and eir_reply["reply"]:
        out["eir_reply"] = eir_reply["reply"]
        out["message_id"] = eir_reply["message_id"]
    return out


# ------------------------------------------------------------------ roadmap
def create_roadmap(user_id, *, llm=None) -> dict:
    uid = str(user_id)
    with user_session(uid) as conn:
        row = _intake_row(conn, uid)
        if not row or row["status"] != "complete":
            raise FlowBlocked("intake_incomplete", "Finish the intake interview first.")
        ctx = load_founder_context(conn, uid)
    content = eir.generate_roadmap(ctx, llm=llm)
    start = date.today()
    with user_session(uid) as conn:
        rid = uuid.uuid4()
        conn.execute("INSERT INTO roadmaps (id, user_id, start_date, end_date, content, prompt_version) VALUES (%s,%s,%s,%s,%s,%s)",
                     (rid, uid, start, start + timedelta(days=29), json.dumps(content), settings.eir_prompt_version))
        conn.execute("UPDATE founder_state SET current_week = 0, updated_at = now() WHERE user_id = %s", (uid,))
    return {"roadmap_id": str(rid), "start_date": start.isoformat(), "end_date": (start + timedelta(days=29)).isoformat(), **content}


# ------------------------------------------------------------------ weekly KPIs
def _kpis(conn, uid: str, week: int | None = None) -> list[dict]:
    sql = "SELECT id, week_number, type, title, detail, status, due_date, replaced_by_guardrail FROM weekly_kpis WHERE user_id = %s"
    args: list = [uid]
    if week is not None:
        sql += " AND week_number = %s"
        args.append(week)
    return [dict(r) for r in conn.execute(sql + " ORDER BY week_number, type DESC, created_at", args).fetchall()]


def set_kpi_status(user_id, kpi_id, status: str) -> dict:
    if status not in ("open", "done", "missed"):
        raise ValueError("status must be open, done or missed")
    uid = str(user_id)
    with user_session(uid) as conn:
        row = conn.execute("UPDATE weekly_kpis SET status = %s, completed_at = CASE WHEN %s = 'done' THEN now() END "
                           "WHERE id = %s AND user_id = %s RETURNING id, status", (status, status, str(kpi_id), uid)).fetchone()
        if not row:
            raise NotFound()
    return {"id": str(row["id"]), "status": row["status"]}


def close_week(user_id) -> dict:
    uid = str(user_id)
    with user_session(uid) as conn:
        week = conn.execute("SELECT current_week FROM founder_state WHERE user_id = %s", (uid,)).fetchone()["current_week"]
        conn.execute("UPDATE weekly_kpis SET status = 'missed' WHERE user_id = %s AND week_number = %s AND status = 'open'",
                     (uid, week))
        missed = [k for k in _kpis(conn, uid, week) if k["status"] == "missed"]
        have = {r["kpi_id"] for r in conn.execute("SELECT kpi_id FROM reflections WHERE user_id = %s", (uid,)).fetchall()}
    need = [str(k["id"]) for k in missed if k["id"] not in have]
    return {"week": week, "missed_needing_reflection": need,
            "prompt": "What got in the way? You can also say \"I'd rather not say\"." if need else None}


def submit_reflection(user_id, kpi_ids: list[str], text: str | None, *, skip: bool = False, classifier=None, llm=None) -> dict:
    uid = str(user_id)
    skipped = skip or is_skip(text)
    with user_session(uid) as conn:
        rows = conn.execute("SELECT id, week_number, status FROM weekly_kpis WHERE user_id = %s AND id = ANY(%s::uuid[])",
                            (uid, [str(k) for k in kpi_ids])).fetchall()
        if len(rows) != len(set(map(str, kpi_ids))):
            raise NotFound()
        if any(r["status"] != "missed" for r in rows):
            raise FlowBlocked("not_missed", "Reflections are only for KPIs marked missed.")
        tags = adaptation.tag_reflection(text, skipped)
        for r in rows:
            conn.execute("INSERT INTO reflections (user_id, kpi_id, week_number, text_enc, skipped, tags) "
                         "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (kpi_id) DO UPDATE SET text_enc = EXCLUDED.text_enc, "
                         "skipped = EXCLUDED.skipped, tags = EXCLUDED.tags",
                         (uid, r["id"], r["week_number"], None if skipped else crypto.encrypt(text, uid), skipped, tags))
    if skipped:
        return {"reply": "Thanks for letting me know — no explanation needed. Your next week is ready when you are.",
                "message_id": None, "kpis_paused": False}
    return founder_turn(uid, text, channel="reflection", classifier=classifier, llm=llm)


def start_next_week(user_id, *, llm=None) -> dict:
    uid = str(user_id)
    with user_session(uid) as conn:
        state = dict(conn.execute("SELECT * FROM founder_state WHERE user_id = %s FOR UPDATE", (uid,)).fetchone())
        if state["paused"]:
            raise FlowBlocked("paused", "Your KPIs are on hold for now. When you're ready, let me know and we'll start "
                                        "with a lighter week.")
        if not conn.execute("SELECT 1 FROM roadmaps WHERE user_id = %s", (uid,)).fetchone():
            raise FlowBlocked("no_roadmap", "Create your 30-day roadmap first.")
        cur = state["current_week"]
        if cur >= 4:
            raise FlowBlocked("roadmap_complete", "Your 30-day roadmap is complete. Let's set the next one.")
        if cur > 0:
            this_week = _kpis(conn, uid, cur)
            open_ = [str(k["id"]) for k in this_week if k["status"] == "open"]
            if open_:
                raise FlowBlocked("week_open", "Mark this week's KPIs done, or close the week first.", kpi_ids=open_)
            have = {r["kpi_id"] for r in conn.execute("SELECT kpi_id FROM reflections WHERE user_id = %s", (uid,)).fetchall()}
            need = [str(k["id"]) for k in this_week if k["status"] == "missed" and k["id"] not in have]
            if need:
                raise FlowBlocked("reflection_required", "Before next week unlocks: what got in the way on the KPIs you "
                                  "missed? \"I'd rather not say\" is a fine answer.", kpi_ids=need)
        kpis = _kpis(conn, uid)
        refl = [dict(r) for r in conn.execute("SELECT tags FROM reflections WHERE user_id = %s ORDER BY created_at DESC LIMIT 10",
                                              (uid,)).fetchall()]
        fb = [dict(r) for r in conn.execute("SELECT rating, reasons FROM feedback WHERE user_id = %s ORDER BY created_at DESC LIMIT 10",
                                            (uid,)).fetchall()]
        learned = adaptation.recompute(state, kpis, refl, fb, starting_week=cur + 1)
        notes = (list(state["learned"]) + [n for n in learned.notes if n not in state["learned"][-8:]])[-12:]
        conn.execute("UPDATE founder_state SET professional_count = %s, professional_stretch = %s, personal_stretch = %s, "
                     "tone = %s, wellbeing_weeks_left = %s, learned = %s, history = %s, updated_at = now() WHERE user_id = %s",
                     (learned.professional_count, learned.professional_stretch, learned.personal_stretch, learned.tone,
                      learned.wellbeing_weeks_left, json.dumps(notes), json.dumps((list(state["history"]) + learned.changes)[-100:]), uid))
        ctx = load_founder_context(conn, uid)

    wellbeing = state["wellbeing_weeks_left"] > 0
    profile = {"professional_count": learned.professional_count, "professional_stretch": learned.professional_stretch,
               "personal_stretch": learned.personal_stretch, "wellbeing_weeks_left": state["wellbeing_weeks_left"]}
    draft = eir.generate_week_kpis(ctx, cur + 1, profile, llm=llm)
    prof, pers, guard_notes = guardrails.clean_kpis(draft, wellbeing=wellbeing)
    if not prof:
        raise RuntimeError("model returned no usable professional KPIs")
    due = date.today() + timedelta(days=6)
    with user_session(uid) as conn:
        moved = conn.execute("UPDATE founder_state SET current_week = %s WHERE user_id = %s AND current_week = %s RETURNING 1",
                             (cur + 1, uid, cur)).fetchone()
        if not moved:
            raise FlowBlocked("conflict", "This week was already started.")
        for t, items, stretch in (("professional", prof, learned.professional_stretch), ("personal", pers, learned.personal_stretch)):
            for k in items:
                conn.execute("INSERT INTO weekly_kpis (user_id, week_number, type, title, detail, due_date, stretch_level, "
                             "replaced_by_guardrail) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                             (uid, cur + 1, t, k["title"], k["detail"], due, stretch, k["replaced"]))
        week_kpis = _kpis(conn, uid, cur + 1)
    return {"week": cur + 1, "due_date": due.isoformat(), "kpis": [_public_kpi(k) for k in week_kpis],
            "why_this_week_looks_like_this": learned.notes}


def resume(user_id) -> dict:
    uid = str(user_id)
    with user_session(uid) as conn:
        st = conn.execute("SELECT paused, pause_reason, paused_at FROM founder_state WHERE user_id = %s FOR UPDATE", (uid,)).fetchone()
        if not st["paused"]:
            return {"kpis_paused": False, "message": "You're all set — nothing is on hold."}
        if st["pause_reason"] == "crisis" and st["paused_at"] and datetime.now(timezone.utc) - st["paused_at"] < CRISIS_COOLDOWN:
            raise FlowBlocked("not_yet", "Let's give it a day before we pick the KPIs back up. I'm here to talk any time "
                                         "in the meantime.")
        conn.execute("UPDATE founder_state SET paused = false, pause_reason = NULL, paused_at = NULL, "
                     "wellbeing_weeks_left = %s, updated_at = now() WHERE user_id = %s", (WELLBEING_WEEKS, uid))
    return {"kpis_paused": False, "message": "Welcome back. The next two weeks will be lighter, with rest built in."}


def _public_kpi(k: dict) -> dict:
    return {"id": str(k["id"]), "week": k["week_number"], "type": k["type"], "title": k["title"], "detail": k["detail"],
            "status": k["status"], "due_date": k["due_date"].isoformat() if k.get("due_date") else None}


def home(user_id) -> dict:
    uid = str(user_id)
    with user_session(uid) as conn:
        st = conn.execute("SELECT current_week, paused FROM founder_state WHERE user_id = %s", (uid,)).fetchone()
        rm = conn.execute("SELECT content, start_date, end_date FROM roadmaps WHERE user_id = %s ORDER BY created_at DESC LIMIT 1",
                          (uid,)).fetchone()
        kpis = _kpis(conn, uid, st["current_week"]) if st["current_week"] else []
        intake = _intake_row(conn, uid)
    return {"week": st["current_week"], "kpis_paused": st["paused"],
            "paused_message": "Your KPIs are on hold while you take care of yourself." if st["paused"] else None,
            "intake_done": bool(intake and intake["status"] == "complete"),
            "roadmap": ({**rm["content"], "start_date": rm["start_date"].isoformat(), "end_date": rm["end_date"].isoformat()}
                        if rm else None),
            "kpis": [_public_kpi(k) for k in kpis]}


# ------------------------------------------------------------------ feedback & adaptation
def give_feedback(user_id, message_id, rating: int, reasons: list[str], comment: str | None) -> dict:
    uid = str(user_id)
    if rating not in (-1, 1):
        raise ValueError("rating must be 1 or -1")
    reasons = [r for r in reasons if r in adaptation.FEEDBACK_REASONS]
    with user_session(uid) as conn:
        msg = conn.execute("SELECT id FROM messages WHERE id = %s AND user_id = %s AND role = 'eir'", (str(message_id), uid)).fetchone()
        if not msg:
            raise NotFound()
        conn.execute("INSERT INTO feedback (user_id, message_id, rating, reasons, comment_enc) VALUES (%s,%s,%s,%s,%s) "
                     "ON CONFLICT (message_id) DO UPDATE SET rating = EXCLUDED.rating, reasons = EXCLUDED.reasons, "
                     "comment_enc = EXCLUDED.comment_enc",
                     (uid, msg["id"], rating, reasons, crypto.encrypt(comment, uid) if comment else None))
        # Tone responds right away; load and difficulty adapt when the next week starts.
        state = dict(conn.execute("SELECT * FROM founder_state WHERE user_id = %s FOR UPDATE", (uid,)).fetchone())
        learned = adaptation.recompute(state, [], [], [{"rating": rating, "reasons": reasons}], starting_week=state["current_week"] + 1)
        if learned.tone != state["tone"]:
            note = next((n for n in learned.notes if "gentler" in n or "direct" in n), None)
            conn.execute("UPDATE founder_state SET tone = %s, learned = %s, history = %s, updated_at = now() WHERE user_id = %s",
                         (learned.tone, json.dumps((list(state["learned"]) + ([note] if note else []))[-12:]),
                          json.dumps((list(state["history"]) + [c for c in learned.changes if c["field"] == "tone"])[-100:]), uid))
    return {"saved": True}


def get_adaptation(user_id) -> dict:
    uid = str(user_id)
    with user_session(uid) as conn:
        st = dict(conn.execute("SELECT * FROM founder_state WHERE user_id = %s", (uid,)).fetchone())
    return adaptation.explain(st)


def set_adaptation(user_id, *, tone: str | None = None, professional_stretch: int | None = None,
                   professional_count: int | None = None) -> dict:
    """Founder overrides. Bounded by the same limits as learning; cannot lift wellbeing mode."""
    uid = str(user_id)
    with user_session(uid) as conn:
        st = dict(conn.execute("SELECT * FROM founder_state WHERE user_id = %s FOR UPDATE", (uid,)).fetchone())
        new = {"tone": tone or st["tone"],
               "professional_stretch": max(1, min(3, professional_stretch or st["professional_stretch"])),
               "professional_count": max(1, min(3, professional_count or st["professional_count"]))}
        if new["tone"] not in adaptation.TONES:
            raise ValueError("tone must be gentle, balanced or direct")
        if st["wellbeing_weeks_left"] or st["paused"]:
            new["professional_stretch"] = min(new["professional_stretch"], st["professional_stretch"])
            new["professional_count"] = min(new["professional_count"], st["professional_count"])
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        changes = [{"at": ts, "field": k, "from": st[k], "to": v, "by": "founder"} for k, v in new.items() if st[k] != v]
        conn.execute("UPDATE founder_state SET tone = %s, professional_stretch = %s, professional_count = %s, history = %s, "
                     "updated_at = now() WHERE user_id = %s",
                     (new["tone"], new["professional_stretch"], new["professional_count"],
                      json.dumps((list(st["history"]) + changes)[-100:]), uid))
    return get_adaptation(uid)


def reset_adaptation(user_id) -> dict:
    uid = str(user_id)
    with user_session(uid) as conn:
        st = dict(conn.execute("SELECT * FROM founder_state WHERE user_id = %s FOR UPDATE", (uid,)).fetchone())
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        d = adaptation.DEFAULTS
        conn.execute("UPDATE founder_state SET professional_count = %s, professional_stretch = %s, personal_stretch = %s, "
                     "tone = %s, learned = '[]'::jsonb, history = %s, updated_at = now() WHERE user_id = %s",
                     (d["professional_count"], d["professional_stretch"], d["personal_stretch"], d["tone"],
                      json.dumps(list(st["history"]) + [{"at": ts, "field": "all", "to": "defaults", "by": "founder"}]), uid))
    return get_adaptation(uid)


# ------------------------------------------------------------------ export & delete
def export_my_data(user_id) -> dict:
    """Everything the founder gave us, decrypted. Internal safety assessments are not part of
    the export: founders never see zone labels (Prompt 2), and staff notes are not founder data."""
    uid = str(user_id)
    with user_session(uid) as conn:
        user = conn.execute("SELECT email, display_name, country, consent_version, consented_at, created_at FROM users WHERE id = %s",
                            (uid,)).fetchone()
        intake = _intake_row(conn, uid)
        msgs = conn.execute("SELECT role, content_enc, channel, created_at FROM messages WHERE user_id = %s ORDER BY created_at",
                            (uid,)).fetchall()
        refl = conn.execute("SELECT r.week_number, r.text_enc, r.skipped, k.title FROM reflections r JOIN weekly_kpis k ON k.id = r.kpi_id "
                            "WHERE r.user_id = %s ORDER BY r.created_at", (uid,)).fetchall()
        roadmaps = conn.execute("SELECT content, start_date, end_date FROM roadmaps WHERE user_id = %s", (uid,)).fetchall()
        fb = conn.execute("SELECT rating, reasons, comment_enc, created_at FROM feedback WHERE user_id = %s", (uid,)).fetchall()
        kpis = _kpis(conn, uid)
        startups = conn.execute("SELECT name, one_liner, sector, stage, goal FROM startups WHERE user_id = %s", (uid,)).fetchall()
    iso = lambda v: v.isoformat() if hasattr(v, "isoformat") else v
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "account": {k: iso(v) for k, v in dict(user).items()},
        "startups": [dict(s) for s in startups],
        "intake": intake["answers"] if intake else [],
        "roadmaps": [{**r["content"], "start_date": iso(r["start_date"]), "end_date": iso(r["end_date"])} for r in roadmaps],
        "kpis": [_public_kpi(k) for k in kpis],
        "reflections": [{"week": r["week_number"], "kpi": r["title"], "skipped": r["skipped"],
                         "text": crypto.decrypt(r["text_enc"], uid)} for r in refl],
        "messages": [{"from": m["role"], "channel": m["channel"], "at": iso(m["created_at"]),
                      "text": crypto.decrypt(m["content_enc"], uid)} for m in msgs],
        "feedback": [{"rating": f["rating"], "reasons": f["reasons"], "comment": crypto.decrypt(f["comment_enc"], uid),
                      "at": iso(f["created_at"])} for f in fb],
        "what_the_eir_learned": get_adaptation(uid),
    }


def delete_my_data(user_id) -> dict:
    uid = str(user_id)
    with user_session(uid) as conn:
        closed = conn.execute("SELECT delete_my_data() AS ids").fetchone()["ids"] or []
    if closed:
        alerts.notify_deleted_with_open_alert(closed)
    return {"deleted": True}
