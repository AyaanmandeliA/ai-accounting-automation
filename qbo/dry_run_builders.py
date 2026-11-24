from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from extraction.coa_loader import validate_account_for_transaction
from qbo.qbo_poster import _duplicate_fingerprint

ENABLE_QBO_EXPENSE_POSTING = "ENABLE_QBO_EXPENSE_POSTING"
ENABLE_QBO_VENDOR_CREDIT_POSTING = "ENABLE_QBO_VENDOR_CREDIT_POSTING"


def _enabled(env_name: str) -> bool:
    return os.environ.get(env_name, "false").strip().lower() in {"1", "true", "yes", "on"}


def expense_posting_enabled() -> bool:
    return _enabled(ENABLE_QBO_EXPENSE_POSTING)


def vendor_credit_posting_enabled() -> bool:
    return _enabled(ENABLE_QBO_VENDOR_CREDIT_POSTING)


def _required(extracted: dict, fields: list[str]) -> list[str]:
    return [field for field in fields if extracted.get(field) in (None, "", [])]


def _attachment_status(extracted: dict) -> dict[str, Any]:
    paths = extracted.get("all_source_files") or [extracted.get("source_file")]
    missing = []
    present = []
    for raw in paths:
        if not raw:
            continue
        path = Path(raw)
        if path.exists():
            present.append(str(path))
        else:
            missing.append(str(path))
    return {"present": present, "missing": missing}


def _line_payload(extracted: dict, allowed_account_types: set[str]) -> tuple[list[dict], list[str]]:
    lines = []
    errors = []
    probe = dict(extracted)
    for index, item in enumerate(extracted.get("line_items") or [], 1):
        account = {
            "name": item.get("account_name"),
            "account_type": item.get("account_type") or "Expense",
            "detail_type": item.get("detail_type") or "",
        }
        account_type = str(account["account_type"] or "").strip().lower()
        if account_type not in allowed_account_types:
            errors.append(f"Line {index} uses disallowed account type: {account['account_type']}")
        reason = validate_account_for_transaction(account, probe, item)
        if reason:
            errors.append(f"Line {index}: {reason}")
        if item.get("amount") in (None, ""):
            errors.append(f"Line {index} is missing amount")
        if not item.get("account_name"):
            errors.append(f"Line {index} is missing account_name")
        lines.append({
            "DetailType": "AccountBasedExpenseLineDetail",
            "description": item.get("description"),
            "Amount": item.get("amount"),
            "AccountBasedExpenseLineDetail": {
                "AccountRef": {"name": item.get("account_name")},
                "ClassRef": {"name": extracted.get("qbo_class_name")} if extracted.get("qbo_class_name") else None,
            },
            "account_type_preflight": item.get("account_type") or "Expense",
        })
    if not lines:
        errors.append("No line items")
    return lines, errors


def _base_result(extracted: dict, posting_type: str, live_enabled: bool, flag_name: str) -> dict:
    attachments = _attachment_status(extracted)
    required_missing = _required(
        extracted,
        ["supplier_name", "invoice_date", "total_amount", "qbo_class_name", "qbo_location_name"],
    )
    errors = [f"Missing required field: {field}" for field in required_missing]
    if attachments["missing"]:
        errors.append("One or more attachment paths are missing")
    return {
        "qbo_posting_type": posting_type,
        "qbo_api_entity": posting_type,
        "live_posting_enabled": live_enabled,
        "feature_flag": flag_name,
        "blocked_from_live_posting": not live_enabled,
        "duplicate_fingerprint": _duplicate_fingerprint(extracted),
        "attachment_handling": attachments,
        "validation_errors": errors,
    }


def _payment_type(value: str | None) -> str:
    method = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if method in {"card", "credit_card", "debit_card"}:
        return "CreditCard"
    if method in {"cheque", "check"}:
        return "Check"
    return "Cash"


def build_expense_dry_run(extracted: dict) -> dict:
    result = _base_result(
        extracted,
        "Expense",
        expense_posting_enabled(),
        ENABLE_QBO_EXPENSE_POSTING,
    )
    result["qbo_api_entity"] = "Purchase"
    lines, errors = _line_payload(extracted, {"expense", "expenses", "cost of goods sold", "other expense"})
    result["validation_errors"].extend(errors)
    result["dry_run_payload"] = {
        "EntityRef": {"type": "Vendor", "name": extracted.get("supplier_name")},
        "TxnDate": extracted.get("payment_date") or extracted.get("invoice_date"),
        "TotalAmt": extracted.get("total_amount"),
        "PaymentType": _payment_type(extracted.get("payment_method")),
        "AccountRef": {"name": extracted.get("payment_bank_account") or extracted.get("payment_account_name")},
        "DepartmentRef": {"name": extracted.get("qbo_location_name")} if extracted.get("qbo_location_name") else None,
        "DocNumber": extracted.get("invoice_number"),
        "Line": lines,
    }
    result["requires_review"] = bool(result["validation_errors"]) or not result["live_posting_enabled"]
    return result


def build_bill_dry_run(extracted: dict) -> dict:
    result = _base_result(extracted, "Bill", True, "CONFIRM_LIVE_POSTING")
    result["blocked_from_live_posting"] = True
    result["live_posting_enabled"] = False
    lines, errors = _line_payload(extracted, {"expense", "expenses", "cost of goods sold", "other expense", "fixed asset"})
    result["validation_errors"].extend(errors)
    result["dry_run_payload"] = {
        "VendorRef": {"name": extracted.get("supplier_name")},
        "TxnDate": extracted.get("invoice_date"),
        "DueDate": extracted.get("due_date"),
        "DocNumber": extracted.get("invoice_number"),
        "DepartmentRef": {"name": extracted.get("qbo_location_name")} if extracted.get("qbo_location_name") else None,
        "Line": lines,
    }
    result["requires_review"] = bool(result["validation_errors"]) or extracted.get("needs_review") or extracted.get("manual_check")
    return result


def build_vendor_credit_dry_run(extracted: dict) -> dict:
    result = _base_result(
        extracted,
        "VendorCredit",
        vendor_credit_posting_enabled(),
        ENABLE_QBO_VENDOR_CREDIT_POSTING,
    )
    lines, errors = _line_payload(extracted, {"expense", "expenses", "cost of goods sold", "other expense"})
    result["validation_errors"].extend(errors)
    result["dry_run_payload"] = {
        "VendorRef": {"name": extracted.get("supplier_name")},
        "TxnDate": extracted.get("invoice_date"),
        "TotalAmt": extracted.get("total_amount"),
        "DepartmentRef": {"name": extracted.get("qbo_location_name")} if extracted.get("qbo_location_name") else None,
        "DocNumber": extracted.get("invoice_number"),
        "Line": lines,
    }
    result["requires_review"] = bool(result["validation_errors"]) or not result["live_posting_enabled"]
    return result
