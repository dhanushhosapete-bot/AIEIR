"""Safety zones (from Prompt 2) and the fixed replies used in RED crisis mode.

Zones describe a message and the current state of a session, never a person. They are
internal: no founder-facing response or endpoint ever includes a zone label.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class Zone(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"

    @property
    def rank(self) -> int:
        return {"green": 0, "yellow": 1, "red": 2}[self.value]


def max_zone(*zones: "Zone | None") -> "Zone":
    present = [z for z in zones if z is not None]
    return max(present, key=lambda z: z.rank) if present else Zone.GREEN


class Mode(str, Enum):
    NORMAL = "normal"               # GREEN
    CAREFUL = "careful"             # YELLOW: extra empathy, stay in lane, refer out, may lighten load
    RED_CHECK_IN = "red_check_in"   # RED chosen under doubt: human alerted, gentle check-in, no KPIs
    RED_CRISIS = "red_crisis"       # RED with clear danger: fixed crisis reply, human alerted, no KPIs


@dataclass
class SafetyResult:
    zone: Zone | None
    source: str
    categories: list[str] = field(default_factory=list)
    rationale: str = ""
    confidence: float | None = None
    model_zone: Zone | None = None
    escalation_reasons: list[str] = field(default_factory=list)
    signals: dict = field(default_factory=dict)
    mode: Mode = Mode.NORMAL
    explicit_danger: bool = False
    classifier_version: str = "none"

    @property
    def label(self) -> str:
        return self.zone.value if self.zone else "unclassified"


class SafetyClassifier(Protocol):
    def classify(self, message: str, history: list[dict] | None = None, signals: dict | None = None) -> SafetyResult: ...


class FixedClassifier:
    """Returns a fixed result. For tests of the code paths around each zone."""

    def __init__(self, zone: Zone, mode: Mode | None = None, categories: list[str] | None = None):
        self.zone, self.categories = zone, categories or []
        self.mode = mode or {Zone.GREEN: Mode.NORMAL, Zone.YELLOW: Mode.CAREFUL, Zone.RED: Mode.RED_CRISIS}[zone]

    def classify(self, message: str, history: list[dict] | None = None, signals: dict | None = None) -> SafetyResult:
        return SafetyResult(zone=self.zone, source="fixed", categories=self.categories, mode=self.mode,
                            model_zone=self.zone, confidence=1.0, classifier_version="fixed", signals=signals or {})


# --------------------------------------------------------------- resources by country
# Keep short and verified. Anything else falls back to the local emergency number and
# an international helpline directory.
CRISIS_LINES: dict[str, tuple[str, str]] = {
    "US": ("call or text 988 to reach the 988 Suicide & Crisis Lifeline, any time", "911"),
    "CA": ("call or text 9-8-8, the Suicide Crisis Helpline", "911"),
    "GB": ("call Samaritans on 116 123, free and any time", "999"),
    "IN": ("call Tele-MANAS on 14416, free and any time", "112"),
    "AU": ("call Lifeline on 13 11 14", "000"),
}
ABUSE_LINES: dict[str, str] = {
    "US": "the National Domestic Violence Hotline at 1-800-799-7233 (or text START to 88788)",
}


def emergency_number(country: str | None) -> str | None:
    line = CRISIS_LINES.get((country or "US").upper())
    return line[1] if line else None


def crisis_line(country: str | None) -> str:
    line = CRISIS_LINES.get((country or "US").upper())
    if line:
        return f"You can {line[0]}."
    return "You can find a free, confidential helpline in your country at findahelpline.com."


def _emergency_sentence(country: str | None, situation: str) -> str:
    num = emergency_number(country)
    return (f"If {situation}, please call {num} now." if num
            else f"If {situation}, please call your local emergency number now.")


def crisis_message(country: str | None, name: str | None = None, categories: list[str] | None = None) -> str:
    """Fixed RED-crisis reply. Deterministic on purpose: this is the one reply that must not
    depend on how a model happens to respond. The conversation stays open afterwards."""
    cats = set(categories or [])
    hello = f"{name.split()[0]}, I" if name and name.strip() else "I"
    if "medical" in cats and not cats & {"self_harm", "harm_to_others", "abuse"}:
        return (
            f"{hello}'m really glad you told me. Chest pain or trouble breathing is something a doctor needs to look at — "
            "I can't assess it, and it's not worth guessing. "
            + _emergency_sentence(country, "it's severe, sudden, or comes with shortness of breath, fainting or pain spreading to your arm or jaw")
            + "\n\nOtherwise, please book an appointment as soon as you can. I've put your KPIs on hold so you have the time — "
              "they'll be here when you're ready, and we'll make the next ones lighter.\n\nI'm still here if you want to talk."
        )
    if "abuse" in cats and not cats & {"self_harm", "harm_to_others"}:
        line = ABUSE_LINES.get((country or "US").upper())
        support = (f"You can reach {line}, any time." if line
                   else "A local abuse or sexual assault helpline can help; findahelpline.com lists them by country.")
        return (
            f"{hello}'m really glad you told me, and I'm sorry this is happening. It isn't your fault. "
            "I'm setting the KPIs aside — your safety comes first.\n\n"
            f"{support} " + _emergency_sentence(country, "you're in danger right now")
            + "\n\nSomeone from our team who is trained to help may reach out to you. If there's someone you trust, "
              "it could also help to let them know.\n\nI'm still here if you want to keep talking."
        )
    if "harm_to_others" in cats and "self_harm" not in cats:
        return (
            f"{hello} can hear how much pressure you're under right now. I'm setting the KPIs aside — this matters more.\n\n"
            "If you feel you might hurt someone, please step away from the situation and talk to someone right now. "
            + _emergency_sentence(country, "anyone is in immediate danger") + " " + crisis_line(country)
            + "\n\nSomeone from our team may reach out to you. I'm still here if you want to keep talking."
        )
    return (
        f"{hello}'m really glad you told me. I'm setting the KPIs aside — they can wait, and you matter more than any of them.\n\n"
        "What you're describing sounds really heavy, and you deserve support from a person right now, not a checklist. "
        f"{crisis_line(country)} " + _emergency_sentence(country, "you might act on these thoughts or you're in danger right now")
        + "\n\nIt could also help to reach out to someone you trust today — a friend, family member or co-founder — and let "
          "them know how you're doing.\n\nI'm still here if you want to keep talking."
    )
