"""
Human Review Layer
Presents each extracted transaction for approval before QBO posting.
Statuses: pending -> approved / skipped.
"""

import json
import logging
import copy
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from accounting.audit_log import append_review_decision, resolve_reviewer_name
from accounting.metrics import record_review_metrics
from accounting.persistence import record_transaction
from accounting.rules import apply_accounting_rules
from allocation.branch_resolver import apply_qbo_mapping
from allocation.confidence import apply_review_gate
from allocation.review_router import copy_to_review_folder
from allocation.rules import remember_correction

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
EXTRACTED_DIR = BASE_DIR / "data/extracted"
REVIEW_FILE = BASE_DIR / "data/processed/review_decisions.json"
FEEDBACK_DIR = BASE_DIR / "data/feedback"
GOOGLE_FEEDBACK_JSONL = FEEDBACK_DIR / "google_extraction_feedback.jsonl"
GOOGLE_FEEDBACK_MD = FEEDBACK_DIR / "google_extraction_feedback.md"

APPROVED = "approved"
SKIPPED = "skipped"
NEEDS_REVIEW = "needs_review"
MANUAL_CHECK = "manual_check"
PENDING = "pending"
QUIT = "quit"
EDIT = "edit"

POSTABLE_QBO_TYPES = {"Bill"}
POSTING_TREATMENTS = {
    "Create Bill",
    "Create Expense",
    "Create Vendor Credit",
    "Attach To Existing Bill",
    "Ignore",
    "Reconciliation Only",
    "Needs Human Review",
    "Unknown",
}
PAYMENT_STATUSES = {"Paid", "Unpaid", "Partially Paid", "Unknown"}
PAYMENT_METHODS = {"Cash", "Credit Card", "Debit Card", "Bank Transfer", "Cheque", "Unknown"}

EXTRACTION_FEEDBACK_FIELDS = {
    "supplier_name": "supplier/vendor",
    "invoice_number": "invoice number",
    "invoice_date": "invoice date",
    "due_date": "due date",
    "total_amount": "total",
    "tax_amount": "VAT amount",
    "supplier_trn": "TRN",
    "line_items": "line items",
}
ACCOUNTING_FEEDBACK_FIELDS = {
    "posting_type": "posting treatment",
    "accounting_treatment": "accounting treatment",
    "payment_status": "payment status",
    "payment_method": "payment method",
    "qbo_posting_type": "QBO posting type",
    "vat_treatment": "VAT treatment",
    "branch": "branch",
    "qbo_class_name": "class",
    "qbo_location_name": "location",
    "notes": "notes",
}

SALON_UNITS = {
    "sample brand a",
    "sample brand a salon",
    "sample brand b",
    "sample brand b salon",
}
OUT_OF_SCOPE_UNITS = {
    "sr",
    "sample restaurant",
    "samplerestaurant",
    "sample kitchen",
}


def _normalise_unit(value) -> str:
    return str(value or "").strip().lower().replace("&", "and")


def _is_out_of_scope(extracted: dict) -> bool:
    unit = _normalise_unit(extracted.get("business_unit"))
    if unit in OUT_OF_SCOPE_UNITS:
        return True
    notes = str(extracted.get("notes") or "").lower()
    return notes.startswith("out of scope - sr")


def _load_decisions() -> dict[str, str]:
    if not REVIEW_FILE.exists():
        return {}
    try:
        data = json.loads(REVIEW_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read review decisions; starting fresh: %s", exc)
        return {}
    return {k: str(v) for k, v in data.items()}


def _save_decisions(decisions: dict[str, str]) -> None:
    REVIEW_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = REVIEW_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(decisions, indent=2))
    tmp.replace(REVIEW_FILE)


def _candidate_key(extracted: dict, fallback: str | None = None) -> str:
    extracted_file = extracted.get("extracted_file")
    if extracted_file:
        return Path(extracted_file).name
    if fallback:
        return fallback
    source = extracted.get("source_file")
    if source:
        return Path(source).name + ".extracted.json"
    return "unknown.extracted.json"


def _load_disk_candidates() -> dict[str, dict]:
    candidates = {}
    if not EXTRACTED_DIR.exists():
        return candidates
    for path in sorted(EXTRACTED_DIR.iterdir(), key=lambda p: p.name.lower()):
        if path.suffix != ".json" or not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not read %s: %s", path.name, exc)
            continue
        if not data.get("extracted_file"):
            data["extracted_file"] = str(path.resolve())
        candidates[path.name] = data
    return candidates


def _build_candidates(extracted_results: list[dict] | None) -> dict[str, dict]:
    candidates = _load_disk_candidates()
    for result in extracted_results or []:
        key = _candidate_key(result)
        if key in candidates:
            candidates[key].update(result)
        else:
            candidates[key] = result
    return candidates


def _save_extracted(filename: str, extracted: dict) -> None:
    EXTRACTED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EXTRACTED_DIR / filename
    extracted["extracted_file"] = str(out_path.resolve())
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(extracted, indent=2, default=str))
    tmp.replace(out_path)
    record_transaction(filename, extracted, status="review_saved")


def _fmt_amount(amount, currency="AED") -> str:
    if amount in (None, ""):
        return "?"
    try:
        return f"{currency} {float(amount):,.2f}"
    except (TypeError, ValueError):
        return f"{currency} {amount}"


def _title_value(value) -> str:
    text = str(value or "").strip()
    if not text:
        return "Unknown"
    aliases = {
        "supplier_bill_unpaid": "Bill",
        "supplier_bill_paid": "Expense",
        "direct_expense_paid": "Expense",
        "cash_expense_paid": "Expense",
        "card_expense_paid": "Expense",
        "supplier_credit": "Vendor Credit",
        "supplier_payment_only": "Attach Only",
        "supporting_only": "Attach Only",
        "merchant_settlement": "Reconciliation Only",
        "bank_deposit": "Reconciliation Only",
        "bank_transfer": "Reconciliation Only",
        "ignore": "Needs Review",
        "None": "Attach Only",
        "SupplierCredit": "Vendor Credit",
        "review required": "Needs Review",
        "Review Required": "Needs Review",
        "Bill": "Create Bill",
        "Expense": "Create Expense",
        "Vendor Credit": "Create Vendor Credit",
        "Supplier Credit": "Create Vendor Credit",
        "VendorCredit": "Create Vendor Credit",
        "Attach Only": "Attach To Existing Bill",
        "Needs Review": "Needs Human Review",
        "Needs Human Review": "Needs Human Review",
    }
    if text in aliases:
        return aliases[text]
    lower = text.lower()
    if lower in aliases:
        return aliases[lower]
    return text.replace("_", " ").title()


def _display_posting_treatment(extracted: dict, decision: dict | None = None) -> str:
    decision = decision or extracted.get("accounting_decision") or {}
    treatment = (
        extracted.get("posting_type")
        or decision.get("posting_type")
        or extracted.get("accounting_treatment")
        or extracted.get("qbo_posting_type")
    )
    label = _title_value(treatment)
    if label == "Review Required":
        return "Needs Human Review"
    return label if label in POSTING_TREATMENTS else label


def _display_payment_status(extracted: dict, decision: dict | None = None) -> str:
    decision = decision or extracted.get("accounting_decision") or {}
    status = extracted.get("payment_status") or decision.get("payment_status")
    if status:
        return _title_value(status)
    if extracted.get("payment_confirmed"):
        return "Paid"
    method = str(extracted.get("payment_method") or "").lower()
    if method in {"unpaid", "pay later"}:
        return "Unpaid"
    return "Unknown"


def _display_payment_method(extracted: dict, decision: dict | None = None) -> str:
    decision = decision or extracted.get("accounting_decision") or {}
    method = str(extracted.get("payment_method") or decision.get("payment_method") or "").strip().lower()
    aliases = {
        "cash": "Cash",
        "card": "Card",
        "credit card": "Credit Card",
        "debit card": "Debit Card",
        "bank": "Bank Transfer",
        "bank_transfer": "Bank Transfer",
        "bank transfer": "Bank Transfer",
        "wire": "Bank Transfer",
        "cheque": "Cheque",
        "check": "Cheque",
        "unpaid": "Unknown",
        "unknown": "Unknown",
    }
    return aliases.get(method, _title_value(method) if method else "Unknown")


def _expense_accounts(extracted: dict) -> list[str]:
    accounts = []
    for line in extracted.get("line_items") or []:
        account = (line.get("account_name") or "").strip()
        if account and account not in accounts:
            accounts.append(account)
    return accounts


def _accounting_reasoning(extracted: dict, decision: dict | None = None) -> str:
    decision = decision or extracted.get("accounting_decision") or {}
    currency = extracted.get("currency") or "AED"
    supplier = extracted.get("supplier_name") or "the supplier"
    doc_type = decision.get("document_type") or extracted.get("document_type") or "document"
    total = _fmt_amount(extracted.get("total_amount"), currency)
    vat = _fmt_amount(extracted.get("tax_amount"), currency)
    payment_status = _display_payment_status(extracted, decision)
    posting = _display_posting_treatment(extracted, decision)
    branch_source = "folder path" if extracted.get("folder_path_hint") else "document/allocation rules"
    account_source = "supplier history" if extracted.get("provider_name") == "google" else "accounting rules"
    if extracted.get("human_override_decision"):
        account_source = "human correction"
    accounts = _expense_accounts(extracted)
    account_text = accounts[0] if accounts else "no expense account"
    payment_text = (
        "payment proof was found"
        if payment_status == "Paid"
        else "no payment proof found"
        if payment_status == "Unpaid"
        else f"payment status is {payment_status.lower()}"
    )
    return (
        f"{extracted.get('provider_name', 'System').title()} detected {doc_type} from {supplier} "
        f"with total {total} and VAT {vat}. {payment_text.capitalize()}, so proposed treatment is "
        f"{payment_status.lower()} {posting}. Branch came from {branch_source}. "
        f"Account {account_text} came from {account_source}."
    )


def _line_total(lines: list[dict]) -> float | None:
    total = 0.0
    seen = False
    for line in lines:
        amount = line.get("amount")
        if amount in (None, ""):
            continue
        try:
            total += float(amount)
            seen = True
        except (TypeError, ValueError):
            return None
    return total if seen else None


def _flags(extracted: dict) -> list[str]:
    flags = []
    doc_type = (extracted.get("document_type") or "").lower()
    treatment = (extracted.get("accounting_treatment") or "").strip()
    qbo_type = (extracted.get("qbo_posting_type") or "").strip()
    payment_method = (extracted.get("payment_method") or "").strip()
    if not doc_type:
        flags.append("missing document type")
    if not treatment:
        flags.append("missing accounting treatment")
    if not payment_method:
        flags.append("missing payment method")
    if not qbo_type:
        flags.append("missing QBO posting type")
    elif qbo_type not in POSTABLE_QBO_TYPES:
        flags.append(f"QBO will skip {qbo_type}")
    elif doc_type not in {"invoice", "bill"}:
        flags.append("QBO Bill selected but document is not invoice/bill")
    if doc_type == "supporting_only" or extracted.get("total_amount") in (None, ""):
        flags.append("CHECK: may not be a bill")
    if doc_type and doc_type not in {"invoice", "bill"}:
        flags.append("not payable bill - do not approve for QBO")
    if not extracted.get("supplier_name"):
        flags.append("missing supplier")
    if not extracted.get("business_unit"):
        flags.append("missing business unit")
    if not extracted.get("brand"):
        flags.append("missing brand")
    if extracted.get("allocation_type") in (None, "", "unknown"):
        flags.append("unknown allocation")
    if extracted.get("allocation_type") == "branch" and not extracted.get("branch"):
        flags.append("missing branch")
    if extracted.get("allocation_type") == "shared":
        flags.append("shared bill requires allocation")
    if not extracted.get("qbo_class_name") or not extracted.get("qbo_location_name"):
        flags.append("missing QBO class/location")
    if extracted.get("needs_review"):
        flags.append("NEEDS REVIEW")
    if extracted.get("manual_check"):
        flags.append("MANUAL ALLOCATION REQUIRED")
    if float(extracted.get("overall_confidence") or 0) < 0.9:
        flags.append(f"confidence {float(extracted.get('overall_confidence') or 0):.2f} < 0.90")
    if not extracted.get("invoice_date"):
        flags.append("missing date")
    lines = extracted.get("line_items") or []
    if not lines:
        flags.append("no line items")
    if any(not line.get("account_name") for line in lines):
        flags.append("line missing account")
    if extracted.get("payment_confirmed"):
        if not extracted.get("payment_date"):
            flags.append("paid but missing payment date")
        if extracted.get("payment_amount") in (None, ""):
            flags.append("paid but missing payment amount")
        if not extracted.get("payment_bank_account"):
            flags.append("paid but missing bank account")
        if extracted.get("payment_amount") not in (None, "") and extracted.get("total_amount") not in (None, ""):
            try:
                if abs(float(extracted["payment_amount"]) - float(extracted["total_amount"])) > 0.01:
                    flags.append("payment amount differs from bill total")
            except (TypeError, ValueError):
                flags.append("payment amount is not numeric")
    line_total = _line_total(lines)
    total = extracted.get("total_amount")
    if line_total is not None and total not in (None, ""):
        try:
            if abs(line_total - float(total)) > 0.01:
                flags.append(f"line total {_fmt_amount(line_total, extracted.get('currency', 'AED'))} != total")
        except (TypeError, ValueError):
            flags.append("total is not numeric")
    return flags


def _normalised_feedback_value(value):
    if isinstance(value, list):
        return json.dumps(value, sort_keys=True, default=str)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, default=str)
    return value


def _field_change(before: dict, after: dict, field: str, label: str) -> dict | None:
    before_value = _normalised_feedback_value(before.get(field))
    after_value = _normalised_feedback_value(after.get(field))
    if before_value == after_value:
        return None
    return {
        "field": field,
        "label": label,
        "before": before.get(field),
        "after": after.get(field),
    }


def _account_change(before: dict, after: dict) -> dict | None:
    before_accounts = _expense_accounts(before)
    after_accounts = _expense_accounts(after)
    if before_accounts == after_accounts:
        return None
    return {
        "field": "line_items.account_name",
        "label": "expense account",
        "before": before_accounts,
        "after": after_accounts,
    }


def _feedback_record(before: dict, after: dict, *, filename: str, decision: str, reviewer: str) -> dict:
    extraction_mistakes = [
        change
        for field, label in EXTRACTION_FEEDBACK_FIELDS.items()
        if (change := _field_change(before, after, field, label))
    ]
    accounting_mistakes = [
        change
        for field, label in ACCOUNTING_FEEDBACK_FIELDS.items()
        if (change := _field_change(before, after, field, label))
    ]
    account_change = _account_change(before, after)
    if account_change:
        accounting_mistakes.append(account_change)
    return {
        "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "filename": filename,
        "source_file": after.get("source_file") or before.get("source_file"),
        "provider_name": after.get("provider_name") or before.get("provider_name"),
        "reviewer": reviewer,
        "decision": decision,
        "extraction_correct": not extraction_mistakes,
        "accounting_treatment_correct": not accounting_mistakes,
        "extraction_mistakes": extraction_mistakes,
        "accounting_mistakes": accounting_mistakes,
        "before": {
            "supplier_name": before.get("supplier_name"),
            "invoice_number": before.get("invoice_number"),
            "invoice_date": before.get("invoice_date"),
            "total_amount": before.get("total_amount"),
            "tax_amount": before.get("tax_amount"),
            "posting_type": before.get("posting_type"),
            "payment_status": before.get("payment_status"),
            "payment_method": before.get("payment_method"),
            "qbo_posting_type": before.get("qbo_posting_type"),
            "vat_treatment": before.get("vat_treatment"),
            "branch": before.get("branch"),
            "accounts": _expense_accounts(before),
        },
        "after": {
            "supplier_name": after.get("supplier_name"),
            "invoice_number": after.get("invoice_number"),
            "invoice_date": after.get("invoice_date"),
            "total_amount": after.get("total_amount"),
            "tax_amount": after.get("tax_amount"),
            "posting_type": after.get("posting_type"),
            "payment_status": after.get("payment_status"),
            "payment_method": after.get("payment_method"),
            "qbo_posting_type": after.get("qbo_posting_type"),
            "vat_treatment": after.get("vat_treatment"),
            "branch": after.get("branch"),
            "accounts": _expense_accounts(after),
        },
    }


def _load_feedback_records(path: Path | None = None) -> list[dict]:
    feedback_path = path or GOOGLE_FEEDBACK_JSONL
    if not feedback_path.exists():
        return []
    records = []
    for line in feedback_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _feedback_summary(records: list[dict]) -> dict:
    total = len(records)
    extraction_correct = sum(1 for record in records if record.get("extraction_correct"))
    accounting_correct = sum(1 for record in records if record.get("accounting_treatment_correct"))
    extraction_mistakes = Counter(
        mistake.get("label") or mistake.get("field")
        for record in records
        for mistake in record.get("extraction_mistakes") or []
    )
    accounting_mistakes = Counter(
        mistake.get("label") or mistake.get("field")
        for record in records
        for mistake in record.get("accounting_mistakes") or []
    )
    return {
        "total_feedback": total,
        "extraction_accuracy": round(extraction_correct / total, 4) if total else None,
        "accounting_treatment_accuracy": round(accounting_correct / total, 4) if total else None,
        "most_common_extraction_mistakes": extraction_mistakes.most_common(10),
        "most_common_accounting_mistakes": accounting_mistakes.most_common(10),
    }


def _format_accuracy(value) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def _write_feedback_markdown(records: list[dict], path: Path | None = None) -> None:
    md_path = path or GOOGLE_FEEDBACK_MD
    summary = _feedback_summary(records)
    lines = [
        "# Google Extraction Feedback",
        "",
        "## Summary",
        f"- Reviewed bills: {summary['total_feedback']}",
        f"- Extraction accuracy: {_format_accuracy(summary['extraction_accuracy'])}",
        f"- Accounting treatment accuracy: {_format_accuracy(summary['accounting_treatment_accuracy'])}",
        "",
        "## Most Common Extraction Mistakes",
    ]
    if summary["most_common_extraction_mistakes"]:
        lines.extend(f"- {label}: {count}" for label, count in summary["most_common_extraction_mistakes"])
    else:
        lines.append("- None recorded")
    lines.extend(["", "## Most Common Accounting Mistakes"])
    if summary["most_common_accounting_mistakes"]:
        lines.extend(f"- {label}: {count}" for label, count in summary["most_common_accounting_mistakes"])
    else:
        lines.append("- None recorded")
    lines.extend(["", "## Recent Feedback"])
    for record in records[-20:]:
        extraction = ", ".join(m["label"] for m in record.get("extraction_mistakes") or []) or "none"
        accounting = ", ".join(m["label"] for m in record.get("accounting_mistakes") or []) or "none"
        lines.append(
            f"- {record.get('reviewed_at')} | {record.get('filename')} | "
            f"extraction: {extraction} | accounting: {accounting}"
        )
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(lines) + "\n")


def _save_google_feedback(before: dict, after: dict, *, filename: str, decision: str, reviewer: str) -> dict:
    record = _feedback_record(before, after, filename=filename, decision=decision, reviewer=reviewer)
    GOOGLE_FEEDBACK_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with GOOGLE_FEEDBACK_JSONL.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    records = _load_feedback_records()
    _write_feedback_markdown(records)
    return record


def _print_transaction(extracted: dict, filename: str, index: int, total: int) -> None:
    sep = "-" * 78
    currency = extracted.get("currency", "AED")
    lines = extracted.get("line_items") or []
    docs = extracted.get("all_source_files") or [extracted.get("source_file", "")]
    source_file = extracted.get("source_file", "")

    print(f"\n{sep}")
    print(f"REVIEW {index}/{total}: {filename}")
    print(f"{extracted.get('business_unit') or '?'} | {extracted.get('supplier_name') or '?'} | "
          f"{extracted.get('invoice_number') or '-'} | "
          f"{extracted.get('invoice_date') or '?'} | {_fmt_amount(extracted.get('total_amount'), currency)}")
    print(f"brand: {extracted.get('brand') or '?'} | branch: {extracted.get('branch') or '?'} | "
          f"allocation: {extracted.get('allocation_type') or '?'}")
    print(f"type: {extracted.get('document_type') or '?'} | "
          f"category: {extracted.get('document_category') or '?'} | "
          f"treatment: {extracted.get('accounting_treatment') or '?'} | "
          f"posting: {extracted.get('posting_type') or '?'} | "
          f"payment: {extracted.get('payment_method') or '?'} | "
          f"QBO: {extracted.get('qbo_posting_type') or '?'}")
    print(f"confidence: {float(extracted.get('overall_confidence') or 0):.2f} | "
          f"class: {extracted.get('qbo_class_name') or '?'} | "
          f"location: {extracted.get('qbo_location_name') or '?'}")

    flags = _flags(extracted)
    if flags:
        print("FLAGS: " + " | ".join(flags))

    decision = extracted.get("accounting_decision") or {}
    vat_status = extracted.get("vat_validation_status")
    approval_level = extracted.get("approval_level")
    tax_tags = extracted.get("corporate_tax_tags") or []
    cutoff = extracted.get("cutoff") or {}
    if vat_status or approval_level or tax_tags or cutoff:
        print(f"rules: VAT {vat_status or '?'} | approval {approval_level or '?'} | "
              f"cutoff risk {bool(cutoff.get('prior_period_risk'))} | "
              f"tax tags {', '.join(tax_tags) if tax_tags else '-'}")
    if decision:
        print(f"decision: {decision.get('posting_type') or '?'} | "
              f"payment {decision.get('payment_status') or '?'} | "
              f"VAT {decision.get('vat_treatment') or '?'} | "
              f"review {bool(decision.get('requires_review'))}")
    explanation = (extracted.get("decision_explanation") or decision.get("decision_explanation") or "").strip()
    if explanation:
        print(f"EXPLANATION: {explanation}")

    accounts = _expense_accounts(extracted)
    class_location = " / ".join(
        part
        for part in (extracted.get("qbo_class_name"), extracted.get("qbo_location_name"))
        if part
    )
    print("\nPROPOSED ACCOUNTING")
    print(f"  posting treatment: {_display_posting_treatment(extracted, decision)}")
    print(f"  payment status: {_display_payment_status(extracted, decision)}")
    print(f"  payment method: {_display_payment_method(extracted, decision)}")
    print("  QuickBooks fields:")
    print(f"    QBO posting type: {extracted.get('qbo_posting_type') or decision.get('qbo_posting_type') or '?'}")
    print(f"    supplier/vendor: {extracted.get('supplier_name') or '?'}")
    print(f"    expense account: {', '.join(accounts) if accounts else '?'}")
    print(f"    VAT treatment: {extracted.get('vat_treatment') or decision.get('vat_treatment') or vat_status or '?'}")
    print(f"    branch: {extracted.get('branch') or '?'}")
    print(f"    class/location: {class_location or '?'}")
    print(f"    due date: {extracted.get('due_date') or '?'}")
    print(f"  reasoning: {_accounting_reasoning(extracted, decision)}")

    print("\nLINES")
    if not lines:
        print("  none")
    for i, line in enumerate(lines, 1):
        acct = line.get("account_name") or "NO ACCOUNT"
        desc = (line.get("description") or "")[:64]
        print(f"  {i:>2}. {_fmt_amount(line.get('amount'), currency):>14}  {acct:<32}  {desc}")

    notes = (extracted.get("notes") or "").strip()
    if notes:
        print(f"\nNOTE: {notes}")
    review_reason = (extracted.get("review_reason") or "").strip()
    if review_reason:
        print(f"REVIEW REASON: {review_reason}")
    source_hint = (extracted.get("folder_path_hint") or "").strip()
    if source_hint:
        print(f"SOURCE PATH: {source_hint}")

    paid = "YES" if extracted.get("payment_confirmed") else "NO"
    print("\nPAYMENT")
    print(f"  paid: {paid}")
    if extracted.get("payment_confirmed"):
        print(f"  date: {extracted.get('payment_date') or '?'}")
        print(f"  amount: {_fmt_amount(extracted.get('payment_amount'), currency)}")
        print(f"  bank: {extracted.get('payment_bank_account') or '?'}")

    print("\nDOCS")
    for doc in docs:
        marker = "primary" if doc and source_file and Path(doc) == Path(source_file) else "support"
        print(f"  {marker:<7} {Path(doc).name if doc else '?'}")
    print(sep)


def _read(prompt: str) -> str | None:
    try:
        return input(prompt).strip()
    except EOFError:
        return None


def _prompt_decision() -> str:
    while True:
        raw = _read("  [A]pprove  [E]dit  [N]eeds review  [M]anual allocation  [Q]uit > ")
        if raw is None:
            return QUIT
        raw = raw.lower()
        if raw in ("a", "approve"):
            return APPROVED
        if raw in ("e", "edit"):
            return EDIT
        if raw in ("n", "needs review", "review"):
            return NEEDS_REVIEW
        if raw in ("m", "manual", "manual allocation"):
            return MANUAL_CHECK
        if raw in ("s", "skip"):
            return NEEDS_REVIEW
        if raw in ("q", "quit"):
            return QUIT
        print("  Enter A, E, N, M, or Q.")


def _prompt_value(label: str, current):
    raw = _read(f"  {label} [{current if current not in (None, '') else '-'}] > ")
    if raw is None:
        return current
    return raw if raw else current


def _prompt_line(lines: list[dict]) -> dict | None:
    if not lines:
        print("  No line items to edit.")
        return None
    raw = _read(f"  Line number 1-{len(lines)} > ")
    if raw is None:
        return None
    try:
        index = int(raw) - 1
    except ValueError:
        return None
    if index < 0 or index >= len(lines):
        return None
    return lines[index]


def _prompt_bool(label: str, current) -> bool:
    current_text = "yes" if current else "no"
    raw = _read(f"  {label} [{current_text}] > ")
    if raw is None or not raw:
        return bool(current)
    return raw.lower() in ("y", "yes", "true", "1", "paid")


def _edit_payment(extracted: dict) -> None:
    extracted["payment_confirmed"] = _prompt_bool(
        "Payment confirmed", extracted.get("payment_confirmed")
    )
    if extracted["payment_confirmed"]:
        extracted["payment_date"] = _prompt_value("Payment date", extracted.get("payment_date"))
        extracted["payment_amount"] = _prompt_value("Payment amount", extracted.get("payment_amount"))
        extracted["payment_bank_account"] = _prompt_value(
            "Payment bank account", extracted.get("payment_bank_account")
        )
    else:
        extracted["payment_date"] = None
        extracted["payment_amount"] = None
        extracted["payment_bank_account"] = None


def _edit_classification(extracted: dict) -> None:
    extracted["human_override_decision"] = True
    extracted["document_type"] = _prompt_value("Document type", extracted.get("document_type"))
    extracted["decision_document_type"] = _prompt_value(
        "Decision document type",
        (extracted.get("accounting_decision") or {}).get("document_type") or extracted.get("decision_document_type"),
    )
    extracted["document_category"] = _prompt_value("Document category", extracted.get("document_category"))
    extracted["payment_status"] = _prompt_value("Payment status", extracted.get("payment_status"))
    extracted["posting_type"] = _prompt_value("Posting type", extracted.get("posting_type"))
    extracted["vat_treatment"] = _prompt_value("VAT treatment", extracted.get("vat_treatment"))
    extracted["accounting_treatment"] = _prompt_value(
        "Accounting treatment", extracted.get("accounting_treatment")
    )
    extracted["payment_method"] = _prompt_value("Payment method", extracted.get("payment_method"))
    extracted["qbo_posting_type"] = _prompt_value("QBO posting type", extracted.get("qbo_posting_type"))
    extracted["qbo_posting_type_override"] = extracted["qbo_posting_type"]
    extracted["needs_review"] = _prompt_bool("Needs review", extracted.get("needs_review"))
    extracted["manual_check"] = _prompt_bool("Manual allocation required", extracted.get("manual_check"))


def _edit_accounting_corrections(extracted: dict) -> None:
    extracted["human_override_decision"] = True
    print("  Posting action options: Create Bill, Create Expense, Create Vendor Credit, Attach To Existing Bill, Ignore, Reconciliation Only, Needs Human Review")
    extracted["posting_type"] = _prompt_value("Posting treatment", _display_posting_treatment(extracted))
    extracted["posting_action"] = extracted["posting_type"]
    print("  Payment status options: Paid, Unpaid, Partially Paid, Unknown")
    extracted["payment_status"] = _prompt_value("Payment status", _display_payment_status(extracted))
    print("  Payment method options: Cash, Credit Card, Debit Card, Bank Transfer, Cheque, Unknown")
    extracted["payment_method"] = _prompt_value("Payment method", _display_payment_method(extracted)).lower().replace(" ", "_")
    extracted["qbo_posting_type"] = _prompt_value("QBO posting type", extracted.get("qbo_posting_type"))
    extracted["qbo_posting_type_override"] = extracted["qbo_posting_type"]
    extracted["supplier_name"] = _prompt_value("Supplier/vendor", extracted.get("supplier_name"))
    lines = extracted.setdefault("line_items", [])
    if lines:
        line = lines[0]
        line["account_name"] = _prompt_value("Expense account", line.get("account_name"))
    else:
        account = _prompt_value("Expense account", "")
        if account:
            lines.append({"description": "Reviewed expense", "amount": extracted.get("total_amount"), "account_name": account})
    extracted["vat_treatment"] = _prompt_value("VAT treatment", extracted.get("vat_treatment"))
    extracted["branch"] = _prompt_value("Branch", extracted.get("branch"))
    extracted["qbo_class_name"] = _prompt_value("Class", extracted.get("qbo_class_name"))
    extracted["qbo_location_name"] = _prompt_value("Location", extracted.get("qbo_location_name"))
    extracted["notes"] = _prompt_value("Notes", extracted.get("notes"))
    if extracted.get("posting_type") == "Needs Human Review":
        extracted["needs_review"] = True
        extracted["review_reason"] = _prompt_value("Review reason", extracted.get("review_reason") or "Accounting treatment corrected for review.")


def _edit_transaction(extracted: dict) -> None:
    while True:
        print("\n  Edit: [A]ccounting  [L]ine  [C]lassification  [T]otal  [D]ate  [S]upplier  [B]rand/branch  [P]ayment  [Q]BO map  [N]ote  [Done]")
        raw = _read("  Edit choice > ")
        if raw is None:
            return
        choice = raw.lower()
        if choice in ("done", ""):
            return
        if choice == "a":
            _edit_accounting_corrections(extracted)
        elif choice == "l":
            line = _prompt_line(extracted.get("line_items") or [])
            if not line:
                print("  No valid line selected.")
                continue
            line["account_name"] = _prompt_value("Account", line.get("account_name"))
            line["amount"] = _prompt_value("Amount", line.get("amount"))
            line["description"] = _prompt_value("Description", line.get("description"))
        elif choice == "c":
            _edit_classification(extracted)
        elif choice == "t":
            extracted["total_amount"] = _prompt_value("Total amount", extracted.get("total_amount"))
        elif choice == "d":
            extracted["invoice_date"] = _prompt_value("Invoice date", extracted.get("invoice_date"))
        elif choice == "s":
            extracted["supplier_name"] = _prompt_value("Supplier", extracted.get("supplier_name"))
        elif choice == "b":
            extracted["brand"] = _prompt_value("Brand", extracted.get("brand"))
            extracted["business_unit"] = _prompt_value("Business unit", extracted.get("business_unit"))
            extracted["branch"] = _prompt_value("Branch", extracted.get("branch"))
            extracted["allocation_type"] = _prompt_value("Allocation type", extracted.get("allocation_type"))
        elif choice == "p":
            _edit_payment(extracted)
        elif choice == "q":
            extracted["qbo_class_name"] = _prompt_value("QBO class", extracted.get("qbo_class_name"))
            extracted["qbo_location_name"] = _prompt_value("QBO location", extracted.get("qbo_location_name"))
        elif choice == "n":
            extracted["notes"] = _prompt_value("Notes", extracted.get("notes"))
            extracted["review_reason"] = _prompt_value("Review reason", extracted.get("review_reason"))
        else:
            print("  Enter A, L, C, T, D, S, B, P, Q, N, or Done.")


def _refresh_safety(extracted: dict) -> dict:
    extracted = apply_qbo_mapping(extracted)
    extracted = apply_accounting_rules(extracted)
    extracted = apply_review_gate(extracted)
    return extracted


def run(extracted_results: list[dict] | None = None, reviewer_name: str | None = None) -> list[dict]:
    reviewer, reviewer_defaulted = resolve_reviewer_name(reviewer_name)
    if reviewer_defaulted:
        log.warning("Reviewer name not provided; using reviewer identity: %s", reviewer)
    decisions = _load_decisions()
    candidates = _build_candidates(extracted_results)
    pending = {
        k: v
        for k, v in candidates.items()
        if decisions.get(k) not in (APPROVED, SKIPPED, NEEDS_REVIEW, MANUAL_CHECK)
    }

    if pending and not sys.stdin.isatty():
        log.warning("Review needs input but stdin is not interactive; returning already-approved items only.")
        return [v for k, v in candidates.items() if decisions.get(k) == APPROVED]

    if not pending:
        log.info("No transactions pending review.")
        return [v for k, v in candidates.items() if decisions.get(k) == APPROVED]

    print(f"\n{'=' * 78}")
    print(f"REVIEW QUEUE - {len(pending)} transaction(s)")
    print(f"{'=' * 78}")

    total = len(pending)
    for index, (filename, extracted) in enumerate(pending.items(), 1):
        original_for_audit = copy.deepcopy(extracted)
        while True:
            extracted = _refresh_safety(extracted)
            _print_transaction(extracted, filename, index, total)
            decision = _prompt_decision()
            if decision == EDIT:
                _edit_transaction(extracted)
                extracted["_review_corrected"] = True
                extracted = _refresh_safety(extracted)
                _save_extracted(filename, extracted)
                continue
            if decision == QUIT:
                print("\nReview paused. Run again to continue.")
                break
            if decision == APPROVED and (extracted.get("needs_review") or extracted.get("manual_check")):
                print("  Cannot approve yet. Resolve review/manual flags first, or route it for review.")
                continue
            decisions[filename] = decision
            _save_decisions(decisions)
            if decision == APPROVED:
                append_review_decision(
                    reviewer_name=reviewer,
                    raw_decision=decision,
                    before=original_for_audit,
                    after=extracted,
                    filename=filename,
                )
                _save_extracted(filename, extracted)
                remember_correction(extracted)
            elif decision in (NEEDS_REVIEW, MANUAL_CHECK):
                extracted["needs_review"] = True
                if decision == MANUAL_CHECK:
                    extracted["manual_check"] = True
                extracted["review_reason"] = extracted.get("review_reason") or "Routed from human review."
                append_review_decision(
                    reviewer_name=reviewer,
                    raw_decision=decision,
                    before=original_for_audit,
                    after=extracted,
                    filename=filename,
                )
                _save_extracted(filename, extracted)
                copy_to_review_folder(extracted)
            if decision in (APPROVED, NEEDS_REVIEW, MANUAL_CHECK):
                _save_google_feedback(
                    original_for_audit,
                    extracted,
                    filename=filename,
                    decision=decision,
                    reviewer=reviewer,
                )
            status = {
                APPROVED: "APPROVED",
                NEEDS_REVIEW: "NEEDS REVIEW",
                MANUAL_CHECK: "MANUAL ALLOCATION REQUIRED",
                SKIPPED: "NEEDS REVIEW",
            }.get(decision, decision.upper())
            print(f"  -> {status}\n")
            break
        if decision == QUIT:
            break

    approved = [v for k, v in candidates.items() if decisions.get(k) == APPROVED]
    needs_review = sum(1 for k in candidates if decisions.get(k) in (NEEDS_REVIEW, SKIPPED))
    manual = sum(1 for k in candidates if decisions.get(k) == MANUAL_CHECK)
    still_pending = sum(
        1
        for k in candidates
        if decisions.get(k) not in (APPROVED, SKIPPED, NEEDS_REVIEW, MANUAL_CHECK)
    )

    print(
        f"\nSummary: {len(approved)} approved | {needs_review} needs review | "
        f"{manual} manual allocation | {still_pending} pending"
    )
    metrics = record_review_metrics(candidates, decisions)
    print(
        f"Metrics: {metrics['auto_approved_candidates']} auto-eligible | "
        f"{metrics['reviewed']} reviewed | {metrics['corrected']} corrected | "
        f"{metrics['rejected']} rejected"
    )
    feedback_summary = _feedback_summary(_load_feedback_records())
    print(
        "Feedback: "
        f"extraction accuracy {_format_accuracy(feedback_summary['extraction_accuracy'])} | "
        f"accounting treatment accuracy {_format_accuracy(feedback_summary['accounting_treatment_accuracy'])}"
    )
    extraction_top = feedback_summary["most_common_extraction_mistakes"][:3]
    accounting_top = feedback_summary["most_common_accounting_mistakes"][:3]
    print(
        "Feedback mistakes: "
        f"extraction {', '.join(f'{label} ({count})' for label, count in extraction_top) or 'none'} | "
        f"accounting {', '.join(f'{label} ({count})' for label, count in accounting_top) or 'none'}"
    )
    return approved


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    approved = run()
    print(f"\n{len(approved)} transaction(s) approved and ready for QBO posting.")
