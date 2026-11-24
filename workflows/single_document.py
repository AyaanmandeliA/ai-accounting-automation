from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from accounting.persistence import record_transaction
from accounting.rules import apply_accounting_rules
from allocation.branch_resolver import apply_folder_allocation, apply_qbo_mapping
from allocation.confidence import apply_review_gate
from document_processing.text_extractor import extract_text_preview
from qbo.dry_run_builders import (
    build_bill_dry_run,
    build_expense_dry_run,
    build_vendor_credit_dry_run,
)

BASE_DIR = Path(__file__).resolve().parents[1]
EXTRACTED_DIR = BASE_DIR / "data/extracted"


def _safe_slug(value: Any, fallback: str = "document") -> str:
    text = re.sub(r"[^A-Za-z0-9._ -]+", "_", str(value or "").strip())
    text = re.sub(r"\s+", " ", text).strip(" ._")
    return text[:80] or fallback


def _load_or_stub_extraction(path: Path) -> dict:
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not read extracted JSON: {path}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"Extracted JSON must contain an object: {path}")
        return data

    preview = extract_text_preview(path)
    reason = "Single-document local extraction requires review before posting."
    if preview.get("warnings"):
        reason += " " + " ".join(str(w) for w in preview["warnings"][:3])
    return {
        "supplier_name": None,
        "business_unit": None,
        "brand": None,
        "branch": None,
        "allocation_type": "unknown",
        "invoice_number": None,
        "invoice_date": None,
        "due_date": None,
        "currency": "AED",
        "total_amount": None,
        "tax_amount": None,
        "line_items": [],
        "notes": (preview.get("text") or "")[:4000],
        "document_type": "single_document_local_preview",
        "accounting_treatment": "supporting_only",
        "payment_method": "unknown",
        "qbo_posting_type": "None",
        "payment_confirmed": False,
        "extraction_method": "single_document_local_text",
        "text_preview": preview,
        "extraction_confidence": preview.get("quality_score") or 0.0,
        "vendor_confidence": 0.0,
        "amount_confidence": 0.0,
        "vat_confidence": 0.0,
        "account_confidence": 0.0,
        "confidence_reasons": [reason],
        "needs_review": True,
        "manual_check": True,
        "review_reason": reason,
    }


def _posting_preview(extracted: dict) -> dict:
    qbo_type = (extracted.get("qbo_posting_type") or "").strip()
    if qbo_type == "Bill":
        return build_bill_dry_run(extracted)
    if qbo_type == "Expense":
        return build_expense_dry_run(extracted)
    if qbo_type in {"SupplierCredit", "VendorCredit"}:
        return build_vendor_credit_dry_run(extracted)
    return {
        "qbo_posting_type": qbo_type or "None",
        "qbo_api_entity": "None",
        "blocked_from_live_posting": True,
        "requires_review": True,
        "validation_errors": [f"No posting preview for QBO type: {qbo_type or 'None'}"],
        "dry_run_payload": None,
    }


def build_single_document_preview(file_path: str | Path, *, save: bool = False) -> dict:
    path = Path(file_path).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Single-document input not found: {path}")

    extracted = _load_or_stub_extraction(path)
    extracted["source_file"] = str(path)
    extracted["all_source_files"] = [str(path)]
    extracted.setdefault("primary_source_file", path.name)
    extracted.setdefault("folder_path_hint", None)
    extracted = apply_folder_allocation(extracted, extracted.get("folder_path_hint"))
    extracted = apply_qbo_mapping(extracted)
    extracted = apply_accounting_rules(extracted)
    extracted = apply_review_gate(extracted)

    out_path = None
    if save:
        EXTRACTED_DIR.mkdir(parents=True, exist_ok=True)
        out_name = f"{path.name}.{_safe_slug(extracted.get('invoice_number') or 'single')}.extracted.json"
        out_path = EXTRACTED_DIR / out_name
        tmp = out_path.with_suffix(".tmp")
        extracted["extracted_file"] = str(out_path.resolve())
        tmp.write_text(json.dumps(extracted, indent=2, default=str))
        tmp.replace(out_path)
        record_transaction(out_name, extracted, status="single_document_preview")

    preview = {
        "workflow": "single_document",
        "source_file": str(path),
        "extracted_file": str(out_path) if out_path else None,
        "stages": [
            "Extraction",
            "Accounting Decision",
            "Review Gate",
            "Dry-run Posting Preview",
        ],
        "extraction": extracted,
        "accounting_decision": extracted.get("accounting_decision") or {},
        "review": {
            "needs_review": bool(extracted.get("needs_review")),
            "manual_check": bool(extracted.get("manual_check")),
            "review_reason": extracted.get("review_reason") or "",
        },
        "posting_preview": _posting_preview(extracted),
    }
    return preview
