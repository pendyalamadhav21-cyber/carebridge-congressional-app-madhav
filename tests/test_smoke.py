"""End-to-end smoke tests without calling the AI. Run: .venv/bin/python -m pytest -q  (or: .venv/bin/python tests/test_smoke.py)"""

import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["CAREBRIDGE_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["ANTHROPIC_API_KEY"] = ""  # force the no-AI paths

import app as cb  # noqa: E402
from carebridge import ai  # noqa: E402
from carebridge.seed import ADMIN_PW, PATIENT_PW, PROVIDER_PW, seed  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

seed(generate_password_hash)
cb.app.config["TESTING"] = True


def login(email, pw):
    c = cb.app.test_client()
    page = c.get("/login").get_data(as_text=True)
    token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
    r = c.post("/login", data={"email": email, "password": pw, "csrf_token": token})
    assert r.status_code == 302, r.status_code
    page = c.get("/", follow_redirects=True).get_data(as_text=True)
    token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
    return c, token


def test_risk_levels():
    def screen(**answers):
        base = {k: ai.CSSRSItem(answer="no", evidence="") for k in (
            "q1_wish_to_be_dead", "q2_suicidal_thoughts", "q3_thoughts_with_method", "q4_intent", "q5_plan",
            "q6_behavior", "self_harm_non_suicidal", "harm_to_others")}
        base.update({k: ai.CSSRSItem(answer=v, evidence="x") for k, v in answers.items()})
        return ai.RiskScreen(**base, distress_level=2, emotions=[], summary="")
    assert ai.risk_level(screen()) == "none"
    assert ai.risk_level(screen(q1_wish_to_be_dead="yes")) == "low"
    assert ai.risk_level(screen(q2_suicidal_thoughts="yes")) == "moderate"
    assert ai.risk_level(screen(q4_intent="yes")) == "high"
    assert ai.risk_level(screen(q5_plan="yes")) == "imminent"


def test_keyword_fallback_never_fails_open():
    level, detail, status = ai.screen_text("I want to end my life")
    assert status == "keyword_fallback" and level == "high"
    assert ai.screen_text("Walked 20 minutes today")[0] == "none"


def test_patient_flow():
    c, token = login("jordan@demo.carebridge", PATIENT_PW)
    for path in ("/patient", "/patient/journal", "/patient/chat", "/patient/upload", "/patient/records", "/support"):
        assert c.get(path).status_code == 200, path
    item = re.search(r'data-toggle="(/patient/item/\d+/toggle)"', c.get("/patient").get_data(as_text=True)).group(1)
    assert c.post(item).status_code == 400  # missing CSRF token
    assert c.post(item, headers={"X-CSRF-Token": token}).get_json() == {"done": True}
    bundle = c.get("/patient/records/fhir.json").get_json()
    types = {e["resource"]["resourceType"] for e in bundle["entry"]}
    assert {"Patient", "CarePlan", "MedicationStatement", "Observation", "RiskAssessment"} <= types
    # Journal with crisis language escalates even without the AI (keyword fallback).
    r = c.post("/patient/journal", data={"text": "I don't want to live anymore", "mood": "1", "csrf_token": token})
    assert r.headers["Location"].startswith("/support")
    assert c.get("/provider").status_code == 403  # patients can't open the provider portal


def test_provider_access_control():
    c, _ = login("dr.lee@demo.carebridge", PROVIDER_PW)
    page = c.get("/provider").get_data(as_text=True)
    assert "Jordan Rivera" in page and "high" in page  # alert from the journal test above, if run in order
    assert c.get("/provider/patient/1").status_code == 200
    assert c.get("/provider/patient/999").status_code == 404
    locked, _ = login("dr.new@demo.carebridge", PROVIDER_PW)
    assert locked.get("/provider").headers["Location"].endswith("/training")  # no training -> no PHI
    assert locked.get("/provider/patient/1").status_code in (302, 404)


def test_admin_and_encryption():
    c, _ = login("admin@demo.carebridge", ADMIN_PW)
    assert "Audit log" in c.get("/admin").get_data(as_text=True)
    assert c.get("/provider/patient/1").status_code == 403
    raw = Path(os.environ["CAREBRIDGE_DB"]).read_bytes()
    assert b"Jordan" not in raw and b"Furosemide" not in raw and b"granddaughter" not in raw


def test_reminders_due_followup_and_done():
    from datetime import datetime
    from carebridge import reminders
    from carebridge.db import connect, enc

    sent = []
    reminders.webpush = lambda sub, data, **kw: sent.append(json.loads(data))
    with connect() as db:
        db.execute("INSERT INTO push_subscriptions (patient_id, endpoint_hash, subscription_enc, created_at) "
                   "VALUES (2, 'h', ?, 'now')", (enc('{"endpoint": "https://push.example/x", "keys": {}}'),))
        db.commit()
    day = datetime.now().date()
    at = lambda hh, mm: datetime.combine(day, datetime.strptime(f"{hh}:{mm}", "%H:%M").time())
    assert reminders.run_once(at("07", "59")) == 0           # not due yet
    assert reminders.run_once(at("08", "00")) == 1           # Sam's 08:00 medication
    assert reminders.run_once(at("08", "05")) == 0           # no duplicates
    assert "Amoxicillin" not in sent[0]["body"]               # no PHI on the lock screen by default
    assert reminders.run_once(at("08", "30")) == 1           # follow-up, still not done
    assert sent[1]["title"].startswith("Reminder")
    with connect() as db:                                     # marking the 16:00 walk done suppresses its reminder
        walk = db.execute("SELECT id FROM care_items WHERE patient_id = 2 AND time_of_day = '16:00'").fetchone()[0]
        db.execute("INSERT INTO completions (care_item_id, day, completed_at) VALUES (?, ?, 'now')", (walk, day.isoformat()))
        db.commit()
    assert reminders.run_once(at("16", "00")) == 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
