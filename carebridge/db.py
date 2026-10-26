"""SQLite storage with field-level encryption for PHI."""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet
from flask import g

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("CAREBRIDGE_DB", BASE_DIR / "instance" / "carebridge.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('patient', 'provider', 'admin')),
    name_enc TEXT NOT NULL,
    emergency_contact_enc TEXT,
    hired_on TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS patients (
    id INTEGER PRIMARY KEY,
    user_id INTEGER UNIQUE NOT NULL REFERENCES users(id),
    provider_id INTEGER NOT NULL REFERENCES users(id),
    dob_enc TEXT,
    mrn_enc TEXT,
    condition_enc TEXT,
    discharged_on TEXT
);
CREATE TABLE IF NOT EXISTS care_plans (
    id INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    source TEXT NOT NULL,
    raw_text_enc TEXT,
    status TEXT NOT NULL DEFAULT 'pending_review' CHECK (status IN ('pending_review', 'approved', 'rejected')),
    created_at TEXT NOT NULL,
    reviewed_by INTEGER REFERENCES users(id),
    reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS care_items (
    id INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    care_plan_id INTEGER REFERENCES care_plans(id),
    kind TEXT NOT NULL CHECK (kind IN ('medication', 'exercise', 'other')),
    title_enc TEXT NOT NULL,
    details_enc TEXT,
    time_of_day TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS completions (
    id INTEGER PRIMARY KEY,
    care_item_id INTEGER NOT NULL REFERENCES care_items(id),
    day TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    UNIQUE (care_item_id, day)
);
CREATE TABLE IF NOT EXISTS journal_entries (
    id INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    created_at TEXT NOT NULL,
    text_enc TEXT NOT NULL,
    mood INTEGER CHECK (mood BETWEEN 1 AND 5),
    emotions_enc TEXT,
    risk_level TEXT,
    screen_enc TEXT,
    screen_status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    source TEXT NOT NULL,
    source_id INTEGER,
    level TEXT NOT NULL,
    summary_enc TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged', 'resolved')),
    created_at TEXT NOT NULL,
    handled_by INTEGER REFERENCES users(id),
    handled_at TEXT,
    delivery_enc TEXT
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content_enc TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    user_id INTEGER,
    role TEXT,
    action TEXT NOT NULL,
    target TEXT,
    ip TEXT,
    outcome TEXT NOT NULL DEFAULT 'ok'
);
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id INTEGER PRIMARY KEY,
    patient_id INTEGER NOT NULL REFERENCES patients(id),
    endpoint_hash TEXT UNIQUE NOT NULL,
    subscription_enc TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_prefs (
    patient_id INTEGER PRIMARY KEY REFERENCES patients(id),
    show_details INTEGER NOT NULL DEFAULT 0,
    followups INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS reminder_log (
    id INTEGER PRIMARY KEY,
    care_item_id INTEGER NOT NULL REFERENCES care_items(id),
    day TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('due', 'followup')),
    sent_at TEXT NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0,
    UNIQUE (care_item_id, day, kind)
);
CREATE TABLE IF NOT EXISTS training_logs (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    course TEXT NOT NULL,
    completed_on TEXT NOT NULL,
    expires_on TEXT NOT NULL,
    recorded_by INTEGER REFERENCES users(id)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_encryption_key() -> None:
    """Create a data-encryption key in .env on first run so PHI is never stored in plaintext."""
    if os.environ.get("DATA_ENCRYPTION_KEY"):
        return
    key = Fernet.generate_key().decode()
    env_path = BASE_DIR / ".env"
    with open(env_path, "a") as f:
        f.write(f"\nDATA_ENCRYPTION_KEY={key}\n")
    os.chmod(env_path, 0o600)
    os.environ["DATA_ENCRYPTION_KEY"] = key


_fernet = None


def fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(os.environ["DATA_ENCRYPTION_KEY"].encode())
    return _fernet


def enc(value):
    if value is None:
        return None
    if not isinstance(value, str):
        value = json.dumps(value)
    return fernet().encrypt(value.encode()).decode()


def dec(token):
    if token is None:
        return None
    return fernet().decrypt(token.encode()).decode()


def dec_json(token):
    raw = dec(token)
    return json.loads(raw) if raw else None


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
    os.chmod(DB_PATH, 0o600)
