from app.guardrails import clean_kpis, personal_kpi_problem, professional_kpi_problem


def test_moderate_personal_kpis_pass():
    for t in ("Jog 10K this week", "Gym 2 days", "Sleep 7+ hours on 5 nights", "Two 30-minute walks", "Run 20 km across the week"):
        assert personal_kpi_problem(t) is None, t


def test_extreme_personal_kpis_are_caught():
    for t in ("Sleep 5 hours a night", "Only 4 hours of sleep", "Fast until dinner", "OMAD this week", "Lose 3 kg",
              "Run a marathon", "Run 50 km this week", "Two workouts a day", "No days off", "Pull all-nighters to ship"):
        assert personal_kpi_problem(t), t


def test_grind_professional_kpis_are_dropped():
    assert professional_kpi_problem("Work 90 hours a week") and professional_kpi_problem("Work through the weekend")
    assert professional_kpi_problem("Interview 5 customers") is None


def test_clean_replaces_and_fills():
    prof, pers, notes = clean_kpis({"professional": [{"title": "Ship v1"}], "personal": [{"title": "Sleep 4 hours"}]}, wellbeing=False)
    assert [p["title"] for p in prof] == ["Ship v1"] and len(pers) == 2 and pers[0]["replaced"] and notes


def test_wellbeing_always_includes_rest():
    _, pers, _ = clean_kpis({"professional": [], "personal": [{"title": "Gym 3 days"}, {"title": "Walk daily"}]}, wellbeing=True)
    assert any("Sleep" in p["title"] or "day off" in p["title"].lower() for p in pers)
