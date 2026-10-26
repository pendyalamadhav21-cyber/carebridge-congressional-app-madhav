"""CareBridge: an AI-driven clinical companion for post-acute and chronic-care patients."""

import json
import os
import secrets
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,  # noqa: E402
                   session, stream_with_context, url_for)
from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402

from carebridge import ai, db as store, fhir, reminders, security  # noqa: E402
from carebridge.db import dec, dec_json, enc, get_db, now_iso  # noqa: E402
from carebridge.escalation import escalate  # noqa: E402
from carebridge.security import audit, login_required  # noqa: E402

store.ensure_encryption_key()
store.init_db()
reminders.ensure_vapid_keys()

app = Flask(__name__, template_folder="carebridge/templates", static_folder="carebridge/static")
app.config.update(
    SECRET_KEY=os.environ.get("SESSION_SECRET") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,          # session cookie unreadable by page scripts
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.environ.get("HTTPS_ONLY", "0") == "1",
    SESSION_COOKIE_NAME="__cb_session",
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
)
app.teardown_appcontext(store.close_db)
app.before_request(security.load_user)
app.before_request(security.check_csrf)
app.after_request(security.set_security_headers)
app.jinja_env.globals.update(csrf_token=security.csrf_token, dec=dec)

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
RISK_ORDER = ai.RISK_ORDER
MOODS = {1: "Very low", 2: "Low", 3: "Okay", 4: "Good", 5: "Very good"}


@app.context_processor
def inject():
    return {"user": g.get("user"), "user_name": dec(g.user["name_enc"]) if g.get("user") else None,
            "MOODS": MOODS}


@app.template_filter("localtime")
def localtime(iso: str) -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone().strftime("%b %d, %I:%M %p")


@app.template_filter("clock")
def clock(hhmm: str) -> str:
    return datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0")


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

@app.get("/")
def home():
    if g.user is None:
        return redirect(url_for("login"))
    return redirect(url_for({"patient": "patient_home", "provider": "provider_home", "admin": "admin_home"}[g.user["role"]]))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if security.login_rate_limited(email):
            audit("login", email, outcome="rate_limited")
            flash("Too many failed attempts. Try again in 15 minutes.", "error")
            return render_template("login.html"), 429
        user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            security.clear_failed_logins(email)
            security.start_session(user)
            audit("login", user=user)
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("home"))
        security.record_failed_login(email)
        audit("login", email, outcome="failed")
        flash("Email or password is incorrect.", "error")
    elif g.get("session_expired"):
        flash("You were signed out after 15 minutes of inactivity.", "info")
    return render_template("login.html")


@app.post("/logout")
def logout():
    if g.user:
        audit("logout")
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Patient: daily checklist
# ---------------------------------------------------------------------------

def plan_items(patient_id: int):
    rows = get_db().execute(
        "SELECT ci.*, cp.status AS plan_status FROM care_items ci LEFT JOIN care_plans cp ON cp.id = ci.care_plan_id "
        "WHERE ci.patient_id = ? AND ci.active = 1 ORDER BY ci.time_of_day", (patient_id,)
    ).fetchall()
    return [{"id": r["id"], "kind": r["kind"], "title": dec(r["title_enc"]), "details": dec(r["details_enc"]) or "",
             "time": r["time_of_day"], "pending": r["plan_status"] == "pending_review"} for r in rows]


@app.get("/patient")
@login_required("patient")
def patient_home():
    p = security.current_patient()
    items = plan_items(p["id"])
    today = date.today().isoformat()
    done = {r["care_item_id"] for r in get_db().execute(
        "SELECT care_item_id FROM completions WHERE day = ? AND care_item_id IN "
        "(SELECT id FROM care_items WHERE patient_id = ?)", (today, p["id"]))}
    for it in items:
        it["done"] = it["id"] in done
    _, week = fhir.adherence(p["id"])
    db = get_db()
    devices = db.execute("SELECT COUNT(*) FROM push_subscriptions WHERE patient_id = ?", (p["id"],)).fetchone()[0]
    audit("view_checklist", f"Patient/{p['id']}")
    return render_template("patient_home.html", items=items, done_count=len(done), week=week,
                           today=date.today().strftime("%A, %B %d"), devices=devices,
                           prefs=reminders.prefs(db, p["id"]), vapid_key=os.environ["VAPID_PUBLIC_KEY"])


@app.post("/patient/item/<int:item_id>/toggle")
@login_required("patient")
def toggle_item(item_id):
    p = security.current_patient()
    db = get_db()
    item = db.execute("SELECT id FROM care_items WHERE id = ? AND patient_id = ? AND active = 1",
                      (item_id, p["id"])).fetchone()
    if not item:
        abort(404)
    today = date.today().isoformat()
    existing = db.execute("SELECT id FROM completions WHERE care_item_id = ? AND day = ?", (item_id, today)).fetchone()
    if existing:
        db.execute("DELETE FROM completions WHERE id = ?", (existing["id"],))
    else:
        db.execute("INSERT INTO completions (care_item_id, day, completed_at) VALUES (?, ?, ?)", (item_id, today, now_iso()))
    db.commit()
    audit("toggle_item", f"CareItem/{item_id}")
    return jsonify(done=not existing)


# ---------------------------------------------------------------------------
# Patient: reminder notifications (Web Push)
# ---------------------------------------------------------------------------

@app.get("/sw.js")
def service_worker():
    resp = app.send_static_file("sw.js")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/manifest.webmanifest")
def manifest():
    resp = app.send_static_file("manifest.webmanifest")
    resp.mimetype = "application/manifest+json"
    return resp


@app.post("/patient/push/subscribe")
@login_required("patient")
def push_subscribe():
    sub = request.get_json(silent=True) or {}
    endpoint = sub.get("endpoint", "")
    keys = sub.get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        return jsonify(error="Invalid subscription"), 400
    p = security.current_patient()
    db = get_db()
    clean = {"endpoint": endpoint, "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]}}
    db.execute("INSERT INTO push_subscriptions (patient_id, endpoint_hash, subscription_enc, created_at) VALUES (?, ?, ?, ?) "
               "ON CONFLICT(endpoint_hash) DO UPDATE SET patient_id = excluded.patient_id, "
               "subscription_enc = excluded.subscription_enc",
               (p["id"], reminders.endpoint_hash(endpoint), enc(json.dumps(clean)), now_iso()))
    db.commit()
    audit("push_subscribe", f"Patient/{p['id']}")
    return jsonify(ok=True)


@app.post("/patient/push/unsubscribe")
@login_required("patient")
def push_unsubscribe():
    endpoint = (request.get_json(silent=True) or {}).get("endpoint", "")
    p = security.current_patient()
    db = get_db()
    db.execute("DELETE FROM push_subscriptions WHERE patient_id = ? AND endpoint_hash = ?",
               (p["id"], reminders.endpoint_hash(endpoint)))
    db.commit()
    audit("push_unsubscribe", f"Patient/{p['id']}")
    return jsonify(ok=True)


@app.post("/patient/push/test")
@login_required("patient")
def push_test():
    p = security.current_patient()
    sent = reminders.send_to_patient(get_db(), p["id"], {
        "title": "CareBridge reminders are on",
        "body": "You will be notified when checklist items are due.", "tag": "test", "url": "/patient"})
    return jsonify(sent=sent)


@app.post("/patient/notifications/settings")
@login_required("patient")
def notification_settings():
    p = security.current_patient()
    db = get_db()
    db.execute("INSERT INTO notification_prefs (patient_id, show_details, followups) VALUES (?, ?, ?) "
               "ON CONFLICT(patient_id) DO UPDATE SET show_details = excluded.show_details, followups = excluded.followups",
               (p["id"], 1 if request.form.get("show_details") else 0, 1 if request.form.get("followups") else 0))
    db.commit()
    audit("notification_settings", f"Patient/{p['id']}")
    flash("Reminder settings saved.", "success")
    return redirect(url_for("patient_home") + "#reminders")


# ---------------------------------------------------------------------------
# Patient: photo of discharge instructions -> checklist
# ---------------------------------------------------------------------------

@app.route("/patient/upload", methods=["GET", "POST"])
@login_required("patient")
def upload():
    if request.method == "GET":
        return render_template("upload.html")
    f = request.files.get("photo")
    if not f or f.mimetype not in IMAGE_TYPES:
        flash("Please choose a JPG, PNG, WEBP or GIF photo.", "error")
        return redirect(url_for("upload"))
    data = f.read()
    if len(data) > 5 * 1024 * 1024:
        flash("That photo is over 5 MB. Try a smaller or lower-resolution photo.", "error")
        return redirect(url_for("upload"))
    try:
        plan = ai.extract_care_plan(data, f.mimetype)
    except ai.AIUnavailable:
        flash("The document reader is unavailable right now. Check the API key or try again later.", "error")
        return redirect(url_for("upload"))
    p = security.current_patient()
    audit("ocr_extract", f"Patient/{p['id']}")
    if not plan.readable:
        flash("That photo doesn't look like readable discharge instructions. Try a clearer, well-lit photo.", "error")
        return redirect(url_for("upload"))
    rows = []
    for m in plan.medications:
        detail = ", ".join(x for x in (m.dose, m.route, m.instructions) if x)
        for t in m.times or [""]:
            rows.append({"kind": "medication", "title": m.name, "details": detail, "time": t, "quote": m.source_quote})
    for e in plan.exercises:
        for t in e.times or [""]:
            rows.append({"kind": "exercise", "title": e.name, "details": e.details, "time": t, "quote": e.source_quote})
    for o in plan.other:
        for t in o.times or [""]:
            rows.append({"kind": "other", "title": o.title, "details": o.details, "time": t, "quote": o.source_quote})
    return render_template("upload_review.html", rows=rows, warnings=plan.warnings, full_text=plan.full_text)


@app.post("/patient/upload/confirm")
@login_required("patient")
def upload_confirm():
    p = security.current_patient()
    f = request.form
    kinds, titles, details, times = f.getlist("kind"), f.getlist("title"), f.getlist("details"), f.getlist("time")
    keep = set(f.getlist("keep"))
    db = get_db()
    cur = db.execute("INSERT INTO care_plans (patient_id, source, raw_text_enc, status, created_at) "
                     "VALUES (?, 'ocr', ?, 'pending_review', ?)", (p["id"], enc(f.get("full_text", "")), now_iso()))
    plan_id, added = cur.lastrowid, 0
    for i, (k, t, d, tm) in enumerate(zip(kinds, titles, details, times)):
        if str(i) not in keep or not t.strip():
            continue
        try:
            tm = datetime.strptime(tm, "%H:%M").strftime("%H:%M")
        except ValueError:
            tm = "09:00"
        db.execute("INSERT INTO care_items (patient_id, care_plan_id, kind, title_enc, details_enc, time_of_day) "
                   "VALUES (?, ?, ?, ?, ?, ?)",
                   (p["id"], plan_id, k if k in ("medication", "exercise", "other") else "other",
                    enc(t.strip()[:200]), enc(d.strip()[:500]), tm))
        added += 1
    db.commit()
    audit("care_plan_create", f"CarePlan/{plan_id}")
    flash(f"Added {added} items to your daily checklist. Your care team will review them.", "success")
    return redirect(url_for("patient_home"))


# ---------------------------------------------------------------------------
# Patient: journal with risk screening
# ---------------------------------------------------------------------------

def screen_and_escalate(patient_id: int, text: str, source: str, source_id: int):
    level, screen, status = ai.screen_text(text)
    summary = screen.get("summary") or f"Risk level {level} detected in {source}."
    escalate(patient_id, source, source_id, level, summary, request.host_url)
    return level, screen, status


@app.route("/patient/journal", methods=["GET", "POST"])
@login_required("patient")
def journal():
    p = security.current_patient()
    db = get_db()
    if request.method == "POST":
        text = request.form.get("text", "").strip()[:5000]
        mood = request.form.get("mood", type=int)
        if not text:
            flash("Write a few words before saving.", "error")
            return redirect(url_for("journal"))
        cur = db.execute("INSERT INTO journal_entries (patient_id, created_at, text_enc, mood) VALUES (?, ?, ?, ?)",
                         (p["id"], now_iso(), enc(text), mood if mood in MOODS else None))
        entry_id = cur.lastrowid
        db.commit()
        level, screen, status = screen_and_escalate(p["id"], text, "journal", entry_id)
        db.execute("UPDATE journal_entries SET risk_level = ?, screen_enc = ?, emotions_enc = ?, screen_status = ? "
                   "WHERE id = ?", (level, enc(screen), enc(screen.get("emotions") or []), status, entry_id))
        db.commit()
        audit("journal_create", f"JournalEntry/{entry_id}")
        if RISK_ORDER.index(level) >= RISK_ORDER.index("high"):
            return redirect(url_for("support", urgent=1))
        if level in ("low", "moderate"):
            flash("Thank you for sharing how you're feeling. You don't have to go through this alone. "
                  "Support is always available below, and your care team can see this entry.", "support")
        else:
            flash("Journal entry saved.", "success")
        return redirect(url_for("journal"))
    rows = db.execute("SELECT * FROM journal_entries WHERE patient_id = ? ORDER BY created_at DESC LIMIT 50",
                      (p["id"],)).fetchall()
    entries = [{"created_at": r["created_at"], "text": dec(r["text_enc"]), "mood": r["mood"],
                "emotions": dec_json(r["emotions_enc"]) or []} for r in rows]
    audit("view_journal", f"Patient/{p['id']}")
    return render_template("journal.html", entries=entries)


@app.get("/support")
@login_required()
def support():
    return render_template("support.html", urgent=request.args.get("urgent") == "1")


# ---------------------------------------------------------------------------
# Patient: fenced companion chatbot
# ---------------------------------------------------------------------------

def plan_text(patient_id: int) -> str:
    lines = []
    for it in plan_items(patient_id):
        tag = " (awaiting provider review)" if it["pending"] else ""
        lines.append(f"- {clock(it['time'])}: [{it['kind']}] {it['title']}"
                     f"{' - ' + it['details'] if it['details'] else ''}{tag}")
    return "\n".join(lines)


@app.get("/patient/chat")
@login_required("patient")
def chat():
    p = security.current_patient()
    rows = get_db().execute("SELECT * FROM chat_messages WHERE patient_id = ? ORDER BY id DESC LIMIT 40",
                            (p["id"],)).fetchall()
    messages = [{"role": r["role"], "content": dec(r["content_enc"])} for r in reversed(rows)]
    audit("view_chat", f"Patient/{p['id']}")
    return render_template("chat.html", messages=messages)


def sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.post("/patient/chat/send")
@login_required("patient")
def chat_send():
    p = security.current_patient()
    text = (request.get_json(silent=True) or {}).get("message", "").strip()[:2000]
    if not text:
        return jsonify(error="Type a message first."), 400
    db = get_db()
    cur = db.execute("INSERT INTO chat_messages (patient_id, role, content_enc, created_at) VALUES (?, 'user', ?, ?)",
                     (p["id"], enc(text), now_iso()))
    msg_id = cur.lastrowid
    db.commit()
    audit("chat_message", f"Patient/{p['id']}")
    level, _, _ = screen_and_escalate(p["id"], text, "chat", msg_id)

    rows = db.execute("SELECT * FROM chat_messages WHERE patient_id = ? ORDER BY id DESC LIMIT 20", (p["id"],)).fetchall()
    history = [{"role": r["role"], "content": dec(r["content_enc"])} for r in reversed(rows)]
    while history and history[0]["role"] != "user":
        history.pop(0)
    patient_user = g.user
    provider = db.execute("SELECT name_enc FROM users WHERE id = ?", (p["provider_id"],)).fetchone()
    context = dict(patient_name=dec(patient_user["name_enc"]), condition=dec(p["condition_enc"]),
                   provider_name=dec(provider["name_enc"]), plan_text=plan_text(p["id"]))

    @stream_with_context
    def generate():
        if RISK_ORDER.index(level) >= RISK_ORDER.index("moderate"):
            yield sse("crisis", {"level": level})
        reply = ""
        try:
            for chunk in ai.chat_stream(messages=history, **context):
                reply += chunk
                yield sse("delta", chunk)
        except ai.AIUnavailable:
            yield sse("error", "The companion is unavailable right now. For urgent questions, call your care team.")
        if reply:
            get_db().execute("INSERT INTO chat_messages (patient_id, role, content_enc, created_at) "
                             "VALUES (?, 'assistant', ?, ?)", (p["id"], enc(reply), now_iso()))
            get_db().commit()
        yield sse("done", {})

    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Patient: record access (HB 300 requires electronic copies within 15 business days; we serve them instantly)
# ---------------------------------------------------------------------------

@app.get("/patient/records")
@login_required("patient")
def records():
    return render_template("records.html")


@app.get("/patient/records/fhir.json")
@login_required("patient")
def records_download():
    p = security.current_patient()
    audit("records_export", f"Patient/{p['id']}")
    return fhir_response(p["id"])


def fhir_response(patient_id: int):
    body = json.dumps(fhir.build_bundle(patient_id), indent=2)
    return Response(body, mimetype="application/fhir+json",
                    headers={"Content-Disposition": f"attachment; filename=carebridge-patient-{patient_id}-fhir.json"})


# ---------------------------------------------------------------------------
# Provider dashboard
# ---------------------------------------------------------------------------

def require_training():
    if not security.training_current(g.user["id"]):
        audit("phi_blocked_training", request.path, outcome="denied")
        flash("Your HIPAA / Texas HB 300 privacy training is missing or expired. "
              "Record a current training to access patient information.", "error")
        return redirect(url_for("training"))
    return None


@app.get("/provider")
@login_required("provider")
def provider_home():
    blocked = require_training()
    if blocked:
        return blocked
    db = get_db()
    rows = db.execute("SELECT p.*, u.name_enc FROM patients p JOIN users u ON u.id = p.user_id WHERE p.provider_id = ?",
                      (g.user["id"],)).fetchall()
    patients = []
    for r in rows:
        _, overall = fhir.adherence(r["id"])
        alerts = db.execute("SELECT level FROM alerts WHERE patient_id = ? AND status = 'open'", (r["id"],)).fetchall()
        top = max((a["level"] for a in alerts), key=RISK_ORDER.index, default=None)
        mood = db.execute("SELECT mood FROM journal_entries WHERE patient_id = ? AND mood IS NOT NULL "
                          "ORDER BY created_at DESC LIMIT 1", (r["id"],)).fetchone()
        pending = db.execute("SELECT COUNT(*) FROM care_plans WHERE patient_id = ? AND status = 'pending_review'",
                             (r["id"],)).fetchone()[0]
        patients.append({"id": r["id"], "name": dec(r["name_enc"]), "condition": dec(r["condition_enc"]),
                         "adherence": overall, "alert": top, "alert_count": len(alerts),
                         "mood": mood["mood"] if mood else None, "pending": pending})
    patients.sort(key=lambda x: (-(RISK_ORDER.index(x["alert"]) if x["alert"] else -1), x["adherence"] or 0))
    audit("view_panel", "provider_panel")
    return render_template("provider_home.html", patients=patients)


def mood_points(patient_id: int):
    rows = get_db().execute(
        "SELECT created_at, mood FROM journal_entries WHERE patient_id = ? AND mood IS NOT NULL "
        "AND created_at >= ? ORDER BY created_at", (patient_id, (datetime.now(timezone.utc) - timedelta(days=14)).isoformat())
    ).fetchall()
    if not rows:
        return []
    n = len(rows)
    return [{"x": 20 + (i * 560 / max(n - 1, 1)), "y": 130 - (r["mood"] - 1) * 28, "mood": r["mood"],
             "label": localtime(r["created_at"])} for i, r in enumerate(rows)]


@app.get("/provider/patient/<int:patient_id>")
@login_required("provider")
def provider_patient(patient_id):
    blocked = require_training()
    if blocked:
        return blocked
    p = security.provider_patient_or_404(patient_id)
    db = get_db()
    patient_user = db.execute("SELECT * FROM users WHERE id = ?", (p["user_id"],)).fetchone()
    per_item, overall = fhir.adherence(patient_id)
    for row in per_item:
        row["kind"] = row["item"]["kind"]
        row["time"] = row["item"]["time_of_day"]
        row["details"] = dec(row["item"]["details_enc"]) or ""
    alerts = [{"id": a["id"], "level": a["level"], "source": a["source"], "status": a["status"],
               "summary": dec(a["summary_enc"]), "created_at": a["created_at"],
               "delivery": dec_json(a["delivery_enc"]) or []}
              for a in db.execute("SELECT * FROM alerts WHERE patient_id = ? ORDER BY status = 'resolved', created_at DESC "
                                  "LIMIT 30", (patient_id,))]
    journals = []
    for j in db.execute("SELECT * FROM journal_entries WHERE patient_id = ? ORDER BY created_at DESC LIMIT 20", (patient_id,)):
        flagged = j["risk_level"] not in (None, "none")
        screen = dec_json(j["screen_enc"]) or {}
        journals.append({"created_at": j["created_at"], "mood": j["mood"], "risk": j["risk_level"] or "pending",
                         "emotions": dec_json(j["emotions_enc"]) or [], "summary": screen.get("summary", ""),
                         "status": j["screen_status"], "flagged": flagged,
                         # Minimum necessary: full journal text is shown to the provider only for flagged entries.
                         "text": dec(j["text_enc"]) if flagged else None,
                         "evidence": [v["evidence"] for k, v in screen.items()
                                      if isinstance(v, dict) and v.get("answer") in ("yes", "unclear") and v.get("evidence")]})
    plans = [{"id": c["id"], "status": c["status"], "created_at": c["created_at"], "source": c["source"],
              "items": [{"title": dec(i["title_enc"]), "details": dec(i["details_enc"]), "time": i["time_of_day"],
                         "kind": i["kind"]} for i in db.execute("SELECT * FROM care_items WHERE care_plan_id = ?", (c["id"],))]}
             for c in db.execute("SELECT * FROM care_plans WHERE patient_id = ? ORDER BY created_at DESC LIMIT 10", (patient_id,))]
    audit("view_patient", f"Patient/{patient_id}")
    return render_template("provider_patient.html", p=p, name=dec(patient_user["name_enc"]),
                           condition=dec(p["condition_enc"]), mrn=dec(p["mrn_enc"]), dob=dec(p["dob_enc"]),
                           per_item=per_item, overall=overall, alerts=alerts, journals=journals, plans=plans,
                           mood=mood_points(patient_id), ehr_configured=bool(os.environ.get("FHIR_SERVER_URL")),
                           webhook_configured=bool(os.environ.get("ESCALATION_WEBHOOK_URL")))


@app.post("/provider/alert/<int:alert_id>/<action>")
@login_required("provider")
def alert_action(alert_id, action):
    if action not in ("acknowledged", "resolved"):
        abort(400)
    db = get_db()
    a = db.execute("SELECT patient_id FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    if not a:
        abort(404)
    security.provider_patient_or_404(a["patient_id"])
    db.execute("UPDATE alerts SET status = ?, handled_by = ?, handled_at = ? WHERE id = ?",
               (action, g.user["id"], now_iso(), alert_id))
    db.commit()
    audit(f"alert_{action}", f"Alert/{alert_id}")
    return redirect(url_for("provider_patient", patient_id=a["patient_id"]) + "#alerts")


@app.post("/provider/plan/<int:plan_id>/<action>")
@login_required("provider")
def plan_action(plan_id, action):
    if action not in ("approved", "rejected"):
        abort(400)
    db = get_db()
    c = db.execute("SELECT patient_id FROM care_plans WHERE id = ?", (plan_id,)).fetchone()
    if not c:
        abort(404)
    security.provider_patient_or_404(c["patient_id"])
    db.execute("UPDATE care_plans SET status = ?, reviewed_by = ?, reviewed_at = ? WHERE id = ?",
               (action, g.user["id"], now_iso(), plan_id))
    if action == "rejected":
        db.execute("UPDATE care_items SET active = 0 WHERE care_plan_id = ?", (plan_id,))
    db.commit()
    audit(f"care_plan_{action}", f"CarePlan/{plan_id}")
    flash(f"Care plan {action}.", "success")
    return redirect(url_for("provider_patient", patient_id=c["patient_id"]) + "#plan")


@app.post("/provider/patient/<int:patient_id>/item")
@login_required("provider")
def add_item(patient_id):
    security.provider_patient_or_404(patient_id)
    f = request.form
    title = f.get("title", "").strip()[:200]
    try:
        tm = datetime.strptime(f.get("time", ""), "%H:%M").strftime("%H:%M")
    except ValueError:
        tm = None
    if not title or not tm:
        flash("Item needs a name and a time.", "error")
        return redirect(url_for("provider_patient", patient_id=patient_id) + "#plan")
    db = get_db()
    cur = db.execute("INSERT INTO care_plans (patient_id, source, status, created_at, reviewed_by, reviewed_at) "
                     "VALUES (?, 'provider', 'approved', ?, ?, ?)", (patient_id, now_iso(), g.user["id"], now_iso()))
    kind = f.get("kind") if f.get("kind") in ("medication", "exercise", "other") else "other"
    db.execute("INSERT INTO care_items (patient_id, care_plan_id, kind, title_enc, details_enc, time_of_day) "
               "VALUES (?, ?, ?, ?, ?, ?)", (patient_id, cur.lastrowid, kind, enc(title), enc(f.get("details", "")[:500]), tm))
    db.commit()
    audit("care_item_add", f"Patient/{patient_id}")
    flash("Added to the patient's checklist.", "success")
    return redirect(url_for("provider_patient", patient_id=patient_id) + "#plan")


@app.post("/provider/item/<int:item_id>/remove")
@login_required("provider")
def remove_item(item_id):
    db = get_db()
    it = db.execute("SELECT patient_id FROM care_items WHERE id = ?", (item_id,)).fetchone()
    if not it:
        abort(404)
    security.provider_patient_or_404(it["patient_id"])
    db.execute("UPDATE care_items SET active = 0 WHERE id = ?", (item_id,))
    db.commit()
    audit("care_item_remove", f"CareItem/{item_id}")
    return redirect(url_for("provider_patient", patient_id=it["patient_id"]) + "#adherence")


@app.get("/provider/patient/<int:patient_id>/fhir.json")
@login_required("provider")
def provider_fhir(patient_id):
    blocked = require_training()
    if blocked:
        return blocked
    security.provider_patient_or_404(patient_id)
    audit("fhir_export", f"Patient/{patient_id}")
    return fhir_response(patient_id)


@app.post("/provider/patient/<int:patient_id>/send-ehr")
@login_required("provider")
def provider_send_ehr(patient_id):
    security.provider_patient_or_404(patient_id)
    result = fhir.send_to_ehr(patient_id)
    audit("fhir_send_ehr", f"Patient/{patient_id}", outcome=result["status"])
    messages = {"sent": ("Sent to the EHR.", "success"),
                "not_configured": ("No EHR endpoint is set. Add FHIR_SERVER_URL to .env.", "error")}
    msg, kind = messages.get(result["status"], (f"EHR transmission failed ({result['status']}).", "error"))
    flash(msg, kind)
    return redirect(url_for("provider_patient", patient_id=patient_id))


# ---------------------------------------------------------------------------
# Staff training log (Texas HB 300) and admin audit view
# ---------------------------------------------------------------------------

COURSES = ["HIPAA Privacy & Security", "Texas HB 300 Privacy (role-specific)", "Suicide risk response protocol"]


@app.route("/training", methods=["GET", "POST"])
@login_required("provider", "admin")
def training():
    db = get_db()
    if request.method == "POST":
        course = request.form.get("course")
        try:
            done = date.fromisoformat(request.form.get("completed_on", ""))
        except ValueError:
            done = None
        if course not in COURSES or not done or done > date.today():
            flash("Pick a course and a completion date that isn't in the future.", "error")
        else:
            expires = done.replace(year=done.year + 2) if not (done.month == 2 and done.day == 29) else done + timedelta(days=730)
            db.execute("INSERT INTO training_logs (user_id, course, completed_on, expires_on, recorded_by) VALUES (?, ?, ?, ?, ?)",
                       (g.user["id"], course, done.isoformat(), expires.isoformat(), g.user["id"]))
            db.commit()
            audit("training_recorded", course)
            flash("Training recorded.", "success")
        return redirect(url_for("training"))
    logs = db.execute("SELECT * FROM training_logs WHERE user_id = ? ORDER BY completed_on DESC", (g.user["id"],)).fetchall()
    return render_template("training.html", logs=logs, courses=COURSES, today=date.today().isoformat(),
                           current=security.training_current(g.user["id"]))


@app.get("/admin")
@login_required("admin")
def admin_home():
    db = get_db()
    staff = []
    for u in db.execute("SELECT * FROM users WHERE role IN ('provider', 'admin') ORDER BY role, email"):
        last = db.execute("SELECT * FROM training_logs WHERE user_id = ? ORDER BY expires_on DESC LIMIT 1", (u["id"],)).fetchone()
        staff.append({"name": dec(u["name_enc"]), "email": u["email"], "role": u["role"], "hired_on": u["hired_on"],
                      "last_course": last["course"] if last else None, "expires_on": last["expires_on"] if last else None,
                      "current": security.training_current(u["id"])})
    logs = db.execute("SELECT a.*, u.email FROM audit_log a LEFT JOIN users u ON u.id = a.user_id "
                      "ORDER BY a.id DESC LIMIT 200").fetchall()
    audit("view_audit_log", "audit_log")
    return render_template("admin.html", staff=staff, logs=logs)


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
def error_page(e):
    return render_template("error.html", code=e.code, message=e.description), e.code


@app.cli.command("seed")
def seed_command():
    """Load demo data: flask --app app seed"""
    from carebridge.seed import seed
    seed(generate_password_hash)
    print("Demo data loaded. Sign-in details are in carebridge/seed.py.")


if __name__ == "__main__":
    reminders.start_scheduler()
    app.run(host="127.0.0.1", port=5050, debug=os.environ.get("FLASK_DEBUG") == "1", threaded=True)
