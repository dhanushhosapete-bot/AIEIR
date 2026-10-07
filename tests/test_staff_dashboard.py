"""The five dashboard views return the right data."""
from app.staff import precision_recall
from tests.conftest import hdr, make_staff, signup


def _say(client, fake, h, zone, cats, msg="hello"):
    fake.script.zone = {"zone": zone, "confidence": 0.9 if zone != "RED" else 0.95, "categories": cats, "rationale": f"{zone} because"}
    fake.script.reply = "ok\n<flow>proceed</flow>"
    return client.post("/chat", json={"message": msg}, headers=hdr(h))


def test_overview_counts_sessions_and_pins_red(client, clean, fake):
    a, b, c = signup(client, "a@x.com", "Ana"), signup(client, "b@x.com", "Ben"), signup(client, "c@x.com", "Cy")
    _say(client, fake, a, "GREEN", [])
    _say(client, fake, b, "YELLOW", ["legal"])
    _say(client, fake, c, "RED", ["self_harm"], "I want to die")
    o = client.get("/staff/api/overview", headers=make_staff(clean, "eir")).json()
    assert o["active_sessions"] == {"green": 1, "yellow": 1, "red": 1}
    assert len(o["red_alerts"]) == 1 and o["red_alerts"][0]["founder"] == "Cy" and o["red_alerts"][0]["emailed"]


def test_founder_list_current_zone_is_latest_session_and_red_pinned_first(client, clean, fake):
    a, b = signup(client, "a@x.com", "Ana"), signup(client, "b@x.com", "Ben")
    _say(client, fake, a, "YELLOW", ["burnout"])
    _say(client, fake, b, "RED", ["self_harm"], "I want to die")
    rows = client.get("/staff/api/founders", headers=make_staff(clean, "eir")).json()
    assert [r["name"] for r in rows] == ["Ben", "Ana"]
    assert rows[0]["current_zone"] == "red" and rows[0]["open_alert"]
    assert rows[1]["current_zone"] == "yellow" and rows[1]["zone_trend_4w"][-1] == "yellow"


def test_zone_decays_after_quiet_period(client, clean, fake, admin_conn):
    a = signup(client)
    _say(client, fake, a, "YELLOW", ["burnout"])
    admin_conn.execute("UPDATE sessions SET last_activity_at = now() - interval '20 days'")
    rows = client.get("/staff/api/founders", headers=make_staff(clean, "eir")).json()
    assert rows[0]["current_zone"] is None


def test_detail_shows_rationale_message_and_change_markers(client, clean, fake):
    a = signup(client)
    _say(client, fake, a, "GREEN", [], "first")
    _say(client, fake, a, "YELLOW", ["burnout"], "so tired")
    d = client.get(f"/staff/api/founders/{a['_uid']}", headers=make_staff(clean, "eir")).json()
    assert [e["zone"] for e in d["timeline"]] == ["yellow", "green"]
    assert d["timeline"][0]["changed"] and d["timeline"][0]["message"] == "so tired"
    assert d["timeline"][0]["rationale"] == "YELLOW because"


def test_review_queue_and_accuracy_metrics(client, clean, fake):
    a = signup(client)
    _say(client, fake, a, "YELLOW", ["burnout"], "tired")
    _say(client, fake, a, "RED", ["self_harm"], "I want to die")
    rev = make_staff(clean, "safety_reviewer")
    q = client.get("/staff/api/reviews", headers=rev).json()
    assert [i["zone"] for i in q] == ["red", "yellow"]            # RED first
    red, yellow = q
    client.post(f"/staff/api/reviews/{red['event_id']}", json={"verdict": "resolved", "human_zone": "red"}, headers=rev)
    client.post(f"/staff/api/reviews/{yellow['event_id']}", json={"verdict": "disagree", "human_zone": "green"}, headers=rev)
    assert client.get("/staff/api/reviews", headers=rev).json() == []
    t = client.get("/staff/api/trends", headers=rev).json()
    assert t["reviews"] == 2 and t["agreement_rate"] == 0.5
    assert t["classifier_precision_recall"]["red"] == {"precision": 1.0, "recall": 1.0, "reviewed": 1}
    assert t["classifier_precision_recall"]["yellow"]["precision"] == 0.0
    # resolving through review closes the alert
    assert client.get("/staff/api/overview", headers=rev).json()["red_alerts"] == []


def test_trends_are_deidentified(client, clean, fake):
    a = signup(client, "a@x.com", "Ana")
    _say(client, fake, a, "YELLOW", ["legal"])
    t = client.get("/staff/api/trends", headers=make_staff(clean, "eir")).json()
    text = str(t)
    assert a["_uid"] not in text and "Ana" not in text
    assert t["top_categories"] == [] and t["categories_suppressed_under_5"] == 1


def test_sampled_green_reaches_the_review_queue(client, clean, fake, admin_conn):
    from app.classifier import ZoneClassifier
    from app import services
    a = signup(client)
    services.founder_turn(a["_uid"], "routine update", classifier=ZoneClassifier(sample_rate=1.0))
    q = client.get("/staff/api/reviews", headers=make_staff(clean, "safety_reviewer")).json()
    assert len(q) == 1 and q[0]["sampled_green"]


def test_precision_recall_math():
    pr = precision_recall([("red", "red"), ("red", "yellow"), ("yellow", "red"), ("green", "green")])
    assert pr["red"] == {"precision": 0.5, "recall": 0.5, "reviewed": 2}
    assert pr["green"] == {"precision": 1.0, "recall": 1.0, "reviewed": 1}


def test_dashboard_page_is_served_without_data(client):
    r = client.get("/staff")
    assert r.status_code == 200 and "Staff token" in r.text and r.headers["x-frame-options"] == "DENY"
