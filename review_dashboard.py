from __future__ import annotations

import copy
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from accounting.audit_log import append_audit_event, resolve_reviewer_name
from accounting.decision_engine import apply_accounting_decision
from accounting.persistence import record_transaction
from accounting.rule_retrieval import apply_learning_rules
from allocation import rules as allocation_rules
from allocation.branch_resolver import apply_folder_allocation, apply_qbo_mapping
from allocation.rules import remember_correction
from ingestion.source_metadata import load_source_metadata_index, read_source_metadata, save_source_metadata_index

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
EXTRACTED_DIR = DATA_DIR / "extracted"
PROCESSED_DIR = DATA_DIR / "processed"
REVIEW_DECISIONS_FILE = PROCESSED_DIR / "review_decisions.json"
QBO_POSTED_FILE = PROCESSED_DIR / "qbo_posted.json"
DASHBOARD_INDEX_FILE = PROCESSED_DIR / "review_dashboard_index.json"
STATIC_DIR = BASE_DIR / "review_dashboard_static"
CONFIG_DIR = BASE_DIR / "config"
VENDOR_RULES_FILE = CONFIG_DIR / "vendor_rules.json"
VENDOR_BRANCH_RULES_FILE = CONFIG_DIR / "vendor_branch_rules.json"
ACCOUNT_RULES_FILE = CONFIG_DIR / "account_rules.json"
SUPPLIER_ACCOUNT_MAP_FILE = CONFIG_DIR / "supplier_account_map.json"

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
DOCUMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".webp", ".tif", ".tiff"}
LOCAL_DOCUMENT_ROOT_NAMES = ("review", "raw")
LOCAL_KEY_PREFIX = "__local__"
ISSUE_TYPES = {
    "OCR",
    "VAT",
    "Supplier",
    "Branch",
    "Account",
    "Posting Action",
    "Payment Method",
    "Duplicate",
    "Other",
}
EDITABLE_FIELDS = {
    "vendor": "supplier_name",
    "supplier_name": "supplier_name",
    "invoice_number": "invoice_number",
    "invoice_date": "invoice_date",
    "date": "invoice_date",
    "due_date": "due_date",
    "total_amount": "total_amount",
    "total": "total_amount",
    "tax_amount": "tax_amount",
    "vat": "tax_amount",
    "currency": "currency",
    "document_type": "document_type",
    "posting_action": "posting_action",
    "qbo_object": "qbo_object",
    "payment_status": "payment_status",
    "payment_method": "payment_method",
    "branch": "branch",
    "business_unit": "business_unit",
    "brand": "brand",
    "account_name": "account_name",
    "qbo_class_name": "qbo_class_name",
    "qbo_location_name": "qbo_location_name",
    "notes": "notes",
    "review_reason": "review_reason",
}
DECISION_STATUSES = {
    "approve": "approved",
    "approve_learn": "approved",
    "reject": "rejected",
    "duplicate": "duplicate",
    "needs_review": "needs_review",
    "manual_allocation": "manual_check",
}

FIELD_LABELS = {
    "vendor": "Vendor",
    "invoice_number": "Invoice number",
    "invoice_date": "Date",
    "total_amount": "Total",
    "tax_amount": "VAT",
    "branch": "Branch",
    "account_name": "Account",
    "posting_action": "Posting Action",
    "payment_method": "Payment Method",
    "payment_status": "Payment Status",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return copy.deepcopy(default)
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return copy.deepcopy(default)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(path)


def _normalise_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _normalise_file_name(value: Any) -> str:
    name = Path(str(value or "")).name.lower()
    for suffix in (".review_extraction.json", ".review_reason.json", ".extracted.json"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


def _local_key_for_path(path: Path) -> str:
    rel = str(path.resolve().relative_to(DATA_DIR.resolve()))
    return f"{LOCAL_KEY_PREFIX}{hashlib.sha1(rel.encode('utf-8')).hexdigest()}.json"


def _is_local_key(key: str) -> bool:
    return key.startswith(LOCAL_KEY_PREFIX) and key.endswith(".json")


def _skip_document_path(path: Path) -> bool:
    parts = {_normalise_text(part) for part in path.parts}
    return any(part.startswith("archive") for part in parts)


def _source_identity(path: Path) -> str:
    return _normalise_file_name(path.name)


def _metadata_source_hint(metadata: dict) -> str:
    return (
        metadata.get("original_relative_source_path")
        or metadata.get("source_path_hint")
        or metadata.get("folder_path_hint")
        or metadata.get("folder_path")
        or ""
    )


def _relative_data_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(DATA_DIR.resolve()))
    except ValueError:
        try:
            return str(path.resolve().relative_to(BASE_DIR.resolve()))
        except ValueError:
            return str(path.resolve())


def _local_source_folder_parts(path: Path) -> tuple[str, ...]:
    for root_name in LOCAL_DOCUMENT_ROOT_NAMES:
        root = DATA_DIR / root_name
        try:
            relative = path.resolve().relative_to(root.resolve())
        except ValueError:
            continue
        return relative.parts[:-1]
    return ()


def _has_source_folder_metadata(path: Path, sidecar_metadata: dict) -> bool:
    return bool(sidecar_metadata or _local_source_folder_parts(path))


def _dashboard_source_metadata_for_path(path: Path) -> dict:
    index = load_source_metadata_index()
    entries = index.get("dashboard_documents") or {}
    for key in (str(path.resolve()), _relative_data_path(path), path.name):
        entry = entries.get(key)
        if isinstance(entry, dict):
            return entry
    return {}


def _all_local_document_paths() -> list[Path]:
    paths: list[Path] = []
    for root_name in LOCAL_DOCUMENT_ROOT_NAMES:
        root = DATA_DIR / root_name
        if not root.exists():
            continue
        for path in sorted(root.rglob("*"), key=lambda p: str(p).lower()):
            if not path.is_file() or path.suffix.lower() not in DOCUMENT_EXTENSIONS:
                continue
            if _skip_document_path(path):
                continue
            safe = safe_data_path(str(path))
            if safe:
                paths.append(safe)
    return paths


def _infer_path_allocation(path_hint: str) -> dict:
    item = apply_folder_allocation({}, path_hint)
    item = apply_qbo_mapping(item)
    return {
        "branch": item.get("branch"),
        "brand": item.get("brand"),
        "business_unit": item.get("business_unit"),
        "qbo_class": item.get("qbo_class_name"),
        "qbo_location": item.get("qbo_location_name"),
        "confidence_reasons": item.get("confidence_reasons") or [],
    }


def _find_extracted_match_for_path(path: Path, extracted_items: dict[str, dict]) -> str | None:
    identity = _source_identity(path)
    for key, item in extracted_items.items():
        if identity in _extracted_match_identities(key, item):
            return key
    return None


def refresh_source_metadata_index(*, write_index: bool = True) -> dict:
    extracted_items = load_extracted_items()
    existing = load_source_metadata_index()
    file_entries = existing.get("files") or {}
    dashboard_entries: dict[str, dict] = {}
    branch_counts: dict[str, int] = {}
    matched = 0
    missing_extraction = 0
    local_paths = _all_local_document_paths()

    for path in local_paths:
        sidecar_metadata = read_source_metadata(path)
        path_hint = _metadata_source_hint(sidecar_metadata) or _relative_data_path(path)
        source_metadata_available = _has_source_folder_metadata(path, sidecar_metadata)
        allocation = _infer_path_allocation(path_hint)
        extracted_key = _find_extracted_match_for_path(path, extracted_items)
        if extracted_key:
            matched += 1
        else:
            missing_extraction += 1
        branch = allocation.get("branch") or "No branch"
        branch_counts[branch] = branch_counts.get(branch, 0) + 1
        entry = {
            "raw_file_path": str(path),
            "raw_relative_path": _relative_data_path(path),
            "raw_filename": path.name,
            "original_relative_source_path": path_hint,
            "source_path_hint": path_hint,
            "source_metadata_available": source_metadata_available,
            "branch": allocation.get("branch"),
            "brand": allocation.get("brand"),
            "business_unit": allocation.get("business_unit"),
            "qbo_class": allocation.get("qbo_class"),
            "qbo_location": allocation.get("qbo_location"),
            "extraction_json_match": extracted_key,
            "extraction_status": "Matched Extracted JSON" if extracted_key else "No Extracted JSON",
            "confidence_reasons": allocation.get("confidence_reasons") or [],
        }
        for key in (str(path), _relative_data_path(path), path.name):
            dashboard_entries[key] = entry

    payload = {
        **existing,
        "generated_at": utc_now(),
        "files": file_entries,
        "dashboard_documents": dashboard_entries,
        "summary": {
            "total_local_files": len(local_paths),
            "files_matched_to_extracted_json": matched,
            "files_missing_extraction": missing_extraction,
            "branch_counts": branch_counts,
        },
    }
    if write_index:
        save_source_metadata_index(payload)
    return payload


def _dedupe_paths(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for path_value in paths:
        if not path_value:
            continue
        safe = safe_data_path(path_value)
        key = str(safe or path_value)
        if key in seen:
            continue
        seen.add(key)
        result.append(str(safe or path_value))
    return result


def scan_local_documents() -> dict[str, dict]:
    documents: dict[str, dict] = {}
    for root_name in LOCAL_DOCUMENT_ROOT_NAMES:
        root = DATA_DIR / root_name
        if not root.exists():
            continue
        for path in sorted(root.rglob("*"), key=lambda p: str(p).lower()):
            if not path.is_file() or path.suffix.lower() not in DOCUMENT_EXTENSIONS:
                continue
            if _skip_document_path(path):
                continue
            safe = safe_data_path(str(path))
            if not safe:
                continue
            metadata = read_source_metadata(safe) or _dashboard_source_metadata_for_path(safe)
            identity = _source_identity(safe)
            record = documents.setdefault(identity, {
                "identity": identity,
                "name": safe.name,
                "paths": [],
                "source_metadata": [],
                "source_metadata_available": False,
                "source_path_hints": [],
                "primary_path": str(safe),
                "key": _local_key_for_path(safe),
            })
            record["paths"].append(str(safe))
            if metadata:
                record["source_metadata"].append(metadata)
                metadata_available = metadata.get("source_metadata_available")
                if metadata_available is None:
                    metadata_available = True
                if metadata_available:
                    record["source_metadata_available"] = True
                hint = _metadata_source_hint(metadata)
                if metadata_available and hint:
                    record["source_path_hints"].append(hint)
            elif root_name == "raw":
                try:
                    relative_hint = str(safe.relative_to(root))
                except ValueError:
                    relative_hint = ""
                if len(Path(relative_hint).parts) > 1:
                    record["source_metadata_available"] = True
                    record["source_path_hints"].append(relative_hint)
            if f"{os.sep}review{os.sep}" in str(safe):
                record["primary_path"] = str(safe)
                record["key"] = _local_key_for_path(safe)
    for record in documents.values():
        record["paths"] = _dedupe_paths(record["paths"])
        record["source_path_hints"] = sorted(set(record.get("source_path_hints") or []))
    return documents


def _extracted_match_identities(key: str, item: dict) -> set[str]:
    identities = {_normalise_file_name(key)}
    for source in _source_paths(item):
        identities.add(_normalise_file_name(source))
    primary = item.get("primary_source_file") or item.get("source_file")
    if primary:
        identities.add(_normalise_file_name(primary))
    return {identity for identity in identities if identity}


def _local_documents_by_extracted(items: dict[str, dict], local_docs: dict[str, dict]) -> dict[str, list[dict]]:
    matched: dict[str, list[dict]] = {}
    for key, item in items.items():
        for identity in _extracted_match_identities(key, item):
            if identity in local_docs:
                matched.setdefault(key, []).append(local_docs[identity])
    return matched


def _matched_local_identities(items: dict[str, dict], local_docs: dict[str, dict]) -> set[str]:
    identities: set[str] = set()
    local_keys = set(local_docs)
    for key, item in items.items():
        identities.update(_extracted_match_identities(key, item) & local_keys)
    return identities


def load_review_decisions() -> dict[str, str]:
    data = _read_json(REVIEW_DECISIONS_FILE, {})
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def save_review_decisions(decisions: dict[str, str]) -> None:
    _write_json(REVIEW_DECISIONS_FILE, decisions)


def _load_qbo_posted() -> dict:
    data = _read_json(QBO_POSTED_FILE, {})
    return data if isinstance(data, dict) else {}


def _posted_names() -> set[str]:
    posted = _load_qbo_posted().get("posted", {})
    return set(posted) if isinstance(posted, dict) else set()


def _load_extracted_item(key: str) -> dict:
    if Path(key).name != key or not key.endswith(".json"):
        raise FileNotFoundError("Invalid extracted JSON key")
    path = EXTRACTED_DIR / key
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Extracted item not found: {key}")
    data = _read_json(path, {})
    if not isinstance(data, dict):
        raise ValueError(f"Extracted item is not a JSON object: {key}")
    data.setdefault("extracted_file", str(path.resolve()))
    return data


def _save_extracted_item(key: str, item: dict) -> None:
    if Path(key).name != key or not key.endswith(".json"):
        raise ValueError("Invalid extracted JSON key")
    path = EXTRACTED_DIR / key
    item["extracted_file"] = str(path.resolve())
    _write_json(path, item)
    record_transaction(key, item, status="review_dashboard_saved")


def load_extracted_items() -> dict[str, dict]:
    if not EXTRACTED_DIR.exists():
        return {}
    items: dict[str, dict] = {}
    for path in sorted(EXTRACTED_DIR.glob("*.json"), key=lambda p: p.name.lower()):
        try:
            item = _load_extracted_item(path.name)
        except (OSError, ValueError):
            continue
        items[path.name] = item
    return items


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _confidence(item: dict) -> float:
    decision = item.get("accounting_decision") if isinstance(item.get("accounting_decision"), dict) else {}
    for value in (decision.get("confidence"), item.get("overall_confidence"), item.get("extraction_confidence")):
        parsed = _float(value)
        if parsed is not None:
            return round(parsed, 4)
    return 0.0


def _account_names(item: dict) -> list[str]:
    accounts = []
    for line in item.get("line_items") or []:
        account = (line.get("account_name") or "").strip() if isinstance(line, dict) else ""
        if account and account not in accounts:
            accounts.append(account)
    return accounts


def _posting_action(item: dict) -> str:
    decision = item.get("accounting_decision") if isinstance(item.get("accounting_decision"), dict) else {}
    return (
        item.get("posting_action")
        or decision.get("posting_action")
        or item.get("posting_type")
        or decision.get("posting_type")
        or item.get("qbo_posting_type")
        or "Unknown"
    )


def _qbo_object(item: dict) -> str:
    decision = item.get("accounting_decision") if isinstance(item.get("accounting_decision"), dict) else {}
    return item.get("qbo_object") or decision.get("qbo_object") or item.get("qbo_posting_type") or "None"


def _document_type(item: dict) -> str:
    decision = item.get("accounting_decision") if isinstance(item.get("accounting_decision"), dict) else {}
    return decision.get("document_type") or item.get("document_type") or item.get("document_category") or "Unknown"


def _source_paths(item: dict) -> list[str]:
    raw = item.get("all_source_files") or [item.get("source_file")]
    return [str(path) for path in raw if path]


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def safe_data_path(path_value: str | None) -> Path | None:
    if not path_value:
        return None
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if not _is_inside(resolved, DATA_DIR):
        return None
    return resolved


def _attachment_missing(item: dict) -> bool:
    paths = _source_paths(item)
    if not paths:
        return True
    return not any((safe_data_path(path) and safe_data_path(path).exists()) for path in paths)


def _folder_hint_for_item(item: dict) -> str | None:
    hints = item.get("source_path_hints") or []
    for hint in hints:
        if hint:
            return str(hint)
    for key in ("original_relative_source_path", "source_path_hint", "folder_path_hint", "source_folder_path"):
        if item.get(key):
            return str(item.get(key))
    for source in _source_paths(item):
        safe = safe_data_path(source)
        if safe:
            try:
                return str(safe.parent.relative_to(DATA_DIR.resolve()))
            except ValueError:
                return str(safe.parent)
    return None


def _with_local_sources(item: dict, local_records: list[dict] | None = None) -> dict:
    enriched = copy.deepcopy(item)
    paths = _source_paths(enriched)
    preferred_path = None
    source_path_hints = list(enriched.get("source_path_hints") or [])
    source_metadata = list(enriched.get("source_metadata") or [])
    source_metadata_available = bool(enriched.get("source_metadata_available"))
    for record in local_records or []:
        paths.extend(record.get("paths") or [])
        source_path_hints.extend(record.get("source_path_hints") or [])
        source_metadata.extend(record.get("source_metadata") or [])
        source_metadata_available = source_metadata_available or bool(record.get("source_metadata_available"))
        if record.get("primary_path"):
            preferred_path = record["primary_path"]
    paths = _dedupe_paths(paths)
    if paths:
        enriched["source_file"] = preferred_path or paths[0]
        enriched["primary_source_file"] = preferred_path or paths[0]
        enriched["all_source_files"] = paths
    if source_path_hints:
        enriched["source_path_hints"] = sorted(set(str(hint) for hint in source_path_hints if hint))
        enriched["source_path_hint"] = enriched["source_path_hints"][0]
        source_metadata_available = True
    enriched["source_metadata_available"] = source_metadata_available
    if source_metadata:
        enriched["source_metadata"] = source_metadata
    return enriched


def _infer_dashboard_fields(item: dict, local_records: list[dict] | None = None) -> dict:
    enriched = _with_local_sources(item, local_records)
    sources: list[str] = []
    folder_hint = _folder_hint_for_item(enriched)
    if folder_hint:
        enriched = apply_folder_allocation(enriched, folder_hint)
        sources.append("folder path")
    before_rules = copy.deepcopy(enriched)
    enriched = apply_learning_rules(enriched)
    if before_rules != enriched:
        sources.append("supplier/account memory")
    before_qbo = copy.deepcopy(enriched)
    enriched = apply_qbo_mapping(enriched)
    if before_qbo != enriched:
        sources.append("branch class/location mapping")
    before_decision = copy.deepcopy(enriched)
    enriched = apply_accounting_decision(enriched)
    if before_decision != enriched:
        sources.append("accounting decision engine")
    existing = list(enriched.get("dashboard_inference_sources") or [])
    enriched["dashboard_inference_sources"] = sorted(set(existing + sources))
    return enriched


def _virtual_item_from_local(record: dict) -> dict:
    path = record.get("primary_path")
    item = {
        "supplier_name": "",
        "invoice_number": "",
        "invoice_date": "",
        "due_date": "",
        "total_amount": None,
        "tax_amount": None,
        "currency": "AED",
        "document_type": "Needs Extraction",
        "document_category": "Needs Extraction",
        "posting_action": "Needs Extraction",
        "qbo_object": "None",
        "payment_status": "Unknown",
        "payment_method": "Unknown",
        "source_file": path,
        "all_source_files": record.get("paths") or ([path] if path else []),
        "source_path_hints": record.get("source_path_hints") or [],
        "source_metadata": record.get("source_metadata") or [],
        "source_metadata_available": bool(record.get("source_metadata_available")),
        "no_extracted_json": True,
        "needs_extraction": True,
        "needs_review": True,
        "review_reason": "No Extracted JSON: local file exists but extraction has not been run.",
        "overall_confidence": 0.0,
    }
    return _infer_dashboard_fields(item, [record])


def _local_record_by_key(key: str) -> dict | None:
    if not _is_local_key(key):
        return None
    for record in scan_local_documents().values():
        if record["key"] == key:
            return record
    return None


def _dashboard_item(key: str, item: dict, local_records: list[dict] | None = None) -> dict:
    if item.get("no_extracted_json"):
        return copy.deepcopy(item)
    enriched = _infer_dashboard_fields(item, local_records)
    enriched["dashboard_key"] = key
    return enriched


def _fingerprint(item: dict) -> str:
    return "|".join(
        str(part or "").strip().lower()
        for part in (
            item.get("supplier_name"),
            item.get("invoice_number"),
            item.get("invoice_date"),
            item.get("total_amount"),
            item.get("brand") or item.get("business_unit"),
            item.get("branch"),
        )
    )


def duplicate_candidates(items: dict[str, dict]) -> dict[str, list[str]]:
    fingerprints: dict[str, list[str]] = {}
    for key, item in items.items():
        fingerprint = _fingerprint(item)
        if fingerprint.strip("|"):
            fingerprints.setdefault(fingerprint, []).append(key)
    duplicates: dict[str, list[str]] = {}
    for keys in fingerprints.values():
        if len(keys) > 1:
            for key in keys:
                duplicates[key] = [candidate for candidate in keys if candidate != key]
    return duplicates


def _warning_bucket(level: str) -> str:
    return {
        "critical": "Critical",
        "review": "Needs Review",
        "info": "Informational",
    }.get(level, "Needs Review")


def _amount_mismatch(item: dict) -> bool:
    subtotal = _float(item.get("subtotal_amount"))
    tax = _float(item.get("tax_amount"))
    total = _float(item.get("total_amount"))
    if subtotal is None or tax is None or total is None:
        return False
    return abs((subtotal + tax) - total) > 0.05


def _supplier_seen(item: dict) -> bool:
    supplier = _normalise_text(item.get("supplier_name"))
    if not supplier:
        return False
    for path in (VENDOR_RULES_FILE, ACCOUNT_RULES_FILE, SUPPLIER_ACCOUNT_MAP_FILE):
        data = _read_json(path, {})
        vendors = data.get("vendors") or data.get("suppliers") or {}
        for name in vendors:
            if _normalise_text(name) == supplier:
                return True
    return False


def _missing_branch_reason(item: dict) -> str:
    if item.get("source_metadata_available") or item.get("source_path_hints"):
        return "Source folder metadata was available, but no branch mapping matched."
    return "Source folder metadata unavailable."


def warnings_for_item(item: dict, duplicate_keys: list[str] | None = None) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    confidence = _confidence(item)
    accounts = _account_names(item)
    doc_type = _document_type(item)
    posting_action = _posting_action(item)
    qbo_object = _qbo_object(item)
    review_reason = str(item.get("review_reason") or "")
    decision = item.get("accounting_decision") if isinstance(item.get("accounting_decision"), dict) else {}

    def add(code: str, label: str, level: str = "review", reason: str = "") -> None:
        warning = {"code": code, "label": label, "level": level, "bucket": _warning_bucket(level)}
        if reason:
            warning["reason"] = reason
        warnings.append(warning)

    if item.get("no_extracted_json"):
        add("no_extracted_json", "No Extracted JSON", "info")
        return warnings
    if _amount_mismatch(item):
        add("amount_mismatch", "Amount mismatch", "critical")
    if confidence < 0.90:
        add("low_confidence", f"Low confidence ({confidence:.2f})", "review")
    if not item.get("branch"):
        add("missing_branch", "Missing branch", "review", _missing_branch_reason(item))
    if not accounts:
        add("missing_account", "Missing account", "review")
    if not item.get("supplier_name"):
        add("missing_vendor", "Missing vendor", "critical")
    elif not _supplier_seen(item):
        add("supplier_unseen", "Supplier unseen before", "info")
    if item.get("total_amount") in (None, ""):
        add("missing_total", "Missing total", "critical")
    if not item.get("invoice_number"):
        add("missing_invoice_number", "Missing invoice number", "review")
    vat_treatment = str(item.get("vat_treatment") or decision.get("vat_treatment") or "").lower()
    if item.get("tax_amount") in (None, "") or "unknown" in vat_treatment or "missing" in vat_treatment:
        add("missing_vat", "Missing VAT", "review")
    if "vat" in review_reason.lower() and not item.get("supplier_trn"):
        add("missing_vat_trn", "Missing VAT/TRN", "review")
    if duplicate_keys:
        add("possible_duplicate", "Duplicate invoice", "critical")
    if item.get("capitalisation_required") or item.get("possible_capital_asset") or decision.get("capitalisation_required"):
        add("capital_asset", "Possible capital asset", "review")
    if item.get("allocation_type") == "shared" or doc_type == "Shared Expense":
        add("shared_expense", "Shared expense", "review")
    if item.get("allocation_type") == "head_office" or doc_type == "Head Office Expense":
        add("head_office_expense", "Head office expense", "review")
    if posting_action in {"Unknown", "Needs Human Review"} or qbo_object not in {"Bill", "Expense", "VendorCredit", "None"}:
        add("unsupported_type", "Unsupported posting type", "review")
    if doc_type == "Payment Proof" or posting_action == "Attach To Existing Bill":
        add("payment_proof_only", "Payment proof only", "info")
    if doc_type == "Supplier Statement":
        add("supplier_statement_only", "Supplier statement only", "info")
    if _attachment_missing(item):
        add("attachment_missing", "Attachment missing", "review")
    if not item.get("qbo_class_name") or not item.get("qbo_location_name"):
        add("qbo_mapping_missing", "QBO class/location missing", "review")
    if item.get("provider_validation_warnings") or item.get("provider_validation_errors"):
        add("ocr_warning", "OCR warning", "info")
    return warnings


def status_for_item(key: str, item: dict, decisions: dict[str, str], posted: set[str]) -> str:
    if item.get("no_extracted_json"):
        return "needs_extraction"
    if key in posted:
        return "posted"
    if decisions.get(key):
        return decisions[key]
    if item.get("manual_check"):
        return "manual_check"
    if item.get("needs_review"):
        return "needs_review"
    return "pending"


def queue_record(key: str, item: dict, status: str, warnings: list[dict[str, str]]) -> dict:
    buckets: dict[str, int] = {"critical": 0, "review": 0, "info": 0}
    for warning in warnings:
        level = warning.get("level") or "review"
        buckets[level] = buckets.get(level, 0) + 1
    return {
        "key": key,
        "vendor": item.get("supplier_name") or ("Needs Extraction" if item.get("no_extracted_json") else "Unknown"),
        "amount": item.get("total_amount"),
        "currency": item.get("currency") or "AED",
        "date": item.get("invoice_date"),
        "branch": item.get("branch"),
        "business_unit": item.get("business_unit") or item.get("brand"),
        "brand": item.get("brand"),
        "qbo_class_name": item.get("qbo_class_name"),
        "qbo_location_name": item.get("qbo_location_name"),
        "document_type": _document_type(item),
        "posting_action": _posting_action(item),
        "qbo_object": _qbo_object(item),
        "confidence": _confidence(item),
        "status": status,
        "extraction_status": "No Extracted JSON" if item.get("no_extracted_json") else "Matched Extracted JSON",
        "warning_count": len(warnings),
        "warning_buckets": buckets,
        "warnings": warnings,
    }


def _matches_filter(record: dict, filter_name: str) -> bool:
    codes = {warning["code"] for warning in record.get("warnings") or []}
    status = record.get("status")
    if filter_name in {"", "all"}:
        return True
    if filter_name == "ready":
        return status == "pending" and not codes
    if filter_name == "needs_review":
        return status == "needs_review"
    if filter_name == "needs_extraction":
        return status == "needs_extraction" or "no_extracted_json" in codes
    if filter_name == "manual":
        return status == "manual_check"
    if filter_name == "duplicates":
        return status == "duplicate" or "possible_duplicate" in codes
    if filter_name == "posted":
        return status == "posted"
    if filter_name == "rejected":
        return status == "rejected"
    if filter_name == "low_confidence":
        return "low_confidence" in codes
    if filter_name == "missing_branch":
        return "missing_branch" in codes
    if filter_name == "missing_account":
        return "missing_account" in codes
    if filter_name == "unsupported_type":
        return "unsupported_type" in codes
    return True


def build_queue_index(filter_name: str = "all", *, write_index: bool = True) -> dict:
    source_metadata = refresh_source_metadata_index(write_index=write_index)
    items = load_extracted_items()
    local_docs = scan_local_documents()
    local_matches = _local_documents_by_extracted(items, local_docs)
    matched_identities = _matched_local_identities(items, local_docs)
    local_file_count = sum(len(record.get("paths") or []) for record in local_docs.values())
    matched_file_count = sum(len(local_docs[identity].get("paths") or []) for identity in matched_identities if identity in local_docs)
    decisions = load_review_decisions()
    posted = _posted_names()
    duplicates = duplicate_candidates(items)
    records = []
    for key, item in items.items():
        dashboard_item = _dashboard_item(key, item, local_matches.get(key))
        warnings = warnings_for_item(dashboard_item, duplicates.get(key))
        status = status_for_item(key, dashboard_item, decisions, posted)
        record = queue_record(key, dashboard_item, status, warnings)
        if _matches_filter(record, filter_name):
            records.append(record)
    for identity, local_record in local_docs.items():
        if identity in matched_identities:
            continue
        key = local_record["key"]
        dashboard_item = _virtual_item_from_local(local_record)
        warnings = warnings_for_item(dashboard_item)
        status = status_for_item(key, dashboard_item, decisions, posted)
        record = queue_record(key, dashboard_item, status, warnings)
        if _matches_filter(record, filter_name):
            records.append(record)
    records.sort(key=lambda record: (record["status"] != "pending", record["date"] or "", record["vendor"].lower()))
    branch_counts: dict[str, int] = {}
    extraction_status_counts: dict[str, int] = {}
    for record in records:
        branch = record.get("branch") or "No branch"
        branch_counts[branch] = branch_counts.get(branch, 0) + 1
        extraction_status = record.get("extraction_status") or "Unknown"
        extraction_status_counts[extraction_status] = extraction_status_counts.get(extraction_status, 0) + 1
    payload = {
        "generated_at": utc_now(),
        "filter": filter_name,
        "total": len(records),
        "local_files_found": local_file_count,
        "unique_local_documents": len(local_docs),
        "files_matched_to_extracted_json": matched_file_count,
        "files_missing_extraction": max(0, local_file_count - matched_file_count),
        "source_metadata_summary": source_metadata.get("summary", {}),
        "branch_counts": branch_counts,
        "extraction_status_counts": extraction_status_counts,
        "items": records,
    }
    if write_index:
        _write_json(DASHBOARD_INDEX_FILE, payload)
    return payload


def metrics_summary() -> dict:
    index = build_queue_index("all", write_index=False)
    items = index["items"]
    total = len(items)
    counts: dict[str, int] = {}
    warning_counts: dict[str, int] = {}
    confidence_total = 0.0
    corrected = 0
    for record in items:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
        confidence_total += float(record.get("confidence") or 0)
        if record["status"] == "approved" and record.get("warning_count"):
            corrected += 1
        for warning in record.get("warnings") or []:
            warning_counts[warning["label"]] = warning_counts.get(warning["label"], 0) + 1
    return {
        "total_items": total,
        "local_files_found": index.get("local_files_found", 0),
        "unique_local_documents": index.get("unique_local_documents", 0),
        "files_matched_to_extracted_json": index.get("files_matched_to_extracted_json", 0),
        "files_missing_extraction": index.get("files_missing_extraction", 0),
        "branch_counts": index.get("branch_counts", {}),
        "extraction_status_counts": index.get("extraction_status_counts", {}),
        "approved": counts.get("approved", 0),
        "rejected": counts.get("rejected", 0),
        "needs_review": counts.get("needs_review", 0),
        "manual_allocation": counts.get("manual_check", 0),
        "duplicates": counts.get("duplicate", 0),
        "posted": counts.get("posted", 0),
        "low_confidence": warning_counts.get("Low confidence", 0)
        or sum(1 for record in items if any(w["code"] == "low_confidence" for w in record.get("warnings") or [])),
        "average_confidence": round(confidence_total / total, 4) if total else 0.0,
        "correction_rate": round(corrected / counts.get("approved", 1), 4) if counts.get("approved") else 0.0,
        "top_warning_types": sorted(warning_counts.items(), key=lambda item: (-item[1], item[0]))[:10],
    }


def item_detail(key: str) -> dict:
    all_items = load_extracted_items()
    local_docs = scan_local_documents()
    if _is_local_key(key):
        local_record = _local_record_by_key(key)
        if not local_record:
            raise FileNotFoundError(f"Local document not found: {key}")
        item = _virtual_item_from_local(local_record)
        local_records = [local_record]
    else:
        item = _load_extracted_item(key)
        local_records = _local_documents_by_extracted({key: item}, local_docs).get(key, [])
        item = _dashboard_item(key, item, local_records)
    duplicates = duplicate_candidates(all_items).get(key, [])
    decisions = load_review_decisions()
    status = status_for_item(key, item, decisions, _posted_names())
    warnings = warnings_for_item(item, duplicates)
    sources = []
    for index, source in enumerate(_source_paths(item)):
        safe = safe_data_path(source)
        sources.append({
            "index": index,
            "name": Path(source).name,
            "path": source,
            "safe": bool(safe),
            "exists": bool(safe and safe.exists()),
            "document_url": f"/api/document/{key}?source={index}" if safe and safe.exists() else None,
            "mime_type": mimetypes.guess_type(source)[0] or "application/octet-stream",
        })
    return {
        "key": key,
        "status": status,
        "item": item,
        "editable": editable_payload(item),
        "field_confidences": field_confidences(item),
        "missing_reasons": missing_reasons(item),
        "inference_sources": item.get("dashboard_inference_sources") or [],
        "read_only": bool(item.get("no_extracted_json")),
        "warnings": warnings,
        "sources": sources,
        "possible_duplicates": duplicates,
        "issue_types": sorted(ISSUE_TYPES),
    }


def editable_payload(item: dict) -> dict:
    accounts = _account_names(item)
    decision = item.get("accounting_decision") if isinstance(item.get("accounting_decision"), dict) else {}
    return {
        "vendor": item.get("supplier_name"),
        "invoice_number": item.get("invoice_number"),
        "invoice_date": item.get("invoice_date"),
        "due_date": item.get("due_date"),
        "total_amount": item.get("total_amount"),
        "tax_amount": item.get("tax_amount"),
        "currency": item.get("currency") or "AED",
        "document_type": _document_type(item),
        "posting_action": _posting_action(item),
        "qbo_object": _qbo_object(item),
        "payment_status": item.get("payment_status") or decision.get("payment_status"),
        "payment_method": item.get("payment_method") or decision.get("payment_method"),
        "branch": item.get("branch"),
        "business_unit": item.get("business_unit"),
        "brand": item.get("brand"),
        "account_name": accounts[0] if accounts else "",
        "qbo_class_name": item.get("qbo_class_name"),
        "qbo_location_name": item.get("qbo_location_name"),
        "notes": item.get("notes"),
        "review_reason": item.get("review_reason"),
    }


def _field_value(item: dict, field: str) -> Any:
    if field == "vendor":
        return item.get("supplier_name")
    if field == "account_name":
        accounts = _account_names(item)
        return accounts[0] if accounts else ""
    return editable_payload(item).get(field)


def _field_numeric_confidence(item: dict, field: str) -> float | None:
    provider = item.get("provider_field_confidences") if isinstance(item.get("provider_field_confidences"), dict) else {}
    direct_map = {
        "vendor": ("vendor_confidence", "supplier"),
        "invoice_number": (None, "invoice_number"),
        "invoice_date": (None, "invoice_date"),
        "total_amount": ("amount_confidence", "total"),
        "tax_amount": ("vat_confidence", "vat"),
        "branch": ("branch_confidence", None),
        "account_name": ("account_confidence", None),
        "posting_action": (None, None),
        "payment_method": (None, "payment_status"),
        "payment_status": (None, "payment_status"),
    }
    direct_key, provider_key = direct_map.get(field, (None, None))
    for value in (
        item.get(direct_key) if direct_key else None,
        provider.get(provider_key) if provider_key else None,
        (item.get("accounting_decision") or {}).get("confidence") if field in {"posting_action", "payment_method", "payment_status"} else None,
    ):
        parsed = _float(value)
        if parsed is not None:
            return parsed
    return None


def _confidence_label(value: Any, confidence: float | None, *, inferred: bool = False) -> str:
    if value in (None, "", [], "Unknown", "Needs Extraction"):
        return "Missing"
    if confidence is None:
        return "Medium" if inferred else "High"
    if confidence >= 0.90:
        return "High"
    if confidence >= 0.65:
        return "Medium"
    return "Low"


def _field_source(item: dict, field: str) -> str:
    if item.get("no_extracted_json"):
        return "No Extracted JSON"
    if field in {"branch", "qbo_class_name", "qbo_location_name"} and "folder path" in (item.get("dashboard_inference_sources") or []):
        return "Folder path / branch mapping"
    if field == "account_name" and any(rule.get("rule_type") == "account_rules" for rule in item.get("deterministic_rules_applied") or [] if isinstance(rule, dict)):
        return "Supplier account memory"
    if field in {"posting_action", "payment_method", "payment_status"}:
        return "Accounting decision engine"
    return "Extracted JSON"


def field_confidences(item: dict) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    inferred_sources = item.get("dashboard_inference_sources") or []
    for field, label in FIELD_LABELS.items():
        value = _field_value(item, field)
        confidence = _field_numeric_confidence(item, field)
        source = _field_source(item, field)
        result[field] = {
            "label": label,
            "confidence": _confidence_label(value, confidence, inferred=bool(inferred_sources)),
            "source": source,
        }
    return result


def missing_reasons(item: dict) -> dict[str, str]:
    reasons: dict[str, str] = {}
    if item.get("no_extracted_json"):
        return {
            "document": "No Extracted JSON: local file exists but extraction has not been run.",
            **{field: "No Extracted JSON: extraction has not been run for this local file." for field in FIELD_LABELS},
        }
    values = editable_payload(item)
    accounts = _account_names(item)
    for field, label in FIELD_LABELS.items():
        value = _field_value(item, field)
        if value not in (None, "", [], "Unknown", "Needs Extraction"):
            continue
        if field == "tax_amount":
            reasons[field] = "VAT not detected in extracted data."
        elif field == "account_name":
            reasons[field] = "No supplier/category rule found in local rule or memory files." if values.get("vendor") else "No supplier detected, so account memory could not be matched."
        elif field == "branch":
            reasons[field] = _missing_branch_reason(item)
        elif field == "posting_action":
            reasons[field] = "Decision engine could not infer a safe posting action from local data."
        elif field == "payment_method":
            reasons[field] = "Payment method was not detected and no supplier rule supplied it."
        elif field == "payment_status":
            reasons[field] = "Payment status was not detected and no supplier rule supplied it."
        else:
            reasons[field] = f"{label} not detected in extracted data."
    if not accounts and "account_name" not in reasons:
        reasons["account_name"] = "No supplier/category rule found in local rule or memory files."
    return reasons


def _set_editable_fields(item: dict, fields: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    for incoming_key, value in fields.items():
        target = EDITABLE_FIELDS.get(incoming_key)
        if not target:
            continue
        if target == "account_name":
            lines = item.setdefault("line_items", [])
            if not lines:
                lines.append({"description": "Reviewed expense", "amount": item.get("total_amount"), "account_name": value})
                changed.append("line_items.account_name")
            elif isinstance(lines[0], dict) and lines[0].get("account_name") != value:
                lines[0]["account_name"] = value
                changed.append("line_items.account_name")
            continue
        if item.get(target) != value:
            item[target] = value
            changed.append(target)

    if "posting_action" in changed:
        item["posting_type"] = item.get("posting_action")
    if "qbo_object" in changed:
        item["qbo_posting_type"] = item.get("qbo_object")
        item["qbo_posting_type_override"] = item.get("qbo_object")
    if changed:
        item["human_override_decision"] = True
        decision = item.setdefault("accounting_decision", {})
        if isinstance(decision, dict):
            for key in ("document_type", "posting_action", "qbo_object", "payment_status", "payment_method", "vat_treatment"):
                if item.get(key) is not None:
                    decision[key] = item.get(key)
            decision["posting_type"] = item.get("posting_action") or item.get("posting_type")
            decision["qbo_posting_type"] = item.get("qbo_object") or item.get("qbo_posting_type")
    return changed


def _review_snapshot(item: dict) -> dict:
    payload = editable_payload(item)
    payload.update({
        "source_file": item.get("source_file"),
        "all_source_files": item.get("all_source_files") or [],
        "extracted_file": item.get("extracted_file"),
        "decision_explanation": item.get("decision_explanation") or (item.get("accounting_decision") or {}).get("decision_explanation"),
    })
    return payload


def _append_history(item: dict, event: dict) -> None:
    history = item.setdefault("review_history", [])
    history.append(event)


def _structured_feedback(payload: dict[str, Any]) -> dict[str, str]:
    raw = payload.get("feedback") if isinstance(payload.get("feedback"), dict) else payload
    issue_type = str(raw.get("issue_type") or raw.get("issueType") or "Other").strip() or "Other"
    if issue_type not in ISSUE_TYPES:
        issue_type = "Other"
    return {
        "issue_type": issue_type,
        "correction": str(raw.get("correction") or "").strip(),
        "reason": str(raw.get("feedback_reason") or raw.get("reason") or "").strip(),
    }


def _field_changes(before: dict, after: dict, changed_fields: list[str] | None, feedback: dict[str, str], reviewer: str) -> list[dict]:
    before_snapshot = _review_snapshot(before)
    after_snapshot = _review_snapshot(after)
    fields = changed_fields or [
        field for field in sorted(set(before_snapshot) | set(after_snapshot)) if before_snapshot.get(field) != after_snapshot.get(field)
    ]
    timestamp = utc_now()
    document = after_snapshot.get("source_file") or after_snapshot.get("extracted_file") or before_snapshot.get("source_file")
    result = []
    snapshot_field_map = {
        "supplier_name": "vendor",
        "invoice_date": "invoice_date",
        "total_amount": "total_amount",
        "tax_amount": "tax_amount",
        "line_items.account_name": "account_name",
    }
    for field in fields:
        snapshot_field = snapshot_field_map.get(field, field)
        result.append({
            "document": document,
            "field_changed": field,
            "original_value": before_snapshot.get(snapshot_field),
            "corrected_value": after_snapshot.get(snapshot_field),
            "issue_type": feedback.get("issue_type") or "Other",
            "reason": feedback.get("reason") or "",
            "correction": feedback.get("correction") or "",
            "timestamp": timestamp,
            "user": reviewer,
        })
    return result


def _backup_file(path: Path) -> None:
    if not path.exists():
        return
    backup_dir = PROCESSED_DIR / "review_dashboard_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / f"{stamp}_{path.name}"
    backup.write_bytes(path.read_bytes())


def _backup_learning_files() -> None:
    for path in (
        allocation_rules.VENDOR_RULES_FILE,
        allocation_rules.VENDOR_BRANCH_RULES_FILE,
        allocation_rules.ACCOUNT_RULES_FILE,
    ):
        _backup_file(path)


def learning_preview(key: str, payload: dict[str, Any] | None = None) -> dict:
    if _is_local_key(key):
        raise ValueError("No Extracted JSON exists for this local file. Run extraction before learning.")
    payload = payload or {}
    item = _load_extracted_item(key)
    preview_item = copy.deepcopy(item)
    _set_editable_fields(preview_item, payload.get("fields") or {})
    supplier = (preview_item.get("supplier_name") or "").strip()
    accounts = _account_names(preview_item)
    changes = []
    if supplier:
        changes.append({
            "file": str(allocation_rules.VENDOR_RULES_FILE),
            "rule": "supplier posting/payment memory",
            "vendor": supplier,
            "values": {
                "brand": preview_item.get("brand") or preview_item.get("business_unit"),
                "posting_action": preview_item.get("posting_action") or preview_item.get("posting_type"),
                "qbo_object": preview_item.get("qbo_object") or preview_item.get("qbo_posting_type"),
                "payment_status": preview_item.get("payment_status"),
                "payment_method": preview_item.get("payment_method"),
            },
        })
        if preview_item.get("branch"):
            changes.append({
                "file": str(allocation_rules.VENDOR_BRANCH_RULES_FILE),
                "rule": "supplier branch memory",
                "vendor": supplier,
                "values": {
                    "brand": preview_item.get("brand") or preview_item.get("business_unit"),
                    "branch": preview_item.get("branch"),
                    "allocation_type": preview_item.get("allocation_type"),
                },
            })
        if accounts:
            changes.append({
                "file": str(allocation_rules.ACCOUNT_RULES_FILE),
                "rule": "supplier account memory",
                "vendor": supplier,
                "values": {"primary_account": accounts[0], "accounts": sorted(set(accounts))},
            })
    return {
        "ok": True,
        "key": key,
        "requires_confirmation": True,
        "can_learn": bool(supplier and changes),
        "message": "Approve & Learn will update local rule files only after confirmation.",
        "changes": changes,
        "feedback": _structured_feedback(payload),
    }


def save_edits(key: str, fields: dict[str, Any], *, reviewer: str | None = None) -> dict:
    if _is_local_key(key):
        raise ValueError("This local file has No Extracted JSON. Run extraction before saving edits.")
    item = _load_extracted_item(key)
    before = copy.deepcopy(item)
    changed = _set_editable_fields(item, fields)
    reviewer_name, _defaulted = resolve_reviewer_name(reviewer or "local_user")
    feedback = _structured_feedback(fields)
    event = _action_event(
        key=key,
        action="save_edits",
        status=None,
        before=before,
        after=item,
        reviewer=reviewer_name,
        reason=str(fields.get("reason") or ""),
        changed_fields=changed,
        feedback=feedback,
    )
    _backup_file(EXTRACTED_DIR / key)
    _append_history(item, event)
    _save_extracted_item(key, item)
    append_audit_event(event)
    return {"ok": True, "key": key, "changed_fields": changed, "item": item_detail(key)}


def perform_action(key: str, action: str, payload: dict[str, Any] | None = None, *, reviewer: str | None = None) -> dict:
    payload = payload or {}
    if _is_local_key(key):
        raise ValueError("This local file has No Extracted JSON. Run extraction before review actions.")
    if action not in DECISION_STATUSES:
        raise ValueError(f"Unsupported dashboard action: {action}")
    if action == "approve_learn" and not payload.get("confirm_learning"):
        raise ValueError("Approve & Learn requires learning preview confirmation.")
    item = _load_extracted_item(key)
    before = copy.deepcopy(item)
    changed = _set_editable_fields(item, payload.get("fields") or {})
    status = DECISION_STATUSES[action]
    reason = str(payload.get("reason") or payload.get("review_reason") or "").strip()
    duplicate_of = str(payload.get("duplicate_of") or "").strip()
    if action == "duplicate" and not duplicate_of:
        possible = duplicate_candidates(load_extracted_items()).get(key) or []
        duplicate_of = possible[0] if possible else ""

    decisions = load_review_decisions()
    decisions[key] = status
    save_review_decisions(decisions)

    if action in {"reject", "needs_review", "manual_allocation", "duplicate"}:
        item["needs_review"] = True
        if action == "manual_allocation":
            item["manual_check"] = True
        if reason:
            item["review_reason"] = reason
    if action == "duplicate":
        item["duplicate_of"] = duplicate_of
        item["duplicate_status"] = "possible_duplicate"
    if action in {"approve", "approve_learn"}:
        item["needs_review"] = False
        item["manual_check"] = False
    if action == "approve_learn":
        _backup_learning_files()
        remember_correction(item)

    reviewer_name, _defaulted = resolve_reviewer_name(reviewer or "local_user")
    feedback = _structured_feedback(payload)
    event = _action_event(
        key=key,
        action=action,
        status=status,
        before=before,
        after=item,
        reviewer=reviewer_name,
        reason=reason,
        duplicate_of=duplicate_of,
        changed_fields=changed,
        feedback=feedback,
    )
    _backup_file(EXTRACTED_DIR / key)
    _append_history(item, event)
    _save_extracted_item(key, item)
    append_audit_event(event)
    return {"ok": True, "key": key, "status": status, "changed_fields": changed, "item": item_detail(key)}


def _action_event(
    *,
    key: str,
    action: str,
    status: str | None,
    before: dict,
    after: dict,
    reviewer: str | None,
    reason: str = "",
    duplicate_of: str = "",
    changed_fields: list[str] | None = None,
    feedback: dict[str, str] | None = None,
) -> dict:
    reviewer_name, _defaulted = resolve_reviewer_name(reviewer or "local_user")
    before_snapshot = _review_snapshot(before)
    after_snapshot = _review_snapshot(after)
    feedback = feedback or {"issue_type": "Other", "correction": "", "reason": ""}
    correction_fields = changed_fields or [
        field for field in sorted(set(before_snapshot) | set(after_snapshot)) if before_snapshot.get(field) != after_snapshot.get(field)
    ]
    field_changes = _field_changes(before, after, correction_fields, feedback, reviewer_name)
    return {
        "event_type": "review_dashboard_action",
        "timestamp": utc_now(),
        "action": action,
        "status": status,
        "reviewer_name": reviewer_name,
        "original_extracted_values": before_snapshot,
        "edited_values": after_snapshot,
        "decision_explanation": after_snapshot.get("decision_explanation") or reason,
        "correction_fields": correction_fields,
        "field_changes": field_changes,
        "structured_feedback": feedback,
        "source_file": after.get("source_file"),
        "extracted_json_path": after.get("extracted_file") or str((EXTRACTED_DIR / key).resolve()),
        "transaction_key": key,
        "reason": reason,
        "duplicate_of": duplicate_of,
    }


class ReviewDashboardHandler(SimpleHTTPRequestHandler):
    server_version = "CEReviewDashboard/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _text(self, message: str, status: int = 404) -> None:
        data = message.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def _guard_localhost(self) -> bool:
        host = self.headers.get("Host", "").split(":")[0]
        if host and host not in LOCAL_HOSTS:
            self._text("Review dashboard is localhost-only.", HTTPStatus.FORBIDDEN)
            return False
        return True

    def do_GET(self) -> None:
        if not self._guard_localhost():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in {"/", "/dashboard"}:
                self._serve_static("index.html")
            elif path.startswith("/static/"):
                self._serve_static(path.removeprefix("/static/"))
            elif path == "/api/queue":
                query = parse_qs(parsed.query)
                self._json(build_queue_index((query.get("filter") or ["all"])[0]))
            elif path == "/api/metrics":
                self._json(metrics_summary())
            elif path.startswith("/api/item/"):
                key = unquote(path.removeprefix("/api/item/"))
                self._json(item_detail(key))
            elif path.startswith("/api/document/"):
                self._serve_document(unquote(path.removeprefix("/api/document/")), parsed.query)
            else:
                self._text("Not found", HTTPStatus.NOT_FOUND)
        except FileNotFoundError as exc:
            self._json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
        except Exception as exc:  # pragma: no cover - defensive boundary for local server
            self._json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        if not self._guard_localhost():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        payload = self._body_json()
        try:
            if path.startswith("/api/item/") and path.endswith("/learn-preview"):
                key = unquote(path.removeprefix("/api/item/").removesuffix("/learn-preview").strip("/"))
                self._json(learning_preview(key, payload))
            elif path.startswith("/api/item/") and path.endswith("/save"):
                key = unquote(path.removeprefix("/api/item/").removesuffix("/save").strip("/"))
                self._json(save_edits(key, payload.get("fields") or payload, reviewer=payload.get("reviewer")))
            elif path.startswith("/api/item/") and path.endswith("/action"):
                key = unquote(path.removeprefix("/api/item/").removesuffix("/action").strip("/"))
                self._json(perform_action(key, payload.get("action"), payload, reviewer=payload.get("reviewer")))
            elif path.startswith("/api/open/"):
                key = unquote(path.removeprefix("/api/open/"))
                self._json(open_local_target(key, payload))
            else:
                self._text("Not found", HTTPStatus.NOT_FOUND)
        except FileNotFoundError as exc:
            self._json({"ok": False, "error": str(exc)}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover
            self._json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _serve_static(self, name: str) -> None:
        path = (STATIC_DIR / name).resolve()
        if not _is_inside(path, STATIC_DIR) or not path.exists() or not path.is_file():
            self._text("Static file not found", HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_document(self, key: str, query: str) -> None:
        if "/" in key or "\\" in key or ".." in key:
            self._text("Invalid document key", HTTPStatus.BAD_REQUEST)
            return
        if _is_local_key(key):
            local_record = _local_record_by_key(key)
            if not local_record:
                self._text("Local document not found", HTTPStatus.NOT_FOUND)
                return
            item = _virtual_item_from_local(local_record)
        else:
            item = item_detail(key)["item"]
        source_index = int((parse_qs(query).get("source") or ["0"])[0])
        sources = _source_paths(item)
        if source_index < 0 or source_index >= len(sources):
            self._text("Source file not found", HTTPStatus.NOT_FOUND)
            return
        source = safe_data_path(sources[source_index])
        if not source or not source.exists() or not source.is_file():
            self._text("Source file is missing or outside project data folders.", HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(str(source))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(source.stat().st_size))
        self.send_header("Content-Disposition", f'inline; filename="{source.name}"')
        self.end_headers()
        with source.open("rb") as handle:
            while chunk := handle.read(1024 * 256):
                self.wfile.write(chunk)


def open_local_target(key: str, payload: dict) -> dict:
    if _is_local_key(key):
        local_record = _local_record_by_key(key)
        if not local_record:
            raise FileNotFoundError("Local document not found")
        item = _virtual_item_from_local(local_record)
    else:
        item = item_detail(key)["item"]
    source_index = int(payload.get("source", 0))
    target = payload.get("target") or "file"
    sources = _source_paths(item)
    if source_index < 0 or source_index >= len(sources):
        raise FileNotFoundError("Source file not found")
    path = safe_data_path(sources[source_index])
    if not path or not path.exists():
        raise FileNotFoundError("Source file is missing or outside project data folders")
    open_path = path.parent if target == "folder" else path
    if os.name == "posix":
        subprocess.Popen(["open", str(open_path)])
    else:
        os.startfile(str(open_path))  # type: ignore[attr-defined]
    return {"ok": True, "opened": str(open_path)}


def create_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    if host not in LOCAL_HOSTS:
        raise ValueError("Review dashboard may only bind to localhost.")
    return ThreadingHTTPServer((host, port), ReviewDashboardHandler)


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = create_server(host, port)
    print(f"Local review dashboard: http://{host}:{server.server_port}")
    print("No live APIs or QuickBooks posting routes are exposed.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def start_in_thread(host: str = "127.0.0.1", port: int = 0) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = create_server(host, port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
