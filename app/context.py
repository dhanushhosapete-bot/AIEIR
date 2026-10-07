"""Builds the model's view of a founder. Privacy rule from the spec: the context for a
session contains ONLY the current founder's data.

How that is enforced in code, not just in the prompt:
  1. `load_founder_context` reads through a `user_session` connection, so Postgres RLS
     hides every other founder's rows. Each query also filters on user_id (belt and braces).
  2. Every loaded row keeps its user_id, and `FounderContext.assert_single_tenant()` fails
     loudly if any row belongs to someone else. `render()` calls it every time.
  3. `FounderContext` has no field that could hold another founder's records, and
     `render()` is the only way context reaches a prompt.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import psycopg

from . import crypto


class PrivacyViolation(RuntimeError):
    pass


@dataclass
class FounderContext:
    user_id: str
    name: str | None = None
    country: str = "US"
    startup: dict = field(default_factory=dict)
    roadmap: dict | None = None
    current_week: int = 0
    kpis: list[dict] = field(default_factory=list)          # recent weeks, newest first
    reflections: list[dict] = field(default_factory=list)
    intake: list[dict] = field(default_factory=list)
    adaptation: dict = field(default_factory=dict)
    paused: bool = False
    pause_reason: str | None = None
    _owned_rows: list[dict] = field(default_factory=list, repr=False)

    def assert_single_tenant(self) -> None:
        for row in self._owned_rows:
            if str(row.get("user_id")) != self.user_id:
                raise PrivacyViolation(f"row {row.get('id')} belongs to another founder")

    def render(self) -> str:
        """The founder file appended to the system prompt."""
        self.assert_single_tenant()
        lines = ["# Founder file",
                 "This file holds only this founder's own data. You have no access to any other founder's information.", ""]
        if self.name:
            lines.append(f"Name: {self.name}")
        lines.append(f"Country: {self.country}")
        if self.startup:
            s = self.startup
            lines.append("Startup: " + "; ".join(f"{k}: {v}" for k, v in s.items() if v not in (None, "")))
        if self.intake:
            lines.append("\n## Intake answers")
            for a in self.intake:
                lines.append(f"- {a.get('question')}: " + ("(skipped)" if a.get("skipped") else str(a.get("answer"))))
        if self.roadmap:
            lines.append("\n## 30-day roadmap")
            lines.append(f"Milestone: {self.roadmap.get('milestone', '')}")
            for w in self.roadmap.get("weeks", []):
                lines.append(f"- Week {w.get('week')}: {w.get('focus')}")
        lines.append(f"\n## Progress\nCurrent week: {self.current_week}")
        if self.paused:
            lines.append(f"KPI flow is PAUSED (reason: {self.pause_reason}). Do not issue new KPIs.")
        for k in self.kpis:
            lines.append(f"- Week {k['week_number']} [{k['type']}] {k['title']} — {k['status']}")
        if self.reflections:
            lines.append("\n## Founder's explanations for missed KPIs")
            for r in self.reflections:
                lines.append(f"- Week {r['week_number']}: " + ("(chose not to say)" if r.get("skipped") else str(r.get("text"))))
        if self.adaptation:
            a = self.adaptation
            lines.append("\n## What you've learned about working with this founder (from their own results and feedback)")
            lines.append(f"Preferred tone: {a.get('tone', 'balanced')}. Professional KPIs this week: {a.get('professional_count', 3)} "
                         f"at stretch level {a.get('professional_stretch', 2)}/3. Personal KPI stretch: {a.get('personal_stretch', 1)}/2.")
            if a.get("wellbeing_weeks_left"):
                lines.append("Wellbeing mode is ON: keep the load light and make rest or sleep a personal KPI.")
            for note in a.get("learned", [])[-6:]:
                lines.append(f"- {note}")
        return "\n".join(lines)


def _rows(conn: psycopg.Connection, sql: str, uid: str) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, (uid,)).fetchall()]


def load_founder_context(conn: psycopg.Connection, user_id: uuid.UUID | str) -> FounderContext:
    """`conn` must come from db.user_session(user_id)."""
    uid = str(user_id)
    scoped = conn.execute("SELECT app_current_user()::text AS u").fetchone()["u"]
    if scoped != uid:
        raise PrivacyViolation("connection is not scoped to this founder")

    user = conn.execute("SELECT id AS user_id, display_name, country FROM users WHERE id = %s", (uid,)).fetchone()
    if not user:
        raise PrivacyViolation("founder not visible in this session")
    startup = conn.execute("SELECT id, user_id, name, one_liner, sector, stage, goal, goal_due FROM startups "
                           "WHERE user_id = %s ORDER BY created_at DESC LIMIT 1", (uid,)).fetchone()
    intake = conn.execute("SELECT id, user_id, answers_enc FROM intake_interviews WHERE user_id = %s "
                          "ORDER BY started_at DESC LIMIT 1", (uid,)).fetchone()
    roadmap = conn.execute("SELECT id, user_id, content FROM roadmaps WHERE user_id = %s "
                           "ORDER BY created_at DESC LIMIT 1", (uid,)).fetchone()
    state = conn.execute("SELECT * FROM founder_state WHERE user_id = %s", (uid,)).fetchone()
    kpis = _rows(conn, "SELECT id, user_id, week_number, type, title, status FROM weekly_kpis WHERE user_id = %s "
                       "ORDER BY week_number DESC, type, created_at LIMIT 20", uid)
    refl = _rows(conn, "SELECT id, user_id, week_number, text_enc, skipped FROM reflections WHERE user_id = %s "
                       "ORDER BY created_at DESC LIMIT 10", uid)
    for r in refl:
        r["text"] = crypto.decrypt(r.pop("text_enc"), uid)

    owned = [dict(user)] + [dict(r) for r in (startup, intake, roadmap, state) if r] + kpis + refl
    ctx = FounderContext(
        user_id=uid, name=user["display_name"], country=user["country"] or "US",
        startup={k: (v.isoformat() if isinstance(v, date) else v) for k, v in dict(startup).items()
                 if k not in ("id", "user_id")} if startup else {},
        roadmap=roadmap["content"] if roadmap else None,
        intake=json.loads(crypto.decrypt(intake["answers_enc"], uid) or "[]") if intake else [],
        current_week=state["current_week"] if state else 0,
        kpis=kpis, reflections=refl,
        adaptation={k: state[k] for k in ("tone", "professional_count", "professional_stretch", "personal_stretch",
                                           "wellbeing_weeks_left", "learned")} if state else {},
        paused=bool(state and state["paused"]), pause_reason=state["pause_reason"] if state else None,
        _owned_rows=owned,
    )
    ctx.assert_single_tenant()
    return ctx


def context_from_fixture(data: dict[str, Any]) -> FounderContext:
    """Build a context from an eval case's `context:` block. Only founder-owned keys are
    read; anything else in the fixture (such as cohort records) is ignored here."""
    uid = data.get("user_id", "00000000-0000-0000-0000-000000000001")
    f = data.get("founder", {})
    kpis = [dict(k, user_id=uid) for k in data.get("kpis", [])]
    refl = [dict(r, user_id=uid) for r in data.get("reflections", [])]
    return FounderContext(user_id=uid, name=f.get("name"), country=f.get("country", "US"),
                          startup=data.get("startup", {}), roadmap=data.get("roadmap"),
                          current_week=data.get("current_week", 1), kpis=kpis, reflections=refl,
                          adaptation=data.get("adaptation", {}), _owned_rows=kpis + refl)
