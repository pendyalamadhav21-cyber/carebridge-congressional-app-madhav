"""Graduated escalation for risk detected in journals or chat.

Level   | Patient sees                    | Provider dashboard | Webhook (SMS / email gateway)
none    | -                               | -                  | -
low     | supportive resources            | flag               | -
moderate| supportive resources + 988      | open alert         | normal priority
high    | crisis screen (988 / 911)       | open alert         | HIGH priority
imminent| crisis screen (988 / 911)       | open alert         | HIGH priority, repeated to backup contact

Webhook payloads carry no clinical text (minimum necessary): only a reference, level, and dashboard link.
"""

import hashlib
import hmac
import json
import os

import requests

from .db import dec, enc, get_db, now_iso

ALERT_LEVELS = ("moderate", "high", "imminent")


def _post_webhook(payload: dict) -> dict:
    url = os.environ.get("ESCALATION_WEBHOOK_URL")
    if not url:
        return {"status": "not_configured", "at": now_iso()}
    body = json.dumps(payload, sort_keys=True).encode()
    headers = {"Content-Type": "application/json"}
    secret = os.environ.get("ESCALATION_WEBHOOK_SECRET")
    if secret:
        headers["X-CareBridge-Signature"] = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    try:
        r = requests.post(url, data=body, headers=headers, timeout=10)
        return {"status": "sent" if r.ok else f"http_{r.status_code}", "at": now_iso()}
    except requests.RequestException as e:
        return {"status": "failed", "error": type(e).__name__, "at": now_iso()}


def escalate(patient_id: int, source: str, source_id: int, level: str, summary: str, base_url: str):
    """Create a provider alert and notify the provider's emergency contact. Returns the alert id or None."""
    if level not in ALERT_LEVELS:
        if level == "low":
            db = get_db()
            db.execute(
                "INSERT INTO alerts (patient_id, source, source_id, level, summary_enc, status, created_at) "
                "VALUES (?, ?, ?, 'low', ?, 'open', ?)",
                (patient_id, source, source_id, enc(summary), now_iso()),
            )
            db.commit()
        return None

    db = get_db()
    provider = db.execute(
        "SELECT u.id, u.emergency_contact_enc FROM patients p JOIN users u ON u.id = p.provider_id WHERE p.id = ?",
        (patient_id,),
    ).fetchone()
    cur = db.execute(
        "INSERT INTO alerts (patient_id, source, source_id, level, summary_enc, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'open', ?)",
        (patient_id, source, source_id, level, enc(summary), now_iso()),
    )
    alert_id = cur.lastrowid
    db.commit()

    contact = json.loads(dec(provider["emergency_contact_enc"]) or "{}") if provider else {}
    payload = {
        "event": "carebridge.risk_alert",
        "priority": "high" if level in ("high", "imminent") else "normal",
        "level": level,
        "alert_id": alert_id,
        "patient_ref": f"Patient/{patient_id}",
        "dashboard_url": f"{base_url}provider/patient/{patient_id}#alerts",
        "notify": {"sms": contact.get("phone"), "email": contact.get("email")},
        "created_at": now_iso(),
    }
    deliveries = [_post_webhook(payload)]
    if level == "imminent" and contact.get("backup_phone"):
        deliveries.append(_post_webhook({**payload, "notify": {"sms": contact["backup_phone"]}}))

    db.execute("UPDATE alerts SET delivery_enc = ? WHERE id = ?", (enc(deliveries), alert_id))
    db.commit()
    return alert_id
