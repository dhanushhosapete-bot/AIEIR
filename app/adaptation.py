"""Per-founder adaptation: the EIR learns how to work with each founder from that founder's
own results and feedback, and nothing else.

Signals (all from the founder's own rows):
  * KPI completion, professional and personal, over recent closed weeks
  * Why KPIs were missed (tags on reflections: time, scope, energy, blocker, priorities)
  * Ratings on EIR replies, with reasons (too_much, too_little, too_harsh, too_soft, not_relevant, helpful)
  * Whether the flow was paused for wellbeing

What it may change: professional KPI count (1-3), professional stretch (1-3), personal
stretch (1-2), tone (gentle/balanced/direct), and plain-language notes the prompt sees.

What it can never change: the safety rules in the system prompt, the zone classifier, the
personal-KPI guardrails, or the 2-personal-KPI cap. While a founder is in wellbeing mode,
nothing may increase. Rules are deterministic so every change can be explained to the
founder, who can see what was learned and reset it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

DEFAULTS = {"professional_count": 3, "professional_stretch": 2, "personal_stretch": 1, "tone": "balanced"}
TONES = ["gentle", "balanced", "direct"]
FEEDBACK_REASONS = {"too_much", "too_little", "too_harsh", "too_soft", "not_relevant", "helpful"}

_TAGS = {
    "time": re.compile(r"\b(time|busy|client|deadline|schedule|ran out)\b", re.I),
    "scope": re.compile(r"\b(too (big|much|many|ambitious)|unrealistic|overcommit|scope)\b", re.I),
    "energy": re.compile(r"\b(tired|exhausted|sick|ill|sleep|burn(ed|t)? ?out|energy|low)\b", re.I),
    "blocker": re.compile(r"\b(waiting|blocked|depend|supplier|vendor|didn'?t (reply|respond)|out of my control)\b", re.I),
    "priorities": re.compile(r"\b(priorit|more important|urgent|fire|pivot|changed)\b", re.I),
}


def tag_reflection(text: str | None, skipped: bool) -> list[str]:
    if skipped or not text:
        return ["skipped"] if skipped else []
    return [t for t, rx in _TAGS.items() if rx.search(text)]


@dataclass
class Learned:
    professional_count: int
    professional_stretch: int
    personal_stretch: int
    tone: str
    wellbeing_weeks_left: int
    notes: list[str] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)


def _rate(ks: list[dict]) -> float | None:
    closed = [k for k in ks if k["status"] != "open"]
    return sum(k["status"] == "done" for k in closed) / len(closed) if closed else None


def recompute(state: dict, kpis: list[dict], reflections: list[dict], feedback: list[dict], *,
              starting_week: int) -> Learned:
    """state: founder_state row. kpis: all of this founder's KPIs. reflections: rows with tags.
    feedback: rows with rating and reasons (most recent first). Called before each new week."""
    cur = {k: state[k] for k in ("professional_count", "professional_stretch", "personal_stretch", "tone")}
    new = dict(cur)
    wellbeing_left = int(state.get("wellbeing_weeks_left") or 0)
    notes: list[str] = []

    weeks = sorted({k["week_number"] for k in kpis if k["week_number"] < starting_week}, reverse=True)
    prof_rates = [_rate([k for k in kpis if k["week_number"] == w and k["type"] == "professional"]) for w in weeks]
    pers_rates = [_rate([k for k in kpis if k["week_number"] == w and k["type"] == "personal"]) for w in weeks]
    last_prof = prof_rates[0] if prof_rates else None
    recent_tags = [t for r in reflections[:6] for t in (r.get("tags") or [])]
    recent_fb = [f for f in feedback[:5]]
    reasons = [r for f in recent_fb for r in (f.get("reasons") or [])]

    if wellbeing_left > 0 or state.get("paused"):
        # Wellbeing mode: lightest load, rest first, nothing may go up.
        new["professional_count"] = min(new["professional_count"], 1)
        new["professional_stretch"] = 1
        new["personal_stretch"] = 1
        notes.append("Keeping weeks light while you recover; rest or sleep is one of your personal KPIs.")
    else:
        two_strong = len(prof_rates) >= 2 and all(r is not None and r >= 0.9 for r in prof_rates[:2])
        if last_prof is not None and last_prof < 0.5:
            if "scope" in recent_tags or "too_much" in reasons or "energy" in recent_tags:
                new["professional_count"] = max(1, new["professional_count"] - 1)
                notes.append("Missed KPIs looked like too much at once, so there's one fewer professional KPI.")
            new["professional_stretch"] = max(1, new["professional_stretch"] - 1)
            notes.append("Last week's completion was under half, so this week's KPIs are smaller steps.")
        elif two_strong or ("too_little" in reasons and last_prof is not None and last_prof >= 0.7):
            if new["professional_count"] < 3:
                new["professional_count"] += 1
                notes.append("You've been finishing everything, so a professional KPI is back.")
            elif new["professional_stretch"] < 3:
                new["professional_stretch"] += 1
                notes.append("You've finished nearly everything two weeks running, so the KPIs stretch a little more.")
        if "blocker" in recent_tags:
            notes.append("Recent misses came from things outside your control; prefer KPIs you can finish without waiting on others.")
        if "time" in recent_tags:
            notes.append("Time was the usual constraint; size KPIs to the hours you actually have.")
        pers_last = pers_rates[0] if pers_rates else None
        if pers_last is not None and pers_last < 0.5:
            new["personal_stretch"] = 1
        # personal_stretch never exceeds 2, by design and by database constraint

    # Tone learns from ratings, one step at a time, in either mode.
    if "too_harsh" in reasons and new["tone"] != "gentle":
        new["tone"] = TONES[TONES.index(new["tone"]) - 1]
        notes.append("You said some replies felt harsh, so I'm being gentler.")
    elif "too_soft" in reasons and new["tone"] != "direct" and not wellbeing_left:
        new["tone"] = TONES[TONES.index(new["tone"]) + 1]
        notes.append("You asked for more directness, so I'm being more direct.")
    if "not_relevant" in reasons:
        notes.append("Some advice felt off-target; tie suggestions closely to the roadmap and the founder's own words.")

    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    changes = [{"at": ts, "week": starting_week, "field": k, "from": cur[k], "to": new[k]}
               for k in cur if cur[k] != new[k]]
    return Learned(new["professional_count"], new["professional_stretch"], new["personal_stretch"], new["tone"],
                   max(0, wellbeing_left - 1) if wellbeing_left else 0, notes, changes)


def explain(state: dict) -> dict:
    """What the founder sees: settings in plain words plus the notes. No safety labels."""
    stretch = {1: "light", 2: "steady", 3: "stretch"}
    return {
        "professional_kpis_per_week": state["professional_count"],
        "professional_difficulty": stretch[state["professional_stretch"]],
        "personal_difficulty": {1: "gentle", 2: "moderate"}[state["personal_stretch"]],
        "tone": state["tone"],
        "lighter_weeks_remaining": state["wellbeing_weeks_left"],
        "what_ive_learned": list(state.get("learned") or [])[-8:],
        "change_history": list(state.get("history") or [])[-20:],
    }
