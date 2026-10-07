"""The EIR's model calls: replying to the founder, drafting the roadmap and drafting KPIs.
These functions are pure (no database), so the eval harness exercises exactly the same
code path the API uses."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .config import load_prompt, settings
from .context import FounderContext
from .llm import LLM, default_llm, parse_json_reply
from .safety import Mode, SafetyClassifier, SafetyResult, Zone, crisis_line, crisis_message, emergency_number

FLOW_RE = re.compile(r"<flow>\s*(proceed|pause)\s*</flow>\s*$", re.I)
WELLBEING = {"burnout", "medical", "self_harm"}

CHANNEL_NOTES = {
    "chat": "The founder sent a general message.",
    "reflection": ("The founder is explaining KPIs they missed, which is required before next week's KPIs unlock. "
                   "\"I'd rather not say\" is a complete, accepted answer."),
    "intake": "This is part of the intake interview.",
}


@dataclass
class EIRReply:
    text: str
    flow: str              # proceed | pause | crisis | none
    safety: SafetyResult
    prompt_version: str


def split_flow(text: str) -> tuple[str, str]:
    """The prompt asks the model to end every reply with <flow>proceed</flow> or
    <flow>pause</flow>. Returns (visible reply, decision); 'none' if the tag is missing."""
    m = FLOW_RE.search(text.strip())
    if not m:
        return text.strip(), "none"
    return text.strip()[: m.start()].strip(), m.group(1).lower()


def mode_instructions(safety: SafetyResult, ctx: FounderContext) -> str:
    """Routing from Prompt 2, expressed as instructions appended to the system prompt."""
    cats = ", ".join(safety.categories) or "unspecified"
    if safety.mode == Mode.CAREFUL:
        return (f"Care mode for this reply (signals: {cats}). Respond with extra empathy, stay in your lane, refer to a "
                "qualified professional where relevant, and consider lightening the KPI load. Do not issue new KPIs in "
                "this reply.")
    if safety.mode == Mode.RED_CHECK_IN:
        num = emergency_number(ctx.country) or "the local emergency number"
        medical = "medical" in safety.categories and "self_harm" not in safety.categories
        if medical:
            body = (f"Tell them a doctor needs to look at this, without diagnosing or suggesting treatments. If it's severe, "
                    f"sudden, or comes with shortness of breath, they should call {num} now. Say their KPIs are on hold so they "
                    "have time to see someone.")
        else:
            body = (f"Acknowledge what they shared with warmth and no judgment. Gently ask how they are doing beyond the business, "
                    f"referring to their own words. Mention softly that support is there any time if things feel heavier "
                    f"than frustration: {crisis_line(ctx.country)} Make clear the KPIs can wait and their wellbeing comes first. "
                    "Keep it short and human, not alarming and not scripted.")
        return ("Check-in mode for this reply. A person from the EIR team has been alerted. Do not coach, and do not list, "
                f"draft or adjust specific KPIs. {body} End with <flow>pause</flow>.")
    return ""


def build_system(ctx: FounderContext, prompt_version: str, safety: SafetyResult, channel: str) -> str:
    parts = [load_prompt(prompt_version), "---", ctx.render(), "---", f"Channel: {CHANNEL_NOTES.get(channel, channel)}"]
    extra = mode_instructions(safety, ctx)
    if extra:
        parts.append(extra)
    return "\n\n".join(parts)


def respond(ctx: FounderContext, founder_message: str, *, channel: str = "chat", history: list[dict] | None = None,
            llm: LLM | None = None, classifier: SafetyClassifier | None = None, signals: dict | None = None,
            prompt_version: str | None = None, precomputed: SafetyResult | None = None) -> EIRReply:
    """Classify, route, reply. RED never reaches KPIs; RED crisis never reaches the model."""
    pv = prompt_version or settings.eir_prompt_version
    if classifier is None and precomputed is None:
        from .classifier import ZoneClassifier
        classifier = ZoneClassifier(llm=llm)
    safety = precomputed or classifier.classify(founder_message, history, signals)

    if safety.mode == Mode.RED_CRISIS:
        return EIRReply(crisis_message(ctx.country, ctx.name, safety.categories), "crisis", safety, pv)

    llm = llm or default_llm()
    msgs = [m for m in (history or [])[-12:] if m.get("role") in ("user", "assistant")]
    msgs.append({"role": "user", "content": founder_message})
    result = llm.complete(system=build_system(ctx, pv, safety, channel), messages=msgs,
                          model=settings.eir_model, max_tokens=settings.max_tokens)
    text, flow = split_flow(result.text)
    # Code has the last word on flow: RED always stops KPIs; a wellbeing YELLOW always pauses them.
    if safety.zone == Zone.RED:
        flow = "crisis"
    elif safety.mode == Mode.CAREFUL and WELLBEING & set(safety.categories) and flow != "pause":
        flow = "pause"
    return EIRReply(text, flow, safety, pv)


ROADMAP_TOOL = {
    "name": "save_roadmap",
    "description": "Save the founder's 30-day roadmap.",
    "input_schema": {
        "type": "object",
        "properties": {
            "milestone": {"type": "string", "description": "What must be true on day 30, measurable."},
            "weeks": {"type": "array", "minItems": 4, "maxItems": 4, "items": {
                "type": "object",
                "properties": {"week": {"type": "integer"}, "focus": {"type": "string"},
                               "outcomes": {"type": "array", "items": {"type": "string"}}},
                "required": ["week", "focus", "outcomes"]}},
        },
        "required": ["milestone", "weeks"],
    },
}

KPI_TOOL = {
    "name": "save_kpis",
    "description": "Save this week's KPIs.",
    "input_schema": {
        "type": "object",
        "properties": {
            "professional": {"type": "array", "items": {"type": "object", "properties": {
                "title": {"type": "string"}, "detail": {"type": "string"}}, "required": ["title"]}},
            "personal": {"type": "array", "items": {"type": "object", "properties": {
                "title": {"type": "string"}, "detail": {"type": "string"}}, "required": ["title"]}},
        },
        "required": ["professional", "personal"],
    },
}


def generate_roadmap(ctx: FounderContext, *, llm: LLM | None = None, prompt_version: str | None = None) -> dict:
    pv = prompt_version or settings.eir_prompt_version
    llm = llm or default_llm()
    task = ("Task: draft this founder's 30-day roadmap toward their goal, as four weeks. Each week gets one focus "
            "and two to four measurable outcomes. Retire the biggest risk first. Call save_roadmap once.")
    out = parse_json_reply(llm.complete(system=build_system(ctx, pv, SafetyResult(None, "n/a"), "intake"),
                                        messages=[{"role": "user", "content": task}], model=settings.eir_model,
                                        max_tokens=settings.max_tokens, tool=ROADMAP_TOOL))
    weeks = [{"week": int(w.get("week", i + 1)), "focus": str(w.get("focus", ""))[:200],
              "outcomes": [str(o)[:200] for o in w.get("outcomes", [])][:4]} for i, w in enumerate(out.get("weeks", [])[:4])]
    if len(weeks) != 4:
        raise ValueError("roadmap must have four weeks")
    return {"milestone": str(out.get("milestone", ""))[:300], "weeks": weeks}


def generate_week_kpis(ctx: FounderContext, week: int, profile: dict, *, llm: LLM | None = None,
                       prompt_version: str | None = None) -> dict:
    """Draft KPIs for `week`. The counts and stretch levels come from the adaptation engine;
    personal KPIs are checked again by app.guardrails before they are saved."""
    pv = prompt_version or settings.eir_prompt_version
    llm = llm or default_llm()
    stretch_words = {1: "light and very achievable", 2: "steady, a realistic step forward", 3: "a stretch, but achievable"}
    focus = ""
    if ctx.roadmap:
        wk = next((w for w in ctx.roadmap.get("weeks", []) if int(w.get("week", 0)) == week), None)
        focus = f" This week's roadmap focus: {wk['focus']}." if wk else ""
    personal_hint = (" Make one personal KPI about rest or sleep (for example, 7+ hours on 5 nights)."
                     if profile.get("wellbeing_weeks_left") else "")
    task = (f"Task: draft week {week} KPIs.{focus} Exactly {profile['professional_count']} professional KPIs, "
            f"{stretch_words[profile['professional_stretch']]}, and exactly 2 personal KPIs that are healthy and moderate."
            f"{personal_hint} Each KPI is one checkable outcome a solo founder can finish this week. Call save_kpis once.")
    out = parse_json_reply(llm.complete(system=build_system(ctx, pv, SafetyResult(None, "n/a"), "chat"),
                                        messages=[{"role": "user", "content": task}], model=settings.eir_model,
                                        max_tokens=settings.max_tokens, tool=KPI_TOOL))
    return {"professional": list(out.get("professional", []))[: profile["professional_count"]],
            "personal": list(out.get("personal", []))[:2]}
