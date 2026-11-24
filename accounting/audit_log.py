from __future__ import annotations

import getpass
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from accounting.persistence import record_audit_event, record_review_decision

BASE_DIR = Path(__file__).resolve().parents[1]
AUDIT_LOG_FILE = BASE_DIR / "data/processed/audit_log.jsonl"

SYSTEM_FIELDS = {
    "_review_corrected",
    "_review_decision_id",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_audit_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def resolve_reviewer_name(explicit: str | None = None) -> tuple[str, bool]:
    value = (explicit or os.environ.get("REVIEWER_NAME") or "").strip()
    if value:
        return value, False
    try:
        username = getpass.getuser().strip()
    except (OSError, RuntimeError):
        username = ""
    if username and username.lower() not in {"unknown", "root"}:
        return username, True
    return "unknown_reviewer", True


def append_audit_event(event: dict[str, Any], audit_path: Path | None = None) -> dict[str, Any]:
    path = audit_path or AUDIT_LOG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "event_id": event.get("event_id") or new_audit_id("audit"),
        "logged_at": event.get("logged_at") or utc_now_iso(),
        **event,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    record_audit_event(payload)
    return payload


def changed_fields(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    changes = []
    keys = (set(before) | set(after)) - SYSTEM_FIELDS
    for key in sorted(keys):
        previous = before.get(key)
        new = after.get(key)
        if previous != new:
            changes.append({
                "field": key,
                "previous_value": previous,
                "new_value": new,
            })
    return changes


def review_decision_label(raw_decision: str, changes: list[dict[str, Any]]) -> str:
    if raw_decision == "approved" and changes:
        return "edited"
    if raw_decision == "approved":
        return "approved"
    if raw_decision == "skipped":
        return "skipped"
    return "rejected"


def append_review_decision(
    *,
    reviewer_name: str,
    raw_decision: str,
    before: dict[str, Any],
    after: dict[str, Any],
    filename: str,
) -> dict[str, Any]:
    changes = changed_fields(before, after)
    decision_id = new_audit_id("review")
    accounting_decision = after.get("accounting_decision") or {}
    event = append_audit_event({
        "event_type": "review_decision",
        "reviewer_id": reviewer_name,
        "reviewer_name": reviewer_name,
        "reviewed_at": utc_now_iso(),
        "decision_id": decision_id,
        "decision": review_decision_label(raw_decision, changes),
        "raw_decision": raw_decision,
        "fields_changed": [change["field"] for change in changes],
        "field_changes": changes,
        "previous_values": {change["field"]: change["previous_value"] for change in changes},
        "new_values": {change["field"]: change["new_value"] for change in changes},
        "decision_explanation": (
            after.get("decision_explanation")
            or accounting_decision.get("decision_explanation")
            or after.get("review_reason")
            or ""
        ),
        "source_file_reference": after.get("source_file"),
        "all_source_files": after.get("all_source_files") or [],
        "extracted_transaction_id": after.get("extracted_file") or filename,
        "accounting_decision_snapshot": accounting_decision,
    })
    after["_review_decision_id"] = decision_id
    record_review_decision(event)
    return event


def append_qbo_posting_attempt(
    *,
    qbo_action: str,
    qbo_posting_type: str | None,
    extracted: dict[str, Any],
    success: bool,
    error_message: str | None = None,
    qbo_id: str | None = None,
    attachment_result: Any = None,
    duplicate_fingerprint: str | None = None,
    reviewer_decision_id: str | None = None,
) -> dict[str, Any]:
    return append_audit_event({
        "event_type": "qbo_posting_attempt",
        "attempted_at": utc_now_iso(),
        "qbo_action": qbo_action,
        "qbo_posting_type": qbo_posting_type,
        "success": bool(success),
        "failure": not bool(success),
        "error_message": error_message,
        "qbo_id": qbo_id,
        "attachment_result": attachment_result,
        "duplicate_fingerprint": duplicate_fingerprint,
        "reviewer_decision_id": reviewer_decision_id or extracted.get("_review_decision_id"),
        "source_file_reference": extracted.get("source_file"),
        "extracted_transaction_id": extracted.get("extracted_file"),
        "supplier_name": extracted.get("supplier_name"),
        "invoice_number": extracted.get("invoice_number"),
        "total_amount": extracted.get("total_amount"),
    })
