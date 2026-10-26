"""Zero-trust request handling: authentication, role checks, CSRF, audit logging, security headers."""

import hmac
import secrets
import time
from collections import defaultdict, deque
from datetime import date
from functools import wraps

from flask import abort, g, redirect, request, session, url_for

from .db import get_db, now_iso

IDLE_TIMEOUT_SECONDS = 15 * 60
LOGIN_ATTEMPTS = defaultdict(deque)
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 15 * 60


def audit(action: str, target: str = None, outcome: str = "ok", user=None) -> None:
    """Append-only access log (HIPAA 164.312(b) audit controls)."""
    u = user or g.get("user")
    db = get_db()
    db.execute(
        "INSERT INTO audit_log (ts, user_id, role, action, target, ip, outcome) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (now_iso(), u["id"] if u else None, u["role"] if u else None, action, target, request.remote_addr, outcome),
    )
    db.commit()


def login_rate_limited(email: str) -> bool:
    key = (request.remote_addr, email.lower())
    attempts = LOGIN_ATTEMPTS[key]
    cutoff = time.time() - LOGIN_WINDOW_SECONDS
    while attempts and attempts[0] < cutoff:
        attempts.popleft()
    return len(attempts) >= MAX_LOGIN_ATTEMPTS


def record_failed_login(email: str) -> None:
    LOGIN_ATTEMPTS[(request.remote_addr, email.lower())].append(time.time())


def clear_failed_logins(email: str) -> None:
    LOGIN_ATTEMPTS.pop((request.remote_addr, email.lower()), None)


def start_session(user) -> None:
    session.clear()
    session.permanent = False
    session["uid"] = user["id"]
    session["csrf"] = secrets.token_urlsafe(32)
    session["seen"] = time.time()


def load_user() -> None:
    """Runs before every request: re-verify the session against the database each time."""
    g.user = None
    uid = session.get("uid")
    if not uid:
        return
    if time.time() - session.get("seen", 0) > IDLE_TIMEOUT_SECONDS:
        session.clear()
        g.session_expired = True
        return
    session["seen"] = time.time()
    g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if g.user is None:
        session.clear()


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def check_csrf() -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
    if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
        abort(400, "Invalid or missing CSRF token. Reload the page and try again.")


def set_security_headers(resp):
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; worker-src 'self'; manifest-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
    if g.get("user"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def login_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("login", next=request.path))
            if roles and g.user["role"] not in roles:
                audit("access_denied", request.path, outcome="denied")
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def training_current(user_id: int) -> bool:
    """Texas HB 300 (Tex. Health & Safety Code 181.101): staff need current privacy training to touch PHI."""
    row = get_db().execute(
        "SELECT 1 FROM training_logs WHERE user_id = ? AND expires_on >= ? LIMIT 1",
        (user_id, date.today().isoformat()),
    ).fetchone()
    return row is not None


def current_patient():
    """The patient record for the logged-in patient user."""
    return get_db().execute("SELECT * FROM patients WHERE user_id = ?", (g.user["id"],)).fetchone()


def provider_patient_or_404(patient_id: int):
    """Providers may only open patients assigned to them; admins see none (no treatment relationship)."""
    row = get_db().execute(
        "SELECT * FROM patients WHERE id = ? AND provider_id = ?", (patient_id, g.user["id"])
    ).fetchone()
    if row is None:
        audit("access_denied", f"Patient/{patient_id}", outcome="denied")
        abort(404)
    return row
