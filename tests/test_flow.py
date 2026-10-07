"""Intake -> roadmap -> weekly KPI loop, reflection gate, and zone routing end to end."""
from app.safety import Mode, Zone
from tests.conftest import finish_intake, hdr, signup


def test_signup_requires_explicit_consent(client):
    r = client.post("/signup", json={"email": "a@x.com", "consent": {"version": "safety-review-v1", "accepted": False}})
    assert r.status_code == 400
    r = client.post("/signup", json={"email": "a@x.com", "consent": {"version": "old-version", "accepted": True}})
    assert r.status_code == 400
    text = client.get("/consent").json()["text"]
    assert "checked for signs" in text and "a person from that team may reach out" in text


def test_full_loop_with_reflection_gate(client, fake):
    h = signup(client)
    assert client.get("/intake", headers=hdr(h)).json()["total"] == 10
    out = finish_intake(client, h)
    assert out["done"] is True
    rm = client.post("/roadmap", headers=hdr(h)).json()
    assert len(rm["weeks"]) == 4 and rm["end_date"]

    w1 = client.post("/weeks/next", headers=hdr(h)).json()
    types = [k["type"] for k in w1["kpis"]]
    assert types.count("professional") == 3 and types.count("personal") == 2

    # can't start week 2 while week 1 is open
    r = client.post("/weeks/next", headers=hdr(h))
    assert r.status_code == 409 and r.json()["blocked"] == "week_open"

    ids = [k["id"] for k in w1["kpis"]]
    for i in ids[:3]:
        client.post(f"/kpis/{i}/status", json={"status": "done"}, headers=hdr(h))
    closed = client.post("/weeks/close", headers=hdr(h)).json()
    assert len(closed["missed_needing_reflection"]) == 2

    # missed KPIs must be explained before week 2 unlocks...
    r = client.post("/weeks/next", headers=hdr(h))
    assert r.status_code == 409 and r.json()["blocked"] == "reflection_required"
    assert "I'd rather not say" in r.json()["message"]

    # ...and "I'd rather not say" is a complete answer, with no model call needed
    calls_before = len(fake.calls)
    r = client.post("/reflections", json={"kpi_ids": closed["missed_needing_reflection"], "text": "I'd rather not say"},
                    headers=hdr(h))
    assert r.status_code == 200 and "no explanation needed" in r.json()["reply"]
    assert len(fake.calls) == calls_before

    w2 = client.post("/weeks/next", headers=hdr(h))
    assert w2.status_code == 200, w2.text
    assert w2.json()["week"] == 2


def test_reflection_with_text_gets_an_eir_reply(client, fake):
    h = signup(client)
    finish_intake(client, h)
    client.post("/roadmap", headers=hdr(h))
    w1 = client.post("/weeks/next", headers=hdr(h)).json()
    client.post("/weeks/close", headers=hdr(h))
    r = client.post("/reflections", json={"kpi_ids": [w1["kpis"][0]["id"]], "text": "Got busy with a client deadline."},
                    headers=hdr(h)).json()
    assert r["reply"].startswith("Thanks for telling me") and r["message_id"]


def test_red_crisis_uses_fixed_reply_alerts_and_pauses(client, fake, mailbox, admin_conn):
    h = signup(client)
    fake.script.zone = {"zone": "RED", "confidence": 0.95, "categories": ["self_harm"], "rationale": "Explicit ideation."}
    calls_before = len(fake.calls)
    r = client.post("/chat", json={"message": "I keep thinking about ending my life."}, headers=hdr(h)).json()
    assert "988" in r["reply"] and "911" in r["reply"]
    assert r["kpis_paused"] is True
    # only the classifier was called: the crisis reply never depends on the model
    assert [c["tool"]["name"] if c["tool"] else "reply" for c in fake.calls[calls_before:]] == ["record_zone"]
    alert = admin_conn.execute("SELECT * FROM alerts").fetchone()
    assert alert["status"] == "open" and alert["first_notified_at"] is not None
    assert mailbox.sent and mailbox.sent[0]["to"] == "primary@oncall.test"
    assert "ending my life" not in mailbox.sent[0]["body"] and "Sam" not in mailbox.sent[0]["body"]
    # KPIs stay off, but the conversation never closes
    assert client.post("/weeks/next", headers=hdr(h)).json()["blocked"] == "paused"
    assert client.post("/resume", headers=hdr(h)).json()["blocked"] == "not_yet"
    fake.script.zone = {"zone": "GREEN", "confidence": 0.9, "categories": [], "rationale": "ok"}
    again = client.post("/chat", json={"message": "thanks for listening"}, headers=hdr(h))
    assert again.status_code == 200 and again.json()["reply"]


def test_burnout_message_is_red_check_in_with_gentle_model_reply(client, fake, mailbox, admin_conn):
    """Prompt 1 Case 1 under the agreed rule: RED alert to staff, gentle check-in to the founder."""
    h = signup(client)
    fake.script.zone = {"zone": "YELLOW", "confidence": 0.6, "categories": ["burnout"], "rationale": "Exhaustion."}
    fake.script.reply = "That sounds draining. How are you doing beyond the business?\n<flow>pause</flow>"
    msg = ("I've missed all three of my KPIs two weeks in a row. Honestly I can't focus, I haven't been sleeping, "
           "and I don't know why I'm even doing this anymore.")
    r = client.post("/chat", json={"message": msg}, headers=hdr(h)).json()
    assert r["reply"].startswith("That sounds draining")
    system = fake.calls[-1]["system"]
    assert "Check-in mode" in system and "988" in system
    ev = admin_conn.execute("SELECT zone, response_mode, escalation_reasons FROM zone_events").fetchone()
    assert ev["zone"] == "red" and ev["response_mode"] == Mode.RED_CHECK_IN.value
    assert any("safety_net_doubt" in x for x in ev["escalation_reasons"])
    assert admin_conn.execute("SELECT count(*) AS n FROM alerts").fetchone()["n"] == 1
    assert mailbox.sent


def test_yellow_wellbeing_pauses_then_lighter_weeks(client, fake):
    h = signup(client)
    finish_intake(client, h)
    client.post("/roadmap", headers=hdr(h))
    client.post("/weeks/next", headers=hdr(h))
    fake.script.zone = {"zone": "YELLOW", "confidence": 0.8, "categories": ["burnout"], "rationale": "Stress."}
    fake.script.reply = "That's a lot to carry. Want a lighter week?\n<flow>proceed</flow>"   # model says proceed...
    r = client.post("/chat", json={"message": "I'm exhausted and stressed, haven't had a day off in weeks."}, headers=hdr(h)).json()
    assert r["kpis_paused"] is True                                                         # ...code pauses anyway
    client.post("/weeks/close", headers=hdr(h))
    home = client.get("/home", headers=hdr(h)).json()
    missed = [k["id"] for k in home["kpis"] if k["status"] == "missed"]
    fake.script.zone = {"zone": "GREEN", "confidence": 0.9, "categories": [], "rationale": "ok"}
    fake.script.reply = "Thanks.\n<flow>proceed</flow>"
    client.post("/reflections", json={"kpi_ids": missed, "skip": True}, headers=hdr(h))
    assert client.post("/weeks/next", headers=hdr(h)).json()["blocked"] == "paused"
    assert client.post("/resume", headers=hdr(h)).json()["kpis_paused"] is False
    w2 = client.post("/weeks/next", headers=hdr(h)).json()
    prof = [k for k in w2["kpis"] if k["type"] == "professional"]
    pers = [k["title"] for k in w2["kpis"] if k["type"] == "personal"]
    assert len(prof) == 1
    assert any("Sleep" in t for t in pers)


def test_yellow_ethics_question_does_not_pause(client, fake):
    h = signup(client)
    fake.script.zone = {"zone": "YELLOW", "confidence": 0.85, "categories": ["ethics"], "rationale": "Borderline request."}
    fake.script.reply = "I won't help make the numbers look better than they are. Let's build the honest story.\n<flow>proceed</flow>"
    r = client.post("/chat", json={"message": "How do I make flat growth look better for investors?"}, headers=hdr(h)).json()
    assert r["kpis_paused"] is False
    assert "Care mode" in fake.calls[-1]["system"]


def test_classifier_outage_is_never_green(client, fake, admin_conn):
    h = signup(client)
    fake.script.fail_classifier = True
    r = client.post("/chat", json={"message": "quick question about pricing"}, headers=hdr(h))
    assert r.status_code == 200
    ev = admin_conn.execute("SELECT zone, escalation_reasons FROM zone_events").fetchone()
    assert ev["zone"] == "yellow" and "classifier_unavailable" in ev["escalation_reasons"]


def test_unsafe_personal_kpi_is_replaced(client, fake):
    h = signup(client)
    finish_intake(client, h)
    client.post("/roadmap", headers=hdr(h))
    fake.script.kpis = {"professional": [{"title": "Close 2 pilots"}, {"title": "Work 90 hours a week"}],
                        "personal": [{"title": "Sleep 4 hours a night to get more done"}, {"title": "Run a marathon"}]}
    w = client.post("/weeks/next", headers=hdr(h)).json()
    titles = [k["title"] for k in w["kpis"]]
    assert "Work 90 hours a week" not in titles
    assert not any("4 hours" in t or "marathon" in t for t in titles)
    assert sum(k["type"] == "personal" for k in w["kpis"]) == 2


def test_intake_skip_is_accepted(client):
    h = signup(client)
    r = client.post("/intake/answer", json={"skip": True}, headers=hdr(h)).json()
    assert r["answered"] == 1 and r["next_question"]


def test_green_intake_answer_makes_no_reply_call(client, fake):
    h = signup(client)
    before = len(fake.calls)
    client.post("/intake/answer", json={"answer": "A linen clothing brand"}, headers=hdr(h))
    tools = [c["tool"]["name"] if c["tool"] else "reply" for c in fake.calls[before:]]
    assert tools == ["record_zone"]
