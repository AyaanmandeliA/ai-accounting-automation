from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from accounting.rule_retrieval import apply_learning_rules

BASE_DIR = Path(__file__).resolve().parents[1]
RULES_FILE = BASE_DIR / "config/accounting_rules.json"

DOCUMENT_SUPPLIER_BILL = "Supplier Bill"
DOCUMENT_EXPENSE_RECEIPT = "Expense Receipt"
DOCUMENT_PAYMENT_PROOF = "Payment Proof"
DOCUMENT_REMITTANCE = "Remittance Advice"
DOCUMENT_STATEMENT = "Supplier Statement"
DOCUMENT_QUOTATION = "Quotation"
DOCUMENT_DELIVERY_NOTE = "Delivery Note"
DOCUMENT_CREDIT_NOTE = "Credit Note"
DOCUMENT_DEBIT_NOTE = "Debit Note"
DOCUMENT_ASSET_PURCHASE = "Asset Purchase"
DOCUMENT_SHARED_COST = "Shared Cost"
DOCUMENT_HEAD_OFFICE_COST = "Head Office Cost"
DOCUMENT_UNKNOWN = "Unknown"

POSTABLE_DOCUMENT_CATEGORIES = {DOCUMENT_SUPPLIER_BILL, DOCUMENT_HEAD_OFFICE_COST}
TRN_RE = re.compile(r"\b\d{15}\b")


def _load_rules() -> dict:
    if not RULES_FILE.exists():
        return {}
    try:
        return json.loads(RULES_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _append_reason(existing: str | None, reason: str) -> str:
    existing = (existing or "").strip()
    if not existing:
        return reason
    if reason in existing:
        return existing
    return f"{existing} | {reason}"


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _month_key(value: date | None) -> str | None:
    return value.strftime("%Y-%m") if value else None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text_blob(extracted: dict) -> str:
    pieces = [
        extracted.get("supplier_name"),
        extracted.get("document_type"),
        extracted.get("accounting_treatment"),
        extracted.get("notes"),
        extracted.get("review_reason"),
        extracted.get("folder_path_hint"),
    ]
    for line in extracted.get("line_items") or []:
        pieces.extend([line.get("description"), line.get("account_name")])
    return " ".join(str(piece or "") for piece in pieces).lower()


def _contains_any(text: str, terms: list[str]) -> bool:
    return any(term.lower() in text for term in terms)


def _classify_document(extracted: dict, rules: dict) -> str:
    allocation = (extracted.get("allocation_type") or "").lower()
    if allocation == "shared":
        return DOCUMENT_SHARED_COST
    if allocation == "head_office":
        return DOCUMENT_HEAD_OFFICE_COST

    text = _text_blob(extracted)
    raw_type = str(extracted.get("document_type") or "").strip().lower()
    treatment = str(extracted.get("accounting_treatment") or "").strip().lower()
    qbo_type = str(extracted.get("qbo_posting_type") or "").strip()

    keyword_map = rules.get("document_type_keywords", {})
    ordered = [
        (DOCUMENT_QUOTATION, keyword_map.get("quotation", [])),
        (DOCUMENT_STATEMENT, keyword_map.get("supplier_statement", [])),
        (DOCUMENT_REMITTANCE, keyword_map.get("remittance_advice", [])),
        (DOCUMENT_PAYMENT_PROOF, keyword_map.get("payment_proof", [])),
        (DOCUMENT_DELIVERY_NOTE, keyword_map.get("delivery_note", [])),
        (DOCUMENT_CREDIT_NOTE, keyword_map.get("credit_note", [])),
        (DOCUMENT_DEBIT_NOTE, keyword_map.get("debit_note", [])),
    ]
    for category, terms in ordered:
        if raw_type.replace("_", " ") in category.lower() or _contains_any(text, terms):
            return category

    if qbo_type == "Bill" or raw_type in {"invoice", "bill", "supplier bill"}:
        return DOCUMENT_SUPPLIER_BILL
    if treatment in {"direct_expense_paid", "cash_expense_paid", "card_expense_paid"}:
        return DOCUMENT_EXPENSE_RECEIPT
    if treatment == "supplier_credit":
        return DOCUMENT_CREDIT_NOTE
    if treatment == "supplier_payment_only":
        return DOCUMENT_PAYMENT_PROOF
    return DOCUMENT_UNKNOWN


def _detect_trn(extracted: dict) -> str | None:
    explicit = str(extracted.get("supplier_trn") or extracted.get("trn") or "").strip()
    if TRN_RE.fullmatch(explicit):
        return explicit
    match = TRN_RE.search(_text_blob(extracted))
    return match.group(0) if match else None


def _apply_vat_rules(enriched: dict, rules: dict) -> list[str]:
    warnings = []
    trn = _detect_trn(enriched)
    enriched["supplier_trn"] = trn
    total = _as_float(enriched.get("total_amount"))
    tax = _as_float(enriched.get("tax_amount"))
    vat_rate = None
    if total and tax is not None and total > tax and tax > 0:
        vat_rate = round(tax / (total - tax), 4)
    enriched["vat_rate"] = vat_rate

    expected = float(rules.get("vat", {}).get("standard_rate", 0.05))
    tolerance = float(rules.get("vat", {}).get("rate_tolerance", 0.002))
    category = enriched.get("document_category")
    if category not in {DOCUMENT_SUPPLIER_BILL, DOCUMENT_EXPENSE_RECEIPT, DOCUMENT_ASSET_PURCHASE}:
        enriched["non_tax_invoice"] = True
        enriched["vat_validation_status"] = "not_tax_invoice"
        return warnings

    enriched["non_tax_invoice"] = False
    if tax in (None, 0):
        warnings.append("Missing VAT amount or zero VAT.")
        enriched["vat_validation_status"] = "missing_or_zero_vat"
        enriched["input_vat_review_required"] = False
        return warnings

    if vat_rate is None or abs(vat_rate - expected) > tolerance:
        warnings.append(f"VAT rate is not close to {expected:.0%}.")
        enriched["vat_validation_status"] = "invalid_vat_rate"
        enriched["input_vat_review_required"] = True
        enriched["needs_review"] = True
        enriched["review_reason"] = _append_reason(enriched.get("review_reason"), "Invalid VAT rate.")
        return warnings

    enriched["vat_validation_status"] = "valid"
    enriched["input_vat_review_required"] = False
    if not trn:
        warnings.append("VAT is present but supplier TRN was not detected.")
        enriched["input_vat_review_required"] = True
        enriched["needs_review"] = True
        enriched["review_reason"] = _append_reason(
            enriched.get("review_reason"),
            "Input VAT claimed without detected supplier TRN.",
        )
    return warnings


def _apply_cutoff_rules(enriched: dict, rules: dict) -> list[str]:
    warnings = []
    invoice_date = _parse_date(enriched.get("invoice_date"))
    service_start = _parse_date(enriched.get("service_period_start"))
    service_end = _parse_date(enriched.get("service_period_end"))
    upload_date = _parse_date(enriched.get("upload_date") or enriched.get("source_upload_date"))
    accounting_month = enriched.get("accounting_month") or _month_key(invoice_date)
    enriched["accounting_month"] = accounting_month

    cutoff = {
        "invoice_date": invoice_date.isoformat() if invoice_date else None,
        "service_period_start": service_start.isoformat() if service_start else None,
        "service_period_end": service_end.isoformat() if service_end else None,
        "upload_date": upload_date.isoformat() if upload_date else None,
        "accounting_month": accounting_month,
        "prior_period_risk": False,
        "warnings": [],
    }

    if upload_date and invoice_date and _month_key(invoice_date) < _month_key(upload_date):
        cutoff["prior_period_risk"] = True
        warnings.append("Invoice date is in an earlier month than upload date.")
    if service_end and accounting_month and _month_key(service_end) < accounting_month:
        cutoff["prior_period_risk"] = True
        warnings.append("Service period appears to belong to a prior accounting month.")

    cutoff["warnings"] = warnings
    enriched["cutoff"] = cutoff
    if warnings:
        enriched["needs_review"] = True
        enriched["review_reason"] = _append_reason(enriched.get("review_reason"), "Possible prior-period cutoff issue.")
    return warnings


def _apply_capital_rules(enriched: dict, rules: dict) -> None:
    text = _text_blob(enriched)
    categories = rules.get("capital_asset_keywords", {})
    matched = []
    for category, terms in categories.items():
        if _contains_any(text, terms):
            matched.append(category)
    enriched["possible_capital_asset"] = bool(matched)
    enriched["capital_asset_category"] = matched[0] if matched else None
    if matched:
        enriched["document_category"] = DOCUMENT_ASSET_PURCHASE
        enriched["needs_review"] = True
        enriched["review_reason"] = _append_reason(enriched.get("review_reason"), "Possible capital asset.")


def _approval_level(amount: float | None, rules: dict) -> str:
    thresholds = rules.get("approval_thresholds", {})
    if amount is None:
        return "unknown"
    for level in ("small", "medium", "large"):
        limit = thresholds.get(level)
        if limit is not None and amount <= float(limit):
            return level
    return "critical"


def _apply_shared_rules(enriched: dict) -> None:
    allocation = (enriched.get("allocation_type") or "").lower()
    if allocation == "shared":
        enriched["shared_expense_type"] = "Shared Cost"
        enriched["needs_review"] = True
        enriched["review_reason"] = _append_reason(enriched.get("review_reason"), "Shared costs must be manually allocated.")
    elif allocation == "head_office":
        enriched["shared_expense_type"] = "Head Office Cost"
    elif allocation == "branch":
        enriched["shared_expense_type"] = "Branch Cost"
    else:
        enriched["shared_expense_type"] = "Unknown"


def _tax_tags(enriched: dict, rules: dict) -> list[str]:
    text = _text_blob(enriched)
    tags = []
    for tag, terms in rules.get("corporate_tax_tags", {}).items():
        if _contains_any(text, terms):
            tags.append(tag)
    if enriched.get("possible_capital_asset") and "capital_asset" not in tags:
        tags.append("capital_asset")
    return sorted(set(tags))


def _audit_trail(enriched: dict) -> dict:
    existing = enriched.get("audit_trail") if isinstance(enriched.get("audit_trail"), dict) else {}
    history = list(existing.get("processing_history") or [])
    if not history or history[-1].get("event") != "accounting_rules_applied":
        history.append({
            "event": "accounting_rules_applied",
            "schema": "accounting_rules_v1",
        })
    return {
        "original_document": enriched.get("source_file"),
        "all_source_files": enriched.get("all_source_files") or [],
        "extraction_result": enriched.get("extracted_file"),
        "review_decision": existing.get("review_decision"),
        "reviewer": existing.get("reviewer"),
        "corrections": existing.get("corrections") or [],
        "confidence": {
            "overall": enriched.get("overall_confidence"),
            "vendor": enriched.get("vendor_confidence"),
            "amount": enriched.get("amount_confidence"),
            "vat": enriched.get("vat_confidence"),
            "branch": enriched.get("branch_confidence"),
            "account": enriched.get("account_confidence"),
        },
        "qbo_ids": existing.get("qbo_ids") or {},
        "processing_history": history,
    }


def apply_accounting_rules(extracted: dict) -> dict:
    from accounting.decision_engine import apply_accounting_decision

    rules = _load_rules()
    enriched = apply_learning_rules(extracted)
    enriched["document_category"] = _classify_document(enriched, rules)
    _apply_capital_rules(enriched, rules)
    _apply_shared_rules(enriched)

    vat_warnings = _apply_vat_rules(enriched, rules)
    cutoff_warnings = _apply_cutoff_rules(enriched, rules)
    enriched["vat_warnings"] = vat_warnings
    enriched["cutoff_warnings"] = cutoff_warnings

    amount = _as_float(enriched.get("total_amount"))
    enriched["approval_level"] = _approval_level(amount, rules)
    enriched["approval_routing"] = {
        "level": enriched["approval_level"],
        "status": "framework_only",
        "approver": None,
    }
    enriched["corporate_tax_tags"] = _tax_tags(enriched, rules)
    enriched["supplier_statement_reconciliation"] = {
        "status": "not_started",
        "framework": "supplier_statement_vs_qbo_ledger",
        "statement_file": None,
        "qbo_ledger_reference": None,
        "differences": [],
    }
    enriched["bank_reconciliation"] = {
        "status": "not_started",
        "framework": "bill_vs_bank_transaction",
        "candidate_bank_transaction_ids": [],
        "matched_bank_transaction_id": None,
    }
    enriched["audit_trail"] = _audit_trail(enriched)

    if enriched["document_category"] not in POSTABLE_DOCUMENT_CATEGORIES:
        if enriched.get("qbo_posting_type") == "Bill":
            enriched["needs_review"] = True
            enriched["review_reason"] = _append_reason(
                enriched.get("review_reason"),
                f"Document category is {enriched['document_category']}, not a supplier bill.",
            )
    return apply_accounting_decision(enriched)
