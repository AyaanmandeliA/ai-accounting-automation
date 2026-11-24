from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from accounting.rules import (
    DOCUMENT_ASSET_PURCHASE,
    DOCUMENT_CREDIT_NOTE,
    DOCUMENT_DEBIT_NOTE as CATEGORY_DEBIT_NOTE,
    DOCUMENT_DELIVERY_NOTE,
    DOCUMENT_EXPENSE_RECEIPT,
    DOCUMENT_HEAD_OFFICE_COST,
    DOCUMENT_PAYMENT_PROOF,
    DOCUMENT_QUOTATION,
    DOCUMENT_REMITTANCE,
    DOCUMENT_SHARED_COST,
    DOCUMENT_STATEMENT,
    DOCUMENT_SUPPLIER_BILL,
    DOCUMENT_UNKNOWN,
    _as_float,
    _detect_trn,
    _text_blob,
)

BASE_DIR = Path(__file__).resolve().parents[1]
VENDOR_RULES_FILE = BASE_DIR / "config/vendor_rules.json"

DOC_SUPPLIER_INVOICE = "Supplier Invoice"
DOC_EXPENSE_RECEIPT = "Expense Receipt"
DOC_CREDIT_NOTE = "Supplier Credit Note"
DOC_DEBIT_NOTE = "Debit Note"
DOC_PAYMENT_PROOF = "Payment Proof"
DOC_REMITTANCE_ADVICE = "Remittance Advice"
DOC_SUPPLIER_STATEMENT = "Supplier Statement"
DOC_QUOTATION = "Quotation"
DOC_DELIVERY_NOTE = "Delivery Note"
DOC_ASSET_PURCHASE = "Asset Purchase"
DOC_HEAD_OFFICE_EXPENSE = "Head Office Expense"
DOC_SHARED_EXPENSE = "Shared Expense"
DOC_MERCHANT_SETTLEMENT = "Merchant Settlement"
DOC_UNKNOWN = "Unknown"

SUPPORTED_DOCUMENT_TYPES = {
    DOC_SUPPLIER_INVOICE,
    DOC_EXPENSE_RECEIPT,
    DOC_CREDIT_NOTE,
    DOC_DEBIT_NOTE,
    DOC_PAYMENT_PROOF,
    DOC_REMITTANCE_ADVICE,
    DOC_SUPPLIER_STATEMENT,
    DOC_QUOTATION,
    DOC_DELIVERY_NOTE,
    DOC_ASSET_PURCHASE,
    DOC_HEAD_OFFICE_EXPENSE,
    DOC_SHARED_EXPENSE,
    DOC_MERCHANT_SETTLEMENT,
    DOC_UNKNOWN,
}

PAID = "Paid"
UNPAID = "Unpaid"
PARTIALLY_PAID = "Partially Paid"
UNKNOWN_PAYMENT = "Unknown"

PAYMENT_CASH = "Cash"
PAYMENT_CREDIT_CARD = "Credit Card"
PAYMENT_DEBIT_CARD = "Debit Card"
PAYMENT_BANK_TRANSFER = "Bank Transfer"
PAYMENT_CHEQUE = "Cheque"
PAYMENT_UNKNOWN = "Unknown"

ACTION_CREATE_BILL = "Create Bill"
ACTION_CREATE_EXPENSE = "Create Expense"
ACTION_CREATE_VENDOR_CREDIT = "Create Vendor Credit"
ACTION_ATTACH_TO_EXISTING_BILL = "Attach To Existing Bill"
ACTION_IGNORE = "Ignore"
ACTION_RECONCILIATION_ONLY = "Reconciliation Only"
ACTION_NEEDS_HUMAN_REVIEW = "Needs Human Review"
ACTION_UNKNOWN = "Unknown"

POSTING_ACTIONS = {
    ACTION_CREATE_BILL,
    ACTION_CREATE_EXPENSE,
    ACTION_CREATE_VENDOR_CREDIT,
    ACTION_ATTACH_TO_EXISTING_BILL,
    ACTION_IGNORE,
    ACTION_RECONCILIATION_ONLY,
    ACTION_NEEDS_HUMAN_REVIEW,
    ACTION_UNKNOWN,
}

QBO_OBJECT_MAP = {
    ACTION_CREATE_BILL: "Bill",
    ACTION_CREATE_EXPENSE: "Expense",
    ACTION_CREATE_VENDOR_CREDIT: "VendorCredit",
    ACTION_ATTACH_TO_EXISTING_BILL: "None",
    ACTION_IGNORE: "None",
    ACTION_RECONCILIATION_ONLY: "None",
    ACTION_NEEDS_HUMAN_REVIEW: "None",
    ACTION_UNKNOWN: "None",
}

VAT_CLAIMABLE = "Claimable"
VAT_NON_CLAIMABLE = "Non Claimable"
VAT_ZERO_RATED = "Zero Rated"
VAT_EXEMPT = "Exempt"
VAT_UNKNOWN = "Unknown"

SUPPORTING_DOCUMENTS = {
    DOC_PAYMENT_PROOF,
    DOC_REMITTANCE_ADVICE,
    DOC_SUPPLIER_STATEMENT,
    DOC_QUOTATION,
    DOC_DELIVERY_NOTE,
    DOC_MERCHANT_SETTLEMENT,
}

CAPITAL_ASSET_TERMS = (
    "salon chair",
    "chair",
    "mirror",
    "laptop",
    "printer",
    "air conditioner",
    "a/c",
    "ac unit",
    "furniture",
    "fit out",
    "fit-out",
    "machinery",
    "machine",
    "leasehold improvement",
    "reception desk",
    "wash basin",
    "dryer",
    "steamer",
    "pos terminal",
)


def _load_vendor_rules() -> dict:
    if not VENDOR_RULES_FILE.exists():
        return {}
    try:
        data = json.loads(VENDOR_RULES_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data.get("vendors", {}) if isinstance(data, dict) else {}


def _normalise(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _supplier_rule(extracted: dict) -> dict:
    supplier = (extracted.get("supplier_name") or "").strip()
    if not supplier:
        return {}
    rules = _load_vendor_rules()
    if supplier in rules:
        return rules.get(supplier) or {}
    supplier_key = _normalise(supplier)
    for name, rule in rules.items():
        if _normalise(name) == supplier_key:
            return rule or {}
    return {}


def _append_reason(reasons: list[str], reason: str) -> None:
    if reason and reason not in reasons:
        reasons.append(reason)


def _decision_text(extracted: dict) -> str:
    pieces = [_text_blob(extracted)]
    for key in (
        "email_subject",
        "email_body",
        "email_context",
        "file_name",
        "filename",
        "source_file",
        "primary_source_file",
        "payment_reference",
        "document_category",
    ):
        pieces.append(str(extracted.get(key) or ""))
    for document in extracted.get("supporting_documents") or []:
        if isinstance(document, dict):
            pieces.extend(str(document.get(key) or "") for key in ("type", "name", "notes", "text"))
        else:
            pieces.append(str(document))
    return " ".join(pieces).lower()


def _contains_any(text: str, terms: tuple[str, ...] | list[str]) -> bool:
    return any(term in text for term in terms)


def _normalised_document_type(extracted: dict) -> str:
    category = extracted.get("document_category")
    allocation = str(extracted.get("allocation_type") or "").lower()
    raw_type = str(extracted.get("document_type") or "").strip().lower()
    treatment = str(extracted.get("accounting_treatment") or "").strip().lower()
    qbo_type = str(extracted.get("qbo_posting_type") or "").strip().lower()
    text = _decision_text(extracted)

    if allocation == "shared" or category == DOCUMENT_SHARED_COST:
        return DOC_SHARED_EXPENSE
    if allocation == "head_office" or category == DOCUMENT_HEAD_OFFICE_COST:
        return DOC_HEAD_OFFICE_EXPENSE

    explicit_map = {
        "supplier invoice": DOC_SUPPLIER_INVOICE,
        "supplier bill": DOC_SUPPLIER_INVOICE,
        "invoice": DOC_SUPPLIER_INVOICE,
        "bill": DOC_SUPPLIER_INVOICE,
        "expense receipt": DOC_EXPENSE_RECEIPT,
        "receipt": DOC_EXPENSE_RECEIPT,
        "cash receipt": DOC_EXPENSE_RECEIPT,
        "card receipt": DOC_EXPENSE_RECEIPT,
        "credit note": DOC_CREDIT_NOTE,
        "supplier credit note": DOC_CREDIT_NOTE,
        "debit note": DOC_DEBIT_NOTE,
        "payment proof": DOC_PAYMENT_PROOF,
        "payment slip": DOC_PAYMENT_PROOF,
        "remittance advice": DOC_REMITTANCE_ADVICE,
        "supplier statement": DOC_SUPPLIER_STATEMENT,
        "statement": DOC_SUPPLIER_STATEMENT,
        "quotation": DOC_QUOTATION,
        "quote": DOC_QUOTATION,
        "delivery note": DOC_DELIVERY_NOTE,
        "asset purchase": DOC_ASSET_PURCHASE,
        "head office expense": DOC_HEAD_OFFICE_EXPENSE,
        "shared expense": DOC_SHARED_EXPENSE,
        "merchant settlement": DOC_MERCHANT_SETTLEMENT,
        "unknown": DOC_UNKNOWN,
    }
    if raw_type in explicit_map:
        return explicit_map[raw_type]

    if category == DOCUMENT_ASSET_PURCHASE:
        return DOC_ASSET_PURCHASE
    if category == DOCUMENT_SUPPLIER_BILL:
        return DOC_SUPPLIER_INVOICE
    if category == DOCUMENT_EXPENSE_RECEIPT:
        return DOC_EXPENSE_RECEIPT
    if category == DOCUMENT_CREDIT_NOTE:
        return DOC_CREDIT_NOTE
    if category == CATEGORY_DEBIT_NOTE:
        return DOC_DEBIT_NOTE
    if category == DOCUMENT_PAYMENT_PROOF:
        return DOC_PAYMENT_PROOF
    if category == DOCUMENT_REMITTANCE:
        return DOC_REMITTANCE_ADVICE
    if category == DOCUMENT_STATEMENT:
        return DOC_SUPPLIER_STATEMENT
    if category == DOCUMENT_QUOTATION:
        return DOC_QUOTATION
    if category == DOCUMENT_DELIVERY_NOTE:
        return DOC_DELIVERY_NOTE
    if category == DOCUMENT_UNKNOWN:
        return DOC_UNKNOWN

    if "merchant settlement" in text or "card settlement" in text or treatment == "merchant_settlement":
        return DOC_MERCHANT_SETTLEMENT
    if "statement of account" in text or "supplier statement" in text:
        return DOC_SUPPLIER_STATEMENT
    if "remittance advice" in text or "payment advice" in text:
        return DOC_REMITTANCE_ADVICE
    if "payment proof" in text or "payment slip" in text or "bank receipt" in text:
        return DOC_PAYMENT_PROOF
    if "delivery note" in text or "delivery order" in text:
        return DOC_DELIVERY_NOTE
    if "quotation" in text or "quote" in text or "proposal" in text or "estimate" in text:
        return DOC_QUOTATION
    if "debit note" in text:
        return DOC_DEBIT_NOTE
    if "credit note" in text or qbo_type in {"suppliercredit", "vendorcredit"}:
        return DOC_CREDIT_NOTE
    if "receipt" in text and not ("invoice" in text or "tax invoice" in text):
        return DOC_EXPENSE_RECEIPT
    if "invoice" in text or "tax invoice" in text or qbo_type == "bill":
        return DOC_SUPPLIER_INVOICE
    return DOC_UNKNOWN


def _capitalisation_required(extracted: dict, document_type: str) -> bool:
    if bool(extracted.get("capitalisation_required")) or bool(extracted.get("possible_capital_asset")):
        return True
    if document_type in SUPPORTING_DOCUMENTS:
        return False
    return _contains_any(_decision_text(extracted), CAPITAL_ASSET_TERMS)


def determine_payment_status(extracted: dict, document_type: str) -> str:
    if document_type in {DOC_PAYMENT_PROOF, DOC_REMITTANCE_ADVICE}:
        return PAID
    if document_type in {DOC_SUPPLIER_STATEMENT, DOC_QUOTATION, DOC_DELIVERY_NOTE, DOC_UNKNOWN}:
        return UNKNOWN_PAYMENT
    if document_type == DOC_MERCHANT_SETTLEMENT:
        return PAID
    if document_type in {DOC_CREDIT_NOTE, DOC_DEBIT_NOTE}:
        return UNKNOWN_PAYMENT

    amount = _as_float(extracted.get("payment_amount"))
    total = _as_float(extracted.get("total_amount"))
    if extracted.get("payment_confirmed"):
        if amount is not None and total is not None and amount + 0.01 < total:
            return PARTIALLY_PAID
        return PAID

    text = _decision_text(extracted)
    if _contains_any(text, ("partially paid", "partial payment", "part payment")):
        return PARTIALLY_PAID
    if "balance due" in text and _contains_any(text, ("paid", "payment")):
        return PARTIALLY_PAID
    if _contains_any(text, ("paid by", "paid via", "payment received", "settled", "fully paid", "receipt")):
        return PAID
    if _contains_any(text, ("amount due", "due amount", "payment pending", "unpaid", "payable", "due date")):
        return UNPAID
    if document_type == DOC_EXPENSE_RECEIPT:
        return PAID
    if document_type in {DOC_SUPPLIER_INVOICE, DOC_HEAD_OFFICE_EXPENSE, DOC_ASSET_PURCHASE, DOC_SHARED_EXPENSE}:
        return UNPAID
    return UNKNOWN_PAYMENT


def determine_payment_method(extracted: dict) -> str:
    raw = str(extracted.get("payment_method") or "").strip().lower().replace("-", " ").replace("_", " ")
    text = f"{raw} {_decision_text(extracted)}"
    if _contains_any(text, ("credit card", "visa credit", "mastercard credit")):
        return PAYMENT_CREDIT_CARD
    if _contains_any(text, ("debit card", "visa debit", "mastercard debit")):
        return PAYMENT_DEBIT_CARD
    if raw == "card" or _contains_any(text, ("card receipt", "pos receipt", "card payment")):
        return PAYMENT_CREDIT_CARD
    if _contains_any(text, ("bank transfer", "wire transfer", "fund transfer", "iban", "swift", "neft", "rtgs")):
        return PAYMENT_BANK_TRANSFER
    if _contains_any(text, ("cheque", "check")):
        return PAYMENT_CHEQUE
    if _contains_any(text, ("cash", "paid cash", "cash receipt")):
        return PAYMENT_CASH
    return PAYMENT_UNKNOWN


def determine_vat_treatment(extracted: dict, document_type: str) -> tuple[str, str]:
    if document_type in SUPPORTING_DOCUMENTS:
        return VAT_NON_CLAIMABLE, f"{document_type} is supporting evidence, not a tax invoice."

    text = _decision_text(extracted)
    status = extracted.get("vat_validation_status")
    tax = _as_float(extracted.get("tax_amount"))
    total = _as_float(extracted.get("total_amount"))
    trn = _detect_trn(extracted)

    if _contains_any(text, ("vat exempt", "exempt supply", "tax exempt")):
        return VAT_EXEMPT, "Document wording indicates exempt VAT treatment."
    if _contains_any(text, ("zero rated", "zero-rated", "0% vat")):
        return VAT_ZERO_RATED, "Document wording indicates zero-rated VAT treatment."
    if status == "valid" and trn:
        return VAT_CLAIMABLE, "Standard-rate VAT and supplier TRN were detected."
    if status in {"invalid_vat_rate"}:
        return VAT_UNKNOWN, "VAT amount is present but the rate does not match configured UAE VAT rules."
    if status in {"missing_or_zero_vat"}:
        if tax == 0:
            return VAT_ZERO_RATED, "VAT is zero; review whether the supply is zero-rated or non-claimable."
        return VAT_UNKNOWN, "VAT amount is missing."
    if tax is None:
        return VAT_UNKNOWN, "VAT amount was not extracted."
    if tax == 0:
        return VAT_ZERO_RATED, "VAT amount is zero."
    if total and total > tax > 0:
        rate = round(tax / (total - tax), 4)
        if abs(rate - 0.05) <= 0.002 and trn:
            return VAT_CLAIMABLE, "VAT rate is approximately 5% and supplier TRN was detected."
        if abs(rate - 0.05) <= 0.002 and not trn:
            return VAT_UNKNOWN, "VAT appears to be 5%, but no supplier TRN was detected."
    return VAT_UNKNOWN, "VAT treatment could not be determined safely."


def determine_posting_action(document_type: str, payment_status: str, capitalisation_required: bool) -> str:
    if capitalisation_required:
        return ACTION_NEEDS_HUMAN_REVIEW
    if document_type == DOC_SUPPLIER_INVOICE:
        if payment_status == PAID:
            return ACTION_CREATE_EXPENSE
        return ACTION_CREATE_BILL
    if document_type == DOC_EXPENSE_RECEIPT:
        return ACTION_CREATE_EXPENSE
    if document_type == DOC_HEAD_OFFICE_EXPENSE:
        if payment_status == PAID:
            return ACTION_CREATE_EXPENSE
        return ACTION_CREATE_BILL
    if document_type == DOC_CREDIT_NOTE:
        return ACTION_CREATE_VENDOR_CREDIT
    if document_type in {DOC_PAYMENT_PROOF, DOC_REMITTANCE_ADVICE}:
        return ACTION_ATTACH_TO_EXISTING_BILL
    if document_type in {DOC_SUPPLIER_STATEMENT, DOC_MERCHANT_SETTLEMENT}:
        return ACTION_RECONCILIATION_ONLY
    if document_type in {DOC_QUOTATION, DOC_DELIVERY_NOTE}:
        return ACTION_IGNORE
    if document_type in {DOC_ASSET_PURCHASE, DOC_SHARED_EXPENSE, DOC_DEBIT_NOTE, DOC_UNKNOWN}:
        return ACTION_NEEDS_HUMAN_REVIEW
    return ACTION_UNKNOWN


def _confidence(extracted: dict) -> float:
    for key in ("overall_confidence", "decision_confidence", "extraction_confidence"):
        value = extracted.get(key)
        try:
            return round(float(value), 4)
        except (TypeError, ValueError):
            continue
    return 0.0


def _branch_allocation(extracted: dict) -> str:
    allocation = extracted.get("allocation_type") or "unknown"
    brand = extracted.get("brand") or extracted.get("business_unit") or "Unknown"
    branch = extracted.get("branch")
    if branch:
        return f"{brand}/{branch} ({allocation})"
    return f"{brand} ({allocation})"


def _is_folder_conflict(extracted: dict) -> bool:
    if extracted.get("folder_conflict") or extracted.get("allocation_conflict"):
        return True
    folder_branch = _normalise(extracted.get("folder_branch") or extracted.get("folder_path_branch"))
    branch = _normalise(extracted.get("branch"))
    if folder_branch and branch and folder_branch != branch:
        return True
    folder_brand = _normalise(extracted.get("folder_brand") or extracted.get("folder_path_brand"))
    brand = _normalise(extracted.get("brand") or extracted.get("business_unit"))
    return bool(folder_brand and brand and folder_brand != brand)


def _apply_rule_overrides(
    extracted: dict,
    vendor_rule: dict,
    document_type: str,
    payment_status: str,
    payment_method: str,
    posting_action: str,
    vat_treatment: str,
) -> tuple[str, str, str, str, str]:
    document_type = vendor_rule.get("document_type") or document_type
    payment_status = vendor_rule.get("payment_status") or payment_status
    payment_method = vendor_rule.get("payment_method") or payment_method
    posting_action = vendor_rule.get("posting_action") or vendor_rule.get("posting_type") or posting_action
    vat_treatment = vendor_rule.get("vat_treatment") or vat_treatment

    if extracted.get("human_override_decision"):
        document_type = extracted.get("decision_document_type") or document_type
        payment_status = extracted.get("payment_status") or payment_status
        payment_method = determine_payment_method(extracted)
        posting_action = extracted.get("posting_action") or extracted.get("posting_type") or posting_action
        vat_treatment = extracted.get("vat_treatment") or vat_treatment

    legacy_action_map = {
        "Bill": ACTION_CREATE_BILL,
        "Expense": ACTION_CREATE_EXPENSE,
        "Vendor Credit": ACTION_CREATE_VENDOR_CREDIT,
        "SupplierCredit": ACTION_CREATE_VENDOR_CREDIT,
        "VendorCredit": ACTION_CREATE_VENDOR_CREDIT,
        "Attach Only": ACTION_ATTACH_TO_EXISTING_BILL,
        "Review Required": ACTION_NEEDS_HUMAN_REVIEW,
        "Needs Review": ACTION_NEEDS_HUMAN_REVIEW,
    }
    posting_action = legacy_action_map.get(posting_action, posting_action)
    if posting_action not in POSTING_ACTIONS:
        posting_action = ACTION_UNKNOWN
    if document_type not in SUPPORTED_DOCUMENT_TYPES:
        document_type = DOC_UNKNOWN
    return document_type, payment_status, payment_method, posting_action, vat_treatment


def _review_reasons(
    extracted: dict,
    document_type: str,
    posting_action: str,
    payment_status: str,
    payment_method: str,
    vat_treatment: str,
    capitalisation_required: bool,
    confidence: float,
) -> list[str]:
    reasons: list[str] = []
    if posting_action in {ACTION_NEEDS_HUMAN_REVIEW, ACTION_UNKNOWN}:
        _append_reason(reasons, f"{document_type} requires accounting review.")
    if capitalisation_required:
        _append_reason(reasons, "Possible fixed asset requires capitalisation review.")
    if document_type == DOC_SHARED_EXPENSE:
        _append_reason(reasons, "Shared expense requires manual allocation.")
    if document_type == DOC_UNKNOWN:
        _append_reason(reasons, "Document type is unknown.")
    if payment_status == UNKNOWN_PAYMENT and posting_action in {
        ACTION_CREATE_BILL,
        ACTION_CREATE_EXPENSE,
        ACTION_CREATE_VENDOR_CREDIT,
    }:
        _append_reason(reasons, "Payment status is unknown.")
    if payment_status == PAID and payment_method == PAYMENT_UNKNOWN and posting_action == ACTION_CREATE_EXPENSE:
        _append_reason(reasons, "Payment method is unknown for paid expense.")
    if vat_treatment == VAT_UNKNOWN and document_type not in SUPPORTING_DOCUMENTS:
        _append_reason(reasons, "VAT treatment is unknown.")
    if confidence < 0.90:
        _append_reason(reasons, f"Confidence {confidence:.2f} below auto-post threshold.")
    if extracted.get("needs_review") or extracted.get("manual_check"):
        _append_reason(reasons, extracted.get("review_reason") or "Existing review flag.")
    if (extracted.get("allocation_type") or "unknown") in {"unknown", "shared"}:
        _append_reason(reasons, "Allocation is not auto-postable.")
    if _is_folder_conflict(extracted):
        _append_reason(reasons, "Folder allocation conflicts with extracted branch or business unit.")
    return reasons


def _decision_explanation(extracted: dict, decision: dict) -> str:
    supplier = extracted.get("supplier_name") or "unknown supplier"
    parts = [
        f"{decision['document_type']} detected from {supplier}.",
        f"Payment status: {decision['payment_status']}.",
        f"Payment method: {decision['payment_method']}.",
        f"Posting action: {decision['posting_action']}.",
        f"QBO object: {decision['qbo_object']}.",
        f"VAT treatment: {decision['vat_treatment']} ({decision['vat_explanation']}).",
        f"Branch allocation: {decision['branch_allocation']}.",
        f"Confidence {round(decision['confidence'] * 100)}%.",
    ]
    if decision["capitalisation_required"]:
        parts.append("Capitalisation review required.")
    if decision["requires_review"]:
        parts.append(f"Review required: {decision['review_reason']}.")
    return " ".join(parts)


def build_accounting_decision(extracted: dict) -> dict:
    vendor_rule = _supplier_rule(extracted)
    document_type = _normalised_document_type(extracted)
    capitalisation_required = _capitalisation_required(extracted, document_type)
    if capitalisation_required and document_type not in SUPPORTING_DOCUMENTS:
        document_type = DOC_ASSET_PURCHASE
    payment_status = determine_payment_status(extracted, document_type)
    payment_method = determine_payment_method(extracted)
    vat_treatment, vat_explanation = determine_vat_treatment(extracted, document_type)
    posting_action = determine_posting_action(document_type, payment_status, capitalisation_required)
    document_type, payment_status, payment_method, posting_action, vat_treatment = _apply_rule_overrides(
        extracted,
        vendor_rule,
        document_type,
        payment_status,
        payment_method,
        posting_action,
        vat_treatment,
    )
    qbo_object = vendor_rule.get("qbo_object") or vendor_rule.get("qbo_posting_type") or QBO_OBJECT_MAP.get(posting_action, "None")
    if qbo_object == "SupplierCredit":
        qbo_object = "VendorCredit"
    if qbo_object not in {"Bill", "Expense", "VendorCredit", "None"}:
        qbo_object = QBO_OBJECT_MAP.get(posting_action, "None")

    confidence = _confidence(extracted)
    reasons = _review_reasons(
        extracted,
        document_type,
        posting_action,
        payment_status,
        payment_method,
        vat_treatment,
        capitalisation_required,
        confidence,
    )

    decision = {
        "document_type": document_type,
        "posting_action": posting_action,
        "qbo_object": qbo_object,
        "payment_status": payment_status,
        "payment_method": payment_method,
        "capitalisation_required": capitalisation_required,
        "vat_treatment": vat_treatment,
        "requires_review": bool(reasons),
        "review_reason": " | ".join(reasons),
        "decision_explanation": "",
        "confidence": confidence,
        "vat_explanation": vat_explanation,
        "branch_allocation": _branch_allocation(extracted),
        "decision_engine_version": 3,
    }

    # Backward-compatible aliases used by the existing review and dry-run layers.
    decision["posting_type"] = posting_action
    decision["qbo_posting_type"] = qbo_object
    decision["capitalisation_flag"] = capitalisation_required
    decision["decision_explanation"] = _decision_explanation(extracted, decision)
    return decision


def apply_accounting_decision(extracted: dict) -> dict:
    enriched = dict(extracted)
    decision = build_accounting_decision(enriched)
    enriched["accounting_decision"] = decision
    enriched["payment_status"] = decision["payment_status"]
    enriched["payment_method"] = decision["payment_method"]
    enriched["posting_action"] = decision["posting_action"]
    enriched["posting_type"] = decision["posting_action"]
    enriched["qbo_object"] = decision["qbo_object"]
    enriched["qbo_posting_type"] = decision["qbo_object"]
    enriched["vat_treatment"] = decision["vat_treatment"]
    enriched["vat_explanation"] = decision["vat_explanation"]
    enriched["capitalisation_required"] = decision["capitalisation_required"]
    enriched["possible_capital_asset"] = decision["capitalisation_required"] or bool(enriched.get("possible_capital_asset"))
    enriched["decision_explanation"] = decision["decision_explanation"]
    if decision["requires_review"]:
        enriched["needs_review"] = True
        existing = (enriched.get("review_reason") or "").strip()
        reason = decision["review_reason"]
        enriched["review_reason"] = f"{existing} | {reason}" if existing and reason not in existing else (existing or reason)
    else:
        enriched.setdefault("needs_review", False)
        enriched.setdefault("review_reason", "")
    return enriched
