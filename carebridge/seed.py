"""Demo data. Every person here is fictional. Run: .venv/bin/flask --app app seed

Demo sign-ins (local testing only):
    patient   jordan@demo.carebridge   Demo-Patient-2026!
    patient   sam@demo.carebridge      Demo-Patient-2026!
    provider  dr.lee@demo.carebridge   Demo-Provider-2026!
    provider  dr.new@demo.carebridge   Demo-Provider-2026!   (no training on file, so records are locked)
    admin     admin@demo.carebridge    Demo-Admin-2026!
"""

import json
import random
from datetime import date, datetime, timedelta, timezone

from .db import DB_PATH, connect, enc, init_db, now_iso

PATIENT_PW, PROVIDER_PW, ADMIN_PW = "Demo-Patient-2026!", "Demo-Provider-2026!", "Demo-Admin-2026!"


def _no(evidence=""):
    return {"answer": "no", "evidence": evidence}


def seed(hash_password):
    if DB_PATH.exists():
        DB_PATH.unlink()
    init_db()
    rnd = random.Random(7)
    today = date.today()
    with connect() as db:
        def user(email, pw, role, name, contact=None, hired=None):
            return db.execute(
                "INSERT INTO users (email, password_hash, role, name_enc, emergency_contact_enc, hired_on, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (email, hash_password(pw), role, enc(name), enc(json.dumps(contact)) if contact else None, hired, now_iso()),
            ).lastrowid

        lee = user("dr.lee@demo.carebridge", PROVIDER_PW, "provider", "Dr. Priya Lee",
                   {"phone": "+1-555-0100", "email": "oncall@demo.carebridge", "backup_phone": "+1-555-0101"},
                   (today - timedelta(days=400)).isoformat())
        new = user("dr.new@demo.carebridge", PROVIDER_PW, "provider", "Dr. Alex Moreno",
                   {"phone": "+1-555-0110", "email": "oncall@demo.carebridge"}, (today - timedelta(days=10)).isoformat())
        admin = user("admin@demo.carebridge", ADMIN_PW, "admin", "Casey Admin", None, (today - timedelta(days=800)).isoformat())
        for uid in (lee, admin):
            done = today - timedelta(days=120)
            db.execute("INSERT INTO training_logs (user_id, course, completed_on, expires_on, recorded_by) VALUES (?, ?, ?, ?, ?)",
                       (uid, "Texas HB 300 Privacy (role-specific)", done.isoformat(),
                        (done + timedelta(days=730)).isoformat(), admin))

        patients = [
            ("jordan@demo.carebridge", "Jordan Rivera", "1961-04-12", "MRN-100234", "Heart failure, post-discharge",
             [("medication", "Furosemide", "40 mg by mouth, in the morning", "08:00", 0.95),
              ("medication", "Metoprolol succinate", "25 mg by mouth", "08:00", 0.9),
              ("medication", "Lisinopril", "10 mg by mouth", "20:00", 0.7),
              ("exercise", "Walk", "20 minutes at an easy pace", "10:00", 0.55),
              ("other", "Weigh yourself", "Before breakfast. Call clinic if up 3 lb in a day", "07:30", 0.85)],
             [(4, "Felt pretty good today. Walked to the mailbox and back twice.", "none", ["hopeful", "proud"]),
              (3, "Ankles a bit swollen this evening. Remembered all my pills though.", "none", ["worried", "tired"]),
              (2, "Tired all day. Some days I wonder what the point of all this is, but my granddaughter visited and that helped.",
               "low", ["tired", "discouraged", "grateful"])]),
            ("sam@demo.carebridge", "Sam Patel", "1978-09-30", "MRN-100871", "Community-acquired pneumonia, recovering",
             [("medication", "Amoxicillin-clavulanate", "875 mg by mouth with food", "08:00", 1.0),
              ("medication", "Amoxicillin-clavulanate", "875 mg by mouth with food", "20:00", 0.85),
              ("exercise", "Incentive spirometer", "10 breaths, every waking hour", "09:00", 0.6),
              ("exercise", "Walk", "10 minutes, twice a day", "16:00", 0.75)],
             [(3, "Cough is a bit better. Still short of breath on the stairs.", "none", ["frustrated", "hopeful"]),
              (4, "Slept through the night for the first time!", "none", ["relieved", "happy"])]),
        ]
        for email, name, dob, mrn, condition, items, journals in patients:
            uid = user(email, PATIENT_PW, "patient", name)
            pid = db.execute("INSERT INTO patients (user_id, provider_id, dob_enc, mrn_enc, condition_enc, discharged_on) "
                             "VALUES (?, ?, ?, ?, ?, ?)",
                             (uid, lee, enc(dob), enc(mrn), enc(condition), (today - timedelta(days=9)).isoformat())).lastrowid
            plan = db.execute("INSERT INTO care_plans (patient_id, source, status, created_at, reviewed_by, reviewed_at) "
                              "VALUES (?, 'provider', 'approved', ?, ?, ?)", (pid, now_iso(), lee, now_iso())).lastrowid
            for kind, title, details, tm, rate in items:
                iid = db.execute("INSERT INTO care_items (patient_id, care_plan_id, kind, title_enc, details_enc, time_of_day) "
                                 "VALUES (?, ?, ?, ?, ?, ?)", (pid, plan, kind, enc(title), enc(details), tm)).lastrowid
                for back in range(1, 8):  # history for the past week, not today
                    if rnd.random() < rate:
                        db.execute("INSERT INTO completions (care_item_id, day, completed_at) VALUES (?, ?, ?)",
                                   (iid, (today - timedelta(days=back)).isoformat(), now_iso()))
            for n, (mood, text, level, emotions) in enumerate(journals):
                when = (datetime.now(timezone.utc) - timedelta(days=2 * (len(journals) - n))).isoformat(timespec="seconds")
                screen = {k: _no() for k in ("q1_wish_to_be_dead", "q2_suicidal_thoughts", "q3_thoughts_with_method",
                                             "q4_intent", "q5_plan", "q6_behavior", "self_harm_non_suicidal", "harm_to_others")}
                screen.update(distress_level=2, emotions=emotions, summary="No indicators of suicidal ideation.")
                if level == "low":
                    screen["q1_wish_to_be_dead"] = {"answer": "unclear",
                                                    "evidence": "Some days I wonder what the point of all this is"}
                    screen.update(distress_level=5, summary="Expresses hopelessness about recovery; no explicit wish to die "
                                                            "or suicidal thoughts. Protective factor: family visit.")
                jid = db.execute("INSERT INTO journal_entries (patient_id, created_at, text_enc, mood, emotions_enc, risk_level, "
                                 "screen_enc, screen_status) VALUES (?, ?, ?, ?, ?, ?, ?, 'ai')",
                                 (pid, when, enc(text), mood, enc(emotions), level, enc(screen))).lastrowid
                if level == "low":
                    db.execute("INSERT INTO alerts (patient_id, source, source_id, level, summary_enc, status, created_at) "
                               "VALUES (?, 'journal', ?, 'low', ?, 'open', ?)", (pid, jid, enc(screen["summary"]), when))
        db.commit()
