"""Zone classifier (Prompt 2). Runs on every founder message before the EIR replies.

Decision = model judgement + code, combined so that code can only ever RAISE the zone:
  1. Model: strict JSON {zone, confidence, categories, rationale} (prompts/zone_classifier_v1.md).
  2. Safety-net phrases: clear danger language forces RED even if the model said otherwise;
     hopelessness-adjacent phrases force RED under the "when in doubt, choose RED" rule.
  3. Signals: 2+ consecutive weeks of mostly missed KPIs, a sharp drop in personal KPI
     completion, or a run of negative reflections raise GREEN to YELLOW.
  4. Doubt rule: a YELLOW that touches self_harm, harm_to_others or abuse becomes RED.
  5. If the model call fails, the result is never GREEN: YELLOW (human review) unless a
     safety-net phrase makes it RED.

Response mode for RED: explicit danger (a clear safety-net phrase, or the model's
explicit_danger flag) gets the fixed crisis reply; RED chosen under doubt gets a gentle
check-in. Both alert a
human and both stop KPIs.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass

from .config import load_prompt, settings
from .llm import LLM, default_llm, parse_json_reply
from .safety import Mode, SafetyResult, Zone, max_zone

CLASSIFIER_VERSION = "zone_classifier_v3"
CATEGORIES = ["burnout", "self_harm", "harm_to_others", "abuse", "medical", "ethics", "privacy", "legal", "financial", "other"]
RED_ADJACENT = {"self_harm", "harm_to_others", "abuse"}

ZONE_TOOL = {
    "name": "record_zone",
    "description": "Record the zone for the founder's latest message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "zone": {"type": "string", "enum": ["GREEN", "YELLOW", "RED"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "categories": {"type": "array", "items": {"type": "string", "enum": CATEGORIES}},
            "explicit_danger": {"type": "boolean"},
            "rationale": {"type": "string"},
        },
        "required": ["zone", "confidence", "categories", "explicit_danger", "rationale"],
    },
}

# Clear danger: always RED, crisis reply. Kept narrow to avoid matching startup hyperbole.
_CLEAR = [
    (re.compile(r"\b(kill(ing)?|hurt(ing)?|harm(ing)?)\s+my\s?self\b|\bsuicid|\bend(ing)?\s+(it all|my life)\b|"
                r"\bwant(ed)?\s+to\s+die\b|\bno\s+reason\s+to\s+(live|go\s+on)\b|\bself[- ]harm|\bcutting\s+myself\b", re.I), "self_harm"),
    (re.compile(r"\bgiv(e|ing|en)\s+away\s+(all\s+)?my\s+(stuff|things|belongings|possessions)\b|"
                r"\bwon'?t\s+need\s+(my\s+(stuff|things|belongings|possessions)|(it|them)\s+where\s+i'?m\s+going)\b|"
                r"\b(everyone|they|people|my\s+family)\s+(would|will|'d)\s+be\s+better\s+off\s+(without\s+me|if\s+i\s+(just\s+)?(disappeared|was\s+gone|were\s+gone|wasn'?t\s+here|weren'?t\s+here|died))\b|"
                r"\bsaying\s+(my\s+)?goodbyes?\b", re.I), "self_harm"),
    # "kill"/"hurt" + person is usually startup hyperbole ("this pitch will kill them"), so
    # only unambiguous violence is here; the model and the doubt rule handle the rest.
    (re.compile(r"\b(going\s+to|gonna|will|want\s+to)\s+(stab|shoot|attack|physically\s+hurt|beat\s+up)\s+(him|her|them|my\s+co-?founder|my\s+partner|my\s+investor|someone|people)\b|"
                r"\bi\s+know\s+where\s+(he|she|they)\s+lives?\b", re.I), "harm_to_others"),
    (re.compile(r"\b(crushing|severe|sudden)\s+chest\s+pain|\bchest\s+pain\b.{0,60}\b(can'?t\s+breathe|short(ness)?\s+of\s+breath|arm\s+is\s+numb|left\s+arm|jaw|fainting|passed\s+out)\b", re.I | re.S), "medical"),
    (re.compile(r"\b(he|she|they|my\s+(\w+\s+){0,2}(partner|husband|wife|boyfriend|girlfriend|investor|boss|co-?founder|mentor|manager))\s+"
                r"(hits|hit|beats|beat|chokes|choked|touches|touched|groped|assaulted|raped|molested)\s+me\b|"
                r"\b(groped|raped|molested|sexually\s+assaulted)\s+me\b|\bsexual(ly)?\s+(harass|assault)", re.I), "abuse"),
]
# Hopelessness-adjacent: ambiguous on its own, so RED under the doubt rule, gentle check-in.
_DOUBT = re.compile(
    r"\bdon'?t\s+know\s+why\s+i'?m\s+(even\s+)?doing\s+(this|any\s+of\s+this)\s*(any\s?more)\b|"
    r"\bwhat'?s\s+the\s+point\s+(of\s+(anything|any\s+of\s+(it|this)|trying|going\s+on)|any\s?more)\b|"
    r"\bcan'?t\s+(do|keep\s+doing)\s+this\s+any\s?more\b|\bi\s+feel\s+(so\s+)?(hopeless|worthless|empty)\b|"
    r"\bdisappear(ed)?\s+(forever|for\s+good)\b|\bnothing\s+matters\s+any\s?more\b", re.I)

_NEG_WORDS = re.compile(r"\b(exhausted|tired|drained|overwhelmed|burn(ed|t)?\s?out|anxious|stressed|hopeless|lonely|"
                        r"can'?t\s+(sleep|focus)|not\s+sleeping|sad|stuck|failing|failure|worthless|numb|empty)\b", re.I)


@dataclass
class Signals:
    consecutive_missed_weeks: int = 0
    personal_completion_last: float | None = None
    personal_completion_prior: float | None = None
    negative_reflections_recent: int = 0

    @property
    def personal_drop(self) -> bool:
        return (self.personal_completion_last is not None and self.personal_completion_prior is not None
                and self.personal_completion_prior - self.personal_completion_last >= 0.5)

    def as_dict(self) -> dict:
        return {"consecutive_missed_weeks": self.consecutive_missed_weeks,
                "personal_completion_last": self.personal_completion_last,
                "personal_completion_prior": self.personal_completion_prior,
                "personal_drop": self.personal_drop,
                "negative_reflections_recent": self.negative_reflections_recent}


def compute_signals(kpis: list[dict], reflection_texts: list[str], current_week: int) -> Signals:
    """kpis: rows with week_number, type, status. A week counts as missed when fewer than half
    of its KPIs were done. Only closed weeks (no open KPIs, or before the current week) count."""
    weeks: dict[int, list[dict]] = {}
    for k in kpis:
        weeks.setdefault(int(k["week_number"]), []).append(k)
    closed = sorted((w for w, ks in weeks.items()
                     if w < current_week or all(k["status"] != "open" for k in ks)), reverse=True)

    def rate(ks: list[dict]) -> float | None:
        return sum(k["status"] == "done" for k in ks) / len(ks) if ks else None

    missed = 0
    for w in closed:
        r = rate(weeks[w])
        if r is not None and r < 0.5:
            missed += 1
        else:
            break
    personal = [rate([k for k in weeks[w] if k["type"] == "personal"]) for w in closed]
    personal = [p for p in personal if p is not None]
    last = personal[0] if personal else None
    prior = sum(personal[1:4]) / len(personal[1:4]) if len(personal) > 1 else None
    neg = sum(1 for t in reflection_texts[:3] if t and len(_NEG_WORDS.findall(t)) >= 1)
    return Signals(missed, last, prior, neg)


def snapshot(kpis: list[dict], signals: Signals) -> dict:
    """KPI completion snapshot stored with every zone event."""
    def r(t: str) -> float | None:
        ks = [k for k in kpis if k["type"] == t and k["status"] != "open"]
        return round(sum(k["status"] == "done" for k in ks) / len(ks), 2) if ks else None
    return {"professional_rate": r("professional"), "personal_rate": r("personal"),
            "consecutive_missed_weeks": signals.consecutive_missed_weeks}


def safety_net(message: str) -> tuple[str | None, str | None]:
    """Returns (kind, category): kind is 'clear' or 'doubt'."""
    for rx, cat in _CLEAR:
        if rx.search(message):
            return "clear", cat
    if _DOUBT.search(message):
        return "doubt", "self_harm"
    return None, None


class ZoneClassifier:
    def __init__(self, llm: LLM | None = None, model: str | None = None, sample_rate: float | None = None):
        self.llm = llm
        self.model = model or settings.classifier_model
        self.sample_rate = sample_rate if sample_rate is not None else float(__import__("os").environ.get("REVIEW_SAMPLE_RATE", "0.02"))

    def _ask_model(self, message: str, history: list[dict] | None, signals: dict) -> dict:
        ctx = "\n".join(f"{'FOUNDER' if m['role'] == 'user' else 'COACH'}: {m['content'][:600]}" for m in (history or [])[-6:])
        user = (f"RECENT CONVERSATION:\n{ctx or '(none)'}\n\nSIGNALS FROM THIS FOUNDER'S KPIs:\n{signals}\n\n"
                f"LATEST FOUNDER MESSAGE (classify this):\n{message}")
        res = (self.llm or default_llm()).complete(system=load_prompt(CLASSIFIER_VERSION), model=self.model,
                                                   messages=[{"role": "user", "content": user}], max_tokens=2000, tool=ZONE_TOOL)
        out = parse_json_reply(res)
        zone = Zone(str(out["zone"]).lower())
        conf = max(0.0, min(1.0, float(out.get("confidence", 0.5))))
        cats = [c for c in out.get("categories", []) if c in CATEGORIES]
        return {"zone": zone, "confidence": conf, "categories": cats, "explicit": out.get("explicit_danger") is True,
                "rationale": str(out.get("rationale", ""))[:300]}

    def classify(self, message: str, history: list[dict] | None = None, signals: dict | None = None) -> SafetyResult:
        signals = signals or {}
        reasons: list[str] = []
        try:
            m = self._ask_model(message, history, signals)
            model_zone, conf, cats, rationale, source = m["zone"], m["confidence"], m["categories"], m["rationale"], "model"
            explicit = m["explicit"]
        except Exception as e:  # model unavailable or malformed: never fall back to GREEN
            model_zone, conf, cats, rationale, source = None, None, [], f"classifier error: {type(e).__name__}", "fallback"
            explicit = False
            reasons.append("classifier_unavailable")

        zone = model_zone or Zone.YELLOW
        kind, net_cat = safety_net(message)
        if kind:
            if zone != Zone.RED:
                reasons.append(f"safety_net_{kind}:{net_cat}")
            zone = Zone.RED
            if net_cat and net_cat not in cats:
                cats.append(net_cat)

        if zone == Zone.GREEN:
            if signals.get("consecutive_missed_weeks", 0) >= 2:
                zone = Zone.YELLOW; reasons.append("missed_kpis_2plus_weeks")
            elif signals.get("personal_drop"):
                zone = Zone.YELLOW; reasons.append("personal_kpi_drop")
            elif signals.get("negative_reflections_recent", 0) >= 2:
                zone = Zone.YELLOW; reasons.append("negative_reflection_trend")
            if zone == Zone.YELLOW and "burnout" not in cats:
                cats.append("burnout")

        if zone == Zone.YELLOW and RED_ADJACENT & set(cats):
            zone = Zone.RED; reasons.append("doubt_to_red")

        mode = {Zone.GREEN: Mode.NORMAL, Zone.YELLOW: Mode.CAREFUL}.get(zone)
        if zone == Zone.RED:
            # The fixed crisis reply is for explicit danger. Confidence measures how sure the model is
            # of the zone, not how severe things are, so it doesn't decide this.
            clear = kind == "clear" or (model_zone == Zone.RED and explicit)
            mode = Mode.RED_CRISIS if clear else Mode.RED_CHECK_IN
        return SafetyResult(zone=zone, source=source, categories=cats, rationale=rationale, confidence=conf,
                            model_zone=model_zone, escalation_reasons=reasons, signals=signals, mode=mode,
                            explicit_danger=explicit or kind == "clear", classifier_version=CLASSIFIER_VERSION)

    def sample_green(self) -> bool:
        return random.random() < self.sample_rate


def classify_offline(message: str, signals: dict | None = None) -> SafetyResult:
    """Code-only path (safety net + signals), used when no model is configured at all."""
    return ZoneClassifier(llm=_Unavailable()).classify(message, None, signals)


class _Unavailable:
    def complete(self, **_):
        raise RuntimeError("no model configured")


__all__ = ["ZoneClassifier", "compute_signals", "snapshot", "safety_net", "CLASSIFIER_VERSION", "CATEGORIES",
           "max_zone"]
