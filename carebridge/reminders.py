"""Care-plan reminders over Web Push.

A background thread checks every 30 seconds for checklist items that are due and not yet done, and pushes a
notification to each of the patient's subscribed browsers: once at the scheduled time and once more 30 minutes
later if the item is still open. By default the notification text has no medication names (lock screens are
visible to others); patients can opt in to showing item details.
"""

import base64
import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush

from .db import BASE_DIR, connect, dec, now_iso

log = logging.getLogger("carebridge.reminders")

FOLLOWUP_MINUTES = 30
CHECK_INTERVAL_SECONDS = 30
DUE_WINDOW_MINUTES = 20  # don't send stale "due now" reminders after a restart

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def ensure_vapid_keys() -> None:
    """Create the server's Web Push (VAPID) key pair in .env on first run."""
    if os.environ.get("VAPID_PRIVATE_KEY") and os.environ.get("VAPID_PUBLIC_KEY"):
        return
    key = ec.generate_private_key(ec.SECP256R1())
    private = _b64url(key.private_numbers().private_value.to_bytes(32, "big"))
    public = _b64url(key.public_key().public_bytes(serialization.Encoding.X962,
                                                   serialization.PublicFormat.UncompressedPoint))
    with open(BASE_DIR / ".env", "a") as f:
        f.write(f"VAPID_PRIVATE_KEY={private}\nVAPID_PUBLIC_KEY={public}\n")
    os.environ["VAPID_PRIVATE_KEY"], os.environ["VAPID_PUBLIC_KEY"] = private, public


def endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()


def prefs(conn, patient_id: int) -> dict:
    row = conn.execute("SELECT * FROM notification_prefs WHERE patient_id = ?", (patient_id,)).fetchone()
    return {"show_details": bool(row["show_details"]) if row else False,
            "followups": bool(row["followups"]) if row else True}


def send_to_patient(conn, patient_id: int, payload: dict) -> int:
    """Push to every subscribed device. Removes subscriptions the push service says are gone. Returns sent count."""
    sent = 0
    claims_sub = os.environ.get("VAPID_CONTACT", "mailto:admin@example.com")
    for sub in conn.execute("SELECT * FROM push_subscriptions WHERE patient_id = ?", (patient_id,)).fetchall():
        try:
            webpush(json.loads(dec(sub["subscription_enc"])), data=json.dumps(payload),
                    vapid_private_key=os.environ["VAPID_PRIVATE_KEY"], vapid_claims={"sub": claims_sub},
                    ttl=FOLLOWUP_MINUTES * 60, timeout=10)
            sent += 1
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status in (404, 410):
                conn.execute("DELETE FROM push_subscriptions WHERE id = ?", (sub["id"],))
            else:
                log.warning("push failed (%s)", status)
        except Exception as e:  # network errors must not stop the scheduler
            log.warning("push failed: %s", type(e).__name__)
    conn.commit()
    return sent


def _message(item, kind: str, show_details: bool) -> dict:
    title = dec(item["title_enc"])
    details = dec(item["details_enc"]) or ""
    label = {"medication": "medication", "exercise": "exercise", "other": "task"}[item["kind"]]
    if show_details:
        body = f"{title}{' - ' + details if details else ''}"
    else:
        body = f"You have a scheduled {label}. Open CareBridge to see it."
    heading = "Reminder: still to do" if kind == "followup" else "Time for your care plan"
    return {"title": heading, "body": body, "tag": f"item-{item['id']}-{kind}", "url": "/patient"}


def run_once(now: datetime = None) -> int:
    """Send any reminders that are due. Returns the number of notifications sent."""
    now = now or datetime.now()
    today = now.date().isoformat()
    total = 0
    with connect() as conn:
        items = conn.execute(
            "SELECT ci.* FROM care_items ci WHERE ci.active = 1 "
            "AND EXISTS (SELECT 1 FROM push_subscriptions s WHERE s.patient_id = ci.patient_id) "
            "AND NOT EXISTS (SELECT 1 FROM completions c WHERE c.care_item_id = ci.id AND c.day = ?)", (today,)
        ).fetchall()
        for item in items:
            due = datetime.combine(now.date(), datetime.strptime(item["time_of_day"], "%H:%M").time())
            p = prefs(conn, item["patient_id"])
            kind = None
            if due <= now < due + timedelta(minutes=DUE_WINDOW_MINUTES):
                kind = "due"
            elif p["followups"] and due + timedelta(minutes=FOLLOWUP_MINUTES) <= now < due + timedelta(minutes=FOLLOWUP_MINUTES + DUE_WINDOW_MINUTES):
                kind = "followup"
            if not kind:
                continue
            claimed = conn.execute("INSERT OR IGNORE INTO reminder_log (care_item_id, day, kind, sent_at) VALUES (?, ?, ?, ?)",
                                   (item["id"], today, kind, now_iso())).rowcount
            conn.commit()
            if not claimed:
                continue
            sent = send_to_patient(conn, item["patient_id"], _message(item, kind, p["show_details"]))
            conn.execute("UPDATE reminder_log SET delivered = ? WHERE care_item_id = ? AND day = ? AND kind = ?",
                         (sent, item["id"], today, kind))
            conn.commit()
            total += sent
    return total


def start_scheduler() -> None:
    def loop():
        while True:
            try:
                run_once()
            except Exception:
                log.exception("reminder run failed")
            time.sleep(CHECK_INTERVAL_SECONDS)

    threading.Thread(target=loop, name="carebridge-reminders", daemon=True).start()
