"""Checks every model-drafted KPI before it is saved. Personal KPIs must be healthy and
moderate; no KPI may push "grind harder". A KPI that fails is replaced with a safe default
and flagged (replaced_by_guardrail), so the founder never sees the unsafe version.

This runs regardless of what the founder asked for or what adaptation has learned."""
from __future__ import annotations

import re

_UNSAFE_PERSONAL = [
    (re.compile(r"\b([0-5](\.\d)?|five|four|three)\s*(-\s*\d\s*)?(hours?|hrs?|h)\b[^.]{0,30}\bsleep|\bsleep\b[^.]{0,30}\b([0-5](\.\d)?|five|four|three)\s*(hours?|hrs?|h)\b", re.I), "sleep below 6 hours"),
    (re.compile(r"\b(skip(ping)?\s+(meals?|breakfast|lunch|dinner)|fast(ing)?\b|one\s+meal\s+a\s+day|omad\b|water\s+only|juice\s+cleanse|no\s+carbs|keto\b|under\s+\d{3,4}\s*(k?cal|calories))", re.I), "restrictive eating"),
    (re.compile(r"\b(lose|drop|cut)\s+\d+(\.\d+)?\s*(kg|kilos?|lbs?|pounds?)\b", re.I), "weight-loss target"),
    (re.compile(r"\b(marathon|ultra|ironman|twice\s+a\s+day|two\s+workouts\s+a\s+day|every\s+single\s+day|7\s+days\s+a\s+week|daily\s+(10|15|20|21)\s*k\b|(10|15|20|21)\s*k\s+(every\s+day|daily))", re.I), "extreme exercise"),
    (re.compile(r"\b(\d{2,3})\s*(k|km|kilomet(er|re)s?|miles?)\b", re.I), "distance"),
]
_GRIND = re.compile(r"\b(all[- ]nighters?|no\s+days?\s+off|grind\s+harder|hustle\s+harder|(8\d|9\d|1\d\d)\s*\+?\s*(hours?|hrs?)\s*(a|per)\s*week|"
                    r"work\s+(through|every)\s+(the\s+)?weekends?|no\s+sleep|sleep\s+less|cut\s+(back\s+)?(on\s+)?sleep)\b", re.I)

SAFE_PERSONAL = [
    ("Sleep 7+ hours on at least 5 nights", "Rest is what keeps the rest of the week possible."),
    ("Take two 30-minute walks", "Moving a little, outdoors if you can."),
    ("Take one full day off", "No work messages for one whole day."),
    ("Gym or exercise 2 days", "Moderate, whatever you enjoy."),
    ("Eat three regular meals on workdays", "Fuel for long days."),
]
WELLBEING_PERSONAL = [SAFE_PERSONAL[0], SAFE_PERSONAL[2]]


def personal_kpi_problem(title: str, detail: str = "") -> str | None:
    text = f"{title} {detail}"
    for rx, label in _UNSAFE_PERSONAL:
        m = rx.search(text)
        if not m:
            continue
        if label == "distance":
            km = float(m.group(1)) * (1.6 if m.group(2).lower().startswith("mile") else 1)
            if km <= 25:          # e.g. "jog 10K this week" is fine
                continue
            label = "distance over 25 km a week"
        return label
    if _GRIND.search(text):
        return "grind-harder goal"
    return None


def professional_kpi_problem(title: str, detail: str = "") -> str | None:
    return "grind-harder goal" if _GRIND.search(f"{title} {detail}") else None


def clean_kpis(draft: dict, *, wellbeing: bool) -> tuple[list[dict], list[dict], list[str]]:
    """Returns (professional, personal, notes). Unsafe items are replaced, never passed through."""
    notes: list[str] = []
    prof = []
    for k in draft.get("professional", []):
        title, detail = str(k.get("title", "")).strip()[:200], str(k.get("detail", "") or "")[:400]
        if not title:
            continue
        problem = professional_kpi_problem(title, detail)
        if problem:
            notes.append(f"Dropped a professional KPI ({problem}).")
            continue
        prof.append({"title": title, "detail": detail, "replaced": False})

    pers = []
    defaults = list(WELLBEING_PERSONAL if wellbeing else SAFE_PERSONAL)
    drafted = draft.get("personal", [])
    if wellbeing and not any(re.search(r"\b(sleep|rest|day off)\b", str(k.get("title", "")), re.I) for k in drafted):
        drafted = [{"title": WELLBEING_PERSONAL[0][0], "detail": WELLBEING_PERSONAL[0][1]}] + list(drafted)
    for k in drafted[:2]:
        title, detail = str(k.get("title", "")).strip()[:200], str(k.get("detail", "") or "")[:400]
        problem = personal_kpi_problem(title, detail) if title else "empty"
        if problem:
            t, d = next(x for x in defaults if x[0] not in [p["title"] for p in pers])
            notes.append(f"Replaced a personal KPI ({problem}) with a healthy default.")
            pers.append({"title": t, "detail": d, "replaced": True})
        else:
            pers.append({"title": title, "detail": detail, "replaced": False})
    while len(pers) < 2:
        t, d = next(x for x in defaults + SAFE_PERSONAL if x[0] not in [p["title"] for p in pers])
        pers.append({"title": t, "detail": d, "replaced": True})
    return prof, pers, notes
