from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
SUPPLIER_TEMPLATES_FILE = BASE_DIR / "config/supplier_templates.json"


def load_supplier_templates(path: str | Path | None = None) -> list[dict]:
    template_path = Path(path) if path else SUPPLIER_TEMPLATES_FILE
    if not template_path.is_absolute():
        template_path = BASE_DIR / template_path
    if not template_path.exists():
        return []
    try:
        data = json.loads(template_path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    templates = data.get("templates", data if isinstance(data, list) else [])
    if not isinstance(templates, list):
        return []
    return [template for template in templates if isinstance(template, dict) and template.get("enabled", True)]


def _search(pattern: str | None, text: str) -> str | None:
    if not pattern:
        return None
    match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    if not match:
        return None
    return (match.group(1) if match.groups() else match.group(0)).strip()


def _search_last(pattern: str | None, text: str) -> str | None:
    if not pattern:
        return None
    matches = list(re.finditer(pattern, text, flags=re.IGNORECASE | re.MULTILINE))
    if not matches:
        return None
    match = matches[-1]
    return (match.group(1) if match.groups() else match.group(0)).strip()


def _money(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).replace(",", "").strip()
    try:
        return round(float(text), 2)
    except ValueError:
        return None


def _date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%m-%y", "%d/%m/%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _supplier_matches(template: dict, text: str, filename: str = "") -> bool:
    haystack = f"{filename}\n{text}"
    for pattern in template.get("supplier_name_patterns") or []:
        if re.search(str(pattern), haystack, flags=re.IGNORECASE):
            return True
    supplier = str(template.get("supplier_name") or "").strip()
    return bool(supplier and supplier.lower() in haystack.lower())


def _format_template(value: Any, fields: dict) -> str:
    text = str(value or "")

    class SafeFields(dict):
        def __missing__(self, key):
            return ""

    try:
        return text.format_map(SafeFields({key: "" if val is None else val for key, val in fields.items()}))
    except (KeyError, ValueError):
        return text


def _confidence(fields: dict, rules: dict | None = None) -> tuple[float, list[str]]:
    rules = rules or {}
    weights = rules.get("field_weights") or {
        "supplier_name": 0.2,
        "invoice_number": 0.2,
        "invoice_date": 0.2,
        "total_amount": 0.2,
        "tax_amount": 0.1,
        "supplier_trn": 0.1,
    }
    total_weight = sum(float(value or 0) for value in weights.values()) or 1.0
    score = 0.0
    missing = []
    for field, weight in weights.items():
        if fields.get(field) not in (None, "", []):
            score += float(weight or 0)
        else:
            missing.append(field)
    required_missing = [
        field for field in rules.get("required_fields", [])
        if fields.get(field) in (None, "", [])
    ]
    if required_missing:
        missing.extend(field for field in required_missing if field not in missing)
    return round(min(score / total_weight, 1.0), 4), missing


def _match_value(match: re.Match, spec: Any) -> str | None:
    if spec in (None, ""):
        return None
    try:
        return match.group(spec)
    except (IndexError, KeyError):
        return None


def _base_fields(text: str, template: dict) -> dict:
    return {
        "supplier_name": template.get("supplier_name"),
        "invoice_number": _search(template.get("invoice_number_regex"), text),
        "invoice_date": _date(_search(template.get("date_regex"), text)),
        "due_date": _date(_search(template.get("due_date_regex"), text)),
        "currency": template.get("currency") or "AED",
        "total_amount": _money(_search_last(template.get("total_regex"), text)),
        "tax_amount": _money(_search(template.get("vat_regex"), text)),
        "supplier_trn": _search(template.get("trn_regex"), text),
        "document_type": template.get("default_document_type") or "invoice",
    }


def _build_result(
    fields: dict,
    template: dict,
    *,
    source: Path | None = None,
    folder_path_hint: str | None = None,
    confidence: float,
    missing: list[str],
) -> dict:
    account = template.get("default_account")
    line_amount = fields.get(template.get("line_amount_field") or "total_amount")
    if line_amount is None:
        line_amount = fields.get("total_amount")
    line_items = []
    if line_amount is not None and account:
        line_items.append({
            "description": _format_template(
                template.get("default_line_description") or "{supplier_name} invoice",
                fields,
            ).strip(),
            "quantity": template.get("default_quantity"),
            "unit_price": line_amount if template.get("default_quantity") == 1 else None,
            "amount": line_amount,
            "account_name": account,
            "account_type": template.get("default_account_type"),
        })
    payment_confirmed = fields.get("payment_confirmed")
    if payment_confirmed is None:
        payment_confirmed = _bool(template.get("default_payment_confirmed"))
    result = {
        **fields,
        "business_unit": template.get("default_business_unit"),
        "brand": template.get("default_brand"),
        "branch": template.get("default_branch"),
        "allocation_type": template.get("default_allocation_type") or "unknown",
        "folder_path_hint": folder_path_hint,
        "line_items": line_items,
        "notes": _format_template(
            template.get("default_notes") or "Extracted locally using supplier template.",
            fields,
        ).strip(),
        "primary_source_file": source.name if source else None,
        "document_type": fields.get("document_type") or template.get("default_document_type") or "invoice",
        "service_period_start": fields.get("service_period_start"),
        "service_period_end": fields.get("service_period_end"),
        "accounting_treatment": template.get("default_accounting_treatment") or "supplier_bill_unpaid",
        "payment_method": template.get("default_payment_method") or "unpaid",
        "qbo_posting_type": template.get("default_posting_type") or "Bill",
        "payment_confirmed": payment_confirmed,
        "payment_date": fields.get("payment_date") or _date(template.get("default_payment_date")),
        "payment_amount": fields.get("payment_amount"),
        "payment_bank_account": fields.get("payment_bank_account") or template.get("default_payment_bank_account"),
        "extraction_method": "supplier_template",
        "template_id": template.get("id"),
        "template_confidence": confidence,
        "extraction_confidence": confidence,
        "vendor_confidence": 1.0 if fields.get("supplier_name") else 0.0,
        "amount_confidence": 1.0 if fields.get("total_amount") is not None else 0.0,
        "vat_confidence": 0.95 if fields.get("tax_amount") is not None or fields.get("supplier_trn") else 0.75,
        "account_confidence": 1.0 if account else 0.0,
        "confidence_reasons": [],
        "needs_review": False,
        "manual_check": False,
        "review_reason": "",
    }
    if fields.get("subtotal_amount") is not None:
        result["subtotal_amount"] = fields["subtotal_amount"]
    if fields.get("balance_amount") is not None:
        result["balance_amount"] = fields["balance_amount"]
    if missing:
        result["confidence_reasons"].append("Template missing fields: " + ", ".join(sorted(set(missing))))
    return result


def _extract_row_transactions(
    text: str,
    template: dict,
    *,
    source: Path | None = None,
    folder_path_hint: str | None = None,
) -> dict | None:
    row_regex = template.get("row_regex")
    row_fields = template.get("row_fields") or {}
    if not row_regex or not isinstance(row_fields, dict):
        return None
    matches = list(re.finditer(str(row_regex), text, flags=re.IGNORECASE | re.MULTILINE))
    if not matches:
        return None

    global_fields = _base_fields(text, template)
    payment_date = _date(_search(template.get("payment_date_regex"), text))
    payment_bank_account = template.get("default_payment_bank_account")
    transactions = []
    confidences = []
    missing_fields = []
    for match in matches:
        fields = dict(global_fields)
        for field_name, group_spec in row_fields.items():
            value = _match_value(match, group_spec)
            if field_name in {
                "subtotal_amount",
                "tax_amount",
                "total_amount",
                "payment_amount",
                "balance_amount",
            }:
                fields[field_name] = _money(value)
            elif field_name in {"invoice_date", "due_date", "payment_date"}:
                fields[field_name] = _date(value)
            else:
                fields[field_name] = value.strip() if isinstance(value, str) else value
        if payment_date and not fields.get("payment_date"):
            fields["payment_date"] = payment_date
        if payment_bank_account and not fields.get("payment_bank_account"):
            fields["payment_bank_account"] = payment_bank_account
        if template.get("default_payment_confirmed") is not None:
            fields["payment_confirmed"] = _bool(template.get("default_payment_confirmed"))
        confidence, missing = _confidence(fields, template.get("confidence_rules"))
        confidences.append(confidence)
        missing_fields.extend(missing)
        transactions.append(_build_result(
            fields,
            template,
            source=source,
            folder_path_hint=folder_path_hint,
            confidence=confidence,
            missing=missing,
        ))

    confidence = round(min(confidences), 4) if confidences else 0.0
    return {
        "transactions": transactions,
        "supplier_name": template.get("supplier_name"),
        "currency": template.get("currency") or "AED",
        "extraction_method": "supplier_template",
        "template_id": template.get("id"),
        "template_confidence": confidence,
        "extraction_confidence": confidence,
        "confidence_reasons": (
            ["Template missing fields: " + ", ".join(sorted(set(missing_fields)))]
            if missing_fields else []
        ),
    }


def match_supplier_template(text: str, templates: list[dict], *, filename: str = "") -> dict | None:
    for template in templates:
        if _supplier_matches(template, text, filename):
            return template
    return None


def extract_with_template(
    text: str,
    template: dict,
    *,
    source_file: str | Path | None = None,
    folder_path_hint: str | None = None,
) -> dict:
    source = Path(source_file) if source_file else None
    row_result = _extract_row_transactions(
        text,
        template,
        source=source,
        folder_path_hint=folder_path_hint,
    )
    if row_result:
        return row_result

    subtotal = _money(_search(template.get("subtotal_regex"), text))
    fields = _base_fields(text, template)
    fields["subtotal_amount"] = subtotal
    confidence, missing = _confidence(fields, template.get("confidence_rules"))
    return _build_result(
        fields,
        template,
        source=source,
        folder_path_hint=folder_path_hint,
        confidence=confidence,
        missing=missing,
    )


def extract_from_templates(
    text: str,
    *,
    source_file: str | Path | None = None,
    folder_path_hint: str | None = None,
    templates: list[dict] | None = None,
) -> dict | None:
    source = Path(source_file) if source_file else None
    templates = templates if templates is not None else load_supplier_templates()
    template = match_supplier_template(text, templates, filename=source.name if source else "")
    if not template:
        return None
    return extract_with_template(text, template, source_file=source, folder_path_hint=folder_path_hint)
