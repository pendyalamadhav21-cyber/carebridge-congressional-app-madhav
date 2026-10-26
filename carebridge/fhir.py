"""HL7 FHIR R4 export of a patient's home-recovery data, for download or transmission to an EHR."""

import os
import uuid
from datetime import date, timedelta

import requests

from .db import dec, dec_json, get_db, now_iso

UCUM = "http://unitsofmeasure.org"
OBS_CATEGORY = "http://terminology.hl7.org/CodeSystem/observation-category"
LOCAL = "urn:carebridge:codes"


def _ref(resource_type: str, local_id) -> str:
    return f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'carebridge/{resource_type}/{local_id}')}"


def _entry(resource: dict, local_id, transaction: bool) -> dict:
    full_url = _ref(resource["resourceType"], local_id)
    entry = {"fullUrl": full_url, "resource": resource}
    if transaction:
        entry["request"] = {"method": "POST", "url": resource["resourceType"]}
    return entry


def adherence(patient_id: int, days: int = 7):
    """Per-item and overall completion % over the last `days` days (today included)."""
    db = get_db()
    start = (date.today() - timedelta(days=days - 1)).isoformat()
    items = db.execute("SELECT * FROM care_items WHERE patient_id = ? AND active = 1", (patient_id,)).fetchall()
    per_item, done_total, due_total = [], 0, 0
    for item in items:
        done = db.execute(
            "SELECT COUNT(*) FROM completions WHERE care_item_id = ? AND day >= ?", (item["id"], start)
        ).fetchone()[0]
        per_item.append({"item": item, "title": dec(item["title_enc"]), "done": done, "due": days,
                         "pct": round(100 * done / days)})
        done_total += done
        due_total += days
    overall = round(100 * done_total / due_total) if due_total else None
    return per_item, overall


def build_bundle(patient_id: int, transaction: bool = False) -> dict:
    db = get_db()
    p = db.execute("SELECT p.*, u.name_enc, u.email FROM patients p JOIN users u ON u.id = p.user_id WHERE p.id = ?",
                   (patient_id,)).fetchone()
    prov = db.execute("SELECT * FROM users WHERE id = ?", (p["provider_id"],)).fetchone()
    patient_ref, prov_ref = _ref("Patient", patient_id), _ref("Practitioner", prov["id"])
    entries = []

    name = dec(p["name_enc"]).split(" ", 1)
    entries.append(_entry({
        "resourceType": "Patient",
        "identifier": [{"system": "urn:carebridge:mrn", "value": dec(p["mrn_enc"])}],
        "name": [{"given": [name[0]], "family": name[1] if len(name) > 1 else ""}],
        "birthDate": dec(p["dob_enc"]),
        "generalPractitioner": [{"reference": prov_ref}],
    }, patient_id, transaction))
    entries.append(_entry({"resourceType": "Practitioner", "name": [{"text": dec(prov["name_enc"])}]},
                          prov["id"], transaction))

    items = db.execute("SELECT * FROM care_items WHERE patient_id = ? AND active = 1", (patient_id,)).fetchall()
    activities = []
    for it in items:
        title, details = dec(it["title_enc"]), dec(it["details_enc"]) or ""
        activities.append({"detail": {
            "kind": "MedicationRequest" if it["kind"] == "medication" else "ServiceRequest",
            "code": {"text": title},
            "description": details,
            "status": "in-progress",
            "scheduledTiming": {"repeat": {"frequency": 1, "period": 1, "periodUnit": "d",
                                           "timeOfDay": [it["time_of_day"] + ":00"]}},
        }})
        if it["kind"] == "medication":
            entries.append(_entry({
                "resourceType": "MedicationStatement",
                "status": "active",
                "medicationCodeableConcept": {"text": title},
                "subject": {"reference": patient_ref},
                "dosage": [{"text": details, "timing": {"repeat": {"timeOfDay": [it["time_of_day"] + ":00"]}}}],
            }, f"med-{it['id']}", transaction))
    entries.append(_entry({
        "resourceType": "CarePlan",
        "status": "active",
        "intent": "plan",
        "title": "Post-discharge home recovery plan",
        "subject": {"reference": patient_ref},
        "author": {"reference": prov_ref},
        "activity": activities,
    }, f"careplan-{patient_id}", transaction))

    per_item, overall = adherence(patient_id)
    today = date.today().isoformat()
    period = {"start": (date.today() - timedelta(days=6)).isoformat(), "end": today}
    for row in per_item:
        entries.append(_entry({
            "resourceType": "Observation",
            "status": "final",
            "category": [{"coding": [{"system": OBS_CATEGORY, "code": "activity"}]}],
            "code": {"coding": [{"system": LOCAL, "code": "adherence-7d"}], "text": f"7-day adherence: {row['title']}"},
            "subject": {"reference": patient_ref},
            "effectivePeriod": period,
            "valueQuantity": {"value": row["pct"], "unit": "%", "system": UCUM, "code": "%"},
            "component": [{"code": {"text": "completed"}, "valueInteger": row["done"]},
                          {"code": {"text": "scheduled"}, "valueInteger": row["due"]}],
        }, f"adh-{row['item']['id']}-{today}", transaction))
    if overall is not None:
        entries.append(_entry({
            "resourceType": "Observation",
            "status": "final",
            "category": [{"coding": [{"system": OBS_CATEGORY, "code": "activity"}]}],
            "code": {"coding": [{"system": LOCAL, "code": "adherence-7d-overall"}], "text": "7-day overall care plan adherence"},
            "subject": {"reference": patient_ref},
            "effectivePeriod": period,
            "valueQuantity": {"value": overall, "unit": "%", "system": UCUM, "code": "%"},
        }, f"adh-overall-{patient_id}-{today}", transaction))

    journals = db.execute(
        "SELECT * FROM journal_entries WHERE patient_id = ? ORDER BY created_at DESC LIMIT 30", (patient_id,)
    ).fetchall()
    for j in journals:
        if j["mood"]:
            entries.append(_entry({
                "resourceType": "Observation",
                "status": "final",
                "category": [{"coding": [{"system": OBS_CATEGORY, "code": "survey"}]}],
                "code": {"coding": [{"system": LOCAL, "code": "mood-1-5"}], "text": "Self-reported mood (1 = very low, 5 = very good)"},
                "subject": {"reference": patient_ref},
                "effectiveDateTime": j["created_at"],
                "valueInteger": j["mood"],
                "note": [{"text": "Emotions: " + ", ".join(dec_json(j["emotions_enc"]) or [])}],
            }, f"mood-{j['id']}", transaction))
        screen = dec_json(j["screen_enc"])
        if screen and j["risk_level"]:
            entries.append(_entry({
                "resourceType": "RiskAssessment",
                "status": "preliminary",  # AI-assisted screen, pending clinician review
                "subject": {"reference": patient_ref},
                "occurrenceDateTime": j["created_at"],
                "method": {"text": "Columbia-Suicide Severity Rating Scale (screener), AI-assisted"
                           + (" - keyword fallback" if j["screen_status"] == "keyword_fallback" else "")},
                "prediction": [{"outcome": {"text": "Suicide risk"},
                                "qualitativeRisk": {"coding": [{"system": LOCAL, "code": j["risk_level"]}],
                                                    "text": j["risk_level"]}}],
                "note": [{"text": screen.get("summary", "")}],
            }, f"risk-{j['id']}", transaction))

    return {
        "resourceType": "Bundle",
        "type": "transaction" if transaction else "collection",
        "timestamp": now_iso(),
        "entry": entries,
    }


def send_to_ehr(patient_id: int) -> dict:
    """POST a transaction Bundle to the hospital FHIR endpoint configured in .env."""
    base = os.environ.get("FHIR_SERVER_URL")
    if not base:
        return {"status": "not_configured"}
    if not base.startswith("https://") and "localhost" not in base and "127.0.0.1" not in base:
        return {"status": "refused", "reason": "FHIR_SERVER_URL must use HTTPS"}
    headers = {"Content-Type": "application/fhir+json", "Accept": "application/fhir+json"}
    token = os.environ.get("FHIR_BEARER_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.post(base.rstrip("/"), json=build_bundle(patient_id, transaction=True), headers=headers, timeout=20)
        return {"status": "sent" if r.ok else f"http_{r.status_code}"}
    except requests.RequestException as e:
        return {"status": "failed", "reason": type(e).__name__}
