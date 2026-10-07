"""How model judgement and code combine into a zone. Code may only raise a zone."""
from app.classifier import ZoneClassifier, compute_signals, safety_net
from app.llm import FakeLLM
from app.safety import Mode, Zone, crisis_message


def clf(zone="GREEN", conf=0.9, cats=(), fail=False, explicit=False):
    def respond(**kw):
        if fail:
            raise RuntimeError("down")
        return {"zone": zone, "confidence": conf, "categories": list(cats), "explicit_danger": explicit, "rationale": "r"}
    return ZoneClassifier(llm=FakeLLM(responder=respond), sample_rate=0)


def test_green_stays_green():
    r = clf().classify("Shipped the landing page, 40 signups.")
    assert r.zone == Zone.GREEN and r.mode == Mode.NORMAL and not r.escalation_reasons


def test_sarcasm_is_not_forced_to_red_by_code():
    r = clf("GREEN").classify("this startup is killing me lol")
    assert r.zone == Zone.GREEN


def test_signals_raise_green_to_yellow():
    assert clf().classify("missed again", signals={"consecutive_missed_weeks": 2}).zone == Zone.YELLOW
    assert clf().classify("fine", signals={"personal_drop": True}).escalation_reasons == ["personal_kpi_drop"]
    assert clf().classify("fine", signals={"negative_reflections_recent": 2}).zone == Zone.YELLOW


def test_doubt_rule_turns_red_adjacent_yellow_into_red_check_in():
    r = clf("YELLOW", 0.6, ["burnout", "self_harm"]).classify("I just feel empty lately")
    assert r.zone == Zone.RED and r.mode == Mode.RED_CHECK_IN and "doubt_to_red" in r.escalation_reasons


def test_explicit_danger_is_crisis_and_ambiguous_red_is_check_in():
    assert clf("RED", 0.6, ["self_harm"], explicit=True).classify("x").mode == Mode.RED_CRISIS
    # high confidence that the zone is RED is not the same as explicit danger (Case 1)
    r = clf("RED", 0.9, ["self_harm"]).classify("I don't know why I'm even doing this anymore")
    assert r.zone == Zone.RED and r.mode == Mode.RED_CHECK_IN and not r.explicit_danger


def test_clear_safety_net_phrase_is_crisis_even_without_the_model_flag():
    r = clf("RED", 0.6, ["self_harm"]).classify("I keep thinking about ending my life")
    assert r.mode == Mode.RED_CRISIS and r.explicit_danger


def test_safety_net_overrides_a_green_model():
    r = clf("GREEN").classify("I've been giving away my stuff, won't need it")
    assert r.zone == Zone.RED and r.mode == Mode.RED_CRISIS and "self_harm" in r.categories


def test_model_outage_falls_back_to_yellow_or_red_never_green():
    assert clf(fail=True).classify("pricing question").zone == Zone.YELLOW
    assert clf(fail=True).classify("I want to kill myself").zone == Zone.RED


def test_safety_net_ignores_startup_hyperbole():
    for m in ("I could kill for a seed round", "this pitch is going to kill them", "I killed it at the demo",
              "We won't need it anymore since we switched vendors", "what's the point of this feature?"):
        assert safety_net(m) == (None, None), m


def test_compute_signals():
    kpis = ([{"week_number": 1, "type": "professional", "status": "done"}] * 3 +
            [{"week_number": 1, "type": "personal", "status": "done"}] * 2 +
            [{"week_number": w, "type": "professional", "status": "missed"} for w in (2, 3) for _ in range(3)] +
            [{"week_number": w, "type": "personal", "status": "missed"} for w in (2, 3) for _ in range(2)])
    s = compute_signals(kpis, ["so tired and stressed", "exhausted", "fine"], current_week=4)
    assert s.consecutive_missed_weeks == 2 and s.negative_reflections_recent == 2
    assert s.personal_completion_last == 0 and s.personal_completion_prior == 0.5 and s.personal_drop is True


def test_crisis_messages_by_category_and_country():
    us = crisis_message("US", "Sam", ["self_harm"])
    assert "988" in us and "911" in us and us.startswith("Sam,")
    med = crisis_message("US", None, ["medical"])
    assert "911" in med and "doctor" in med and "on hold" in med and "988" not in med
    assert "Samaritans" in crisis_message("GB") and "999" in crisis_message("GB")
    assert "findahelpline.com" in crisis_message("BR")
    assert "1-800-799-7233" in crisis_message("US", None, ["abuse"])
