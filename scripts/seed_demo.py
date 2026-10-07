"""Seed a development database with clearly fictional demo founders, so the staff dashboard
has something to show. Uses a scripted stand-in model: no API key, no real people.

    ADMIN_DATABASE_URL=... DATABASE_URL=... STAFF_DATABASE_URL=... AIEIR_DATA_KEYS=... \
      python scripts/seed_demo.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import alerts, db, llm, services  # noqa: E402
from app.classifier import ZoneClassifier  # noqa: E402
from app.staff_admin import create  # noqa: E402

SCRIPT = [
    ("Demo Ana (LumenWear)", "ana@demo.invalid", [
        ("Shipped the v2 firmware and booked 3 clinic demos.", "GREEN", [], "Routine progress update."),
        ("Missed the BOM KPI, supplier never replied.", "GREEN", [], "Ordinary reason for a missed KPI.")]),
    ("Demo Ben (Shelfie)", "ben@demo.invalid", [
        ("My numbers are flat but I'm pitching Friday. How do I make growth look better than it is?", "YELLOW", ["ethics"],
         "Asks for help presenting numbers misleadingly.")]),
    ("Demo Cy (Kilnworks)", "cy@demo.invalid", [
        ("I've been getting chest pains from all the stress. What should I do?", "YELLOW", ["medical"],
         "Non-acute physical symptom; needs a doctor.")]),
    ("Demo Dee (Thread & Thimble)", "dee@demo.invalid", [
        ("Two weeks of missed KPIs. I can't focus, I'm not sleeping, and I don't know why I'm even doing this anymore.",
         "YELLOW", ["burnout"], "Exhaustion with a possible hopelessness signal.")]),
    ("Demo Eli (Brightlane)", "eli@demo.invalid", [
        ("What KPIs is that other founder in my cohort working on?", "YELLOW", ["privacy"], "Asks about another founder's data.")]),
    ("Demo Fay (Orbitly)", "fay@demo.invalid", [
        ("this startup is killing me lol, third all-nighter", "GREEN", [], "Startup hyperbole, no danger signal."),
        ("Landing page is live, 40 signups.", "GREEN", [], "Routine update.")]),
]


def main():
    admin = os.environ["ADMIN_DATABASE_URL"]
    alerts.set_sender(alerts.OutboxSender("outbox"))
    alerts.RUN_INLINE = True
    print("Staff tokens (demo only):")
    for role in ("admin", "safety_reviewer", "eir"):
        print(f"  {role:<16}", create(admin, f"{role}@demo.invalid", f"Demo {role.replace('_', ' ')}", role, "2026-09-01"))
    for name, email, turns in SCRIPT:
        token = "fdr_demo_" + email.split("@")[0]
        uid = db.create_founder(email, name, "US", token, services.CONSENT_VERSION)
        for text, zone, cats, why in turns:
            fake = llm.FakeLLM(responder=lambda tool=None, z=zone, c=cats, w=why, **_:
                               {"zone": z, "confidence": 0.85, "categories": c, "rationale": w} if tool
                               else "Thanks for sharing that with me.\n<flow>proceed</flow>")
            services.founder_turn(uid, text, classifier=ZoneClassifier(llm=fake, sample_rate=0), llm=fake)
    print("Seeded", len(SCRIPT), "demo founders.")
    for p in db._pools.values():
        p.close()


if __name__ == "__main__":
    main()
