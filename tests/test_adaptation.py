"""Per-founder adaptation: learns from the founder's own results and ratings, within limits."""
from app.adaptation import recompute, tag_reflection

BASE = {"professional_count": 3, "professional_stretch": 2, "personal_stretch": 1, "tone": "balanced",
        "wellbeing_weeks_left": 0, "paused": False}


def week(n, prof_done, pers_done=2):
    return ([{"week_number": n, "type": "professional", "status": "done" if i < prof_done else "missed"} for i in range(3)] +
            [{"week_number": n, "type": "personal", "status": "done" if i < pers_done else "missed"} for i in range(2)])


def test_two_strong_weeks_stretch_up():
    L = recompute(BASE, week(1, 3) + week(2, 3), [], [], starting_week=3)
    assert L.professional_stretch == 3 and L.changes and L.notes


def test_low_completion_with_scope_reasons_lightens_load():
    L = recompute(BASE, week(1, 1), [{"tags": ["scope"]}], [], starting_week=2)
    assert L.professional_count == 2 and L.professional_stretch == 1


def test_wellbeing_mode_never_increases_anything():
    st = dict(BASE, wellbeing_weeks_left=2, professional_stretch=3)
    L = recompute(st, week(1, 3) + week(2, 3), [], [{"rating": -1, "reasons": ["too_little"]}], starting_week=3)
    assert (L.professional_count, L.professional_stretch, L.personal_stretch) == (1, 1, 1)
    assert L.wellbeing_weeks_left == 1


def test_count_recovers_one_step_at_a_time_after_wellbeing():
    st = dict(BASE, professional_count=1, professional_stretch=1)
    L = recompute(st, week(1, 3) + week(2, 3), [], [], starting_week=3)
    assert L.professional_count == 2 and L.professional_stretch == 1


def test_tone_follows_feedback_one_step():
    assert recompute(BASE, [], [], [{"rating": -1, "reasons": ["too_harsh"]}], starting_week=1).tone == "gentle"
    assert recompute(BASE, [], [], [{"rating": -1, "reasons": ["too_soft"]}], starting_week=1).tone == "direct"
    gentle = dict(BASE, tone="gentle")
    assert recompute(gentle, [], [], [{"rating": -1, "reasons": ["too_harsh"]}], starting_week=1).tone == "gentle"


def test_personal_stretch_is_capped_at_moderate():
    L = recompute(dict(BASE, personal_stretch=2), week(1, 3) + week(2, 3), [], [{"rating": -1, "reasons": ["too_little"]}],
                  starting_week=3)
    assert L.personal_stretch <= 2


def test_reflection_tags():
    assert tag_reflection("Supplier didn't reply, waiting on them", False) == ["blocker"]
    assert set(tag_reflection("too much on my plate and I'm exhausted", False)) == {"scope", "energy"}
    assert tag_reflection(None, True) == ["skipped"]


def test_founder_sees_and_resets_what_was_learned(client, fake):
    from tests.conftest import hdr, signup
    h = signup(client)
    mid = client.post("/chat", json={"message": "hi"}, headers=hdr(h)).json()["message_id"]
    client.post(f"/messages/{mid}/feedback", json={"rating": -1, "reasons": ["too_harsh"]}, headers=hdr(h))
    a = client.get("/me/adaptation", headers=hdr(h)).json()
    assert a["tone"] == "gentle" and any("gentler" in n for n in a["what_ive_learned"])
    r = client.put("/me/adaptation", json={"professional_stretch": 9}, headers=hdr(h)).json()
    assert r["professional_difficulty"] == "stretch"                 # clamped to the maximum
    r = client.delete("/me/adaptation", headers=hdr(h)).json()
    assert r["tone"] == "balanced" and r["what_ive_learned"] == []
