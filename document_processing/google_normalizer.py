from __future__ import annotations

import json
import re
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from document_processing.document_parser import normalized_provider_result

BASE_DIR = Path(__file__).resolve().parents[1]
GOOGLE_FEEDBACK_JSONL = BASE_DIR / "data/feedback/google_extraction_feedback.jsonl"

FIELD_ALIASES = {
    "supplier_name": ("supplier_name", "supplier", "vendor_name", "vendor", "supplier"),
    "invoice_number": ("invoice_number", "invoice_id", "invoice_no", "receipt_number", "bill_number"),
    "invoice_date": ("invoice_date", "date", "receipt_date", "bill_date"),
    "due_date": ("due_date", "payment_due_date"),
    "subtotal_amount": ("subtotal_amount", "subtotal", "net_amount", "amount_before_tax"),
    "tax_amount": ("tax_amount", "vat_amount", "total_tax_amount", "vat"),
    "total_amount": ("total_amount", "invoice_total", "amount_due", "total", "grand_total"),
    "supplier_trn": ("supplier_trn", "trn", "supplier_tax_id", "tax_id"),
    "currency": ("currency", "currency_code"),
    "payment_status": ("payment_status", "payment_confirmed", "paid"),
    "payment_method": ("payment_method",),
    "document_type": ("document_type",),
}

MONEY_FIELDS = {"subtotal_amount", "tax_amount", "total_amount", "payment_amount", "quantity", "unit_price", "amount"}
DATE_FIELDS = {"invoice_date", "due_date", "payment_date", "service_period_start", "service_period_end"}
TRN_RE = re.compile(r"\b\d{15}\b")
INVOICE_LABEL_RE = re.compile(r"^(?:(?:tax\s+)?invoice|inv|bill|receipt)(?:\s*(?:no|number))?[:#\s-]+|^(?:no|number|#)[:#\s-]+", re.I)
LEGAL_SUFFIX_RE = re.compile(
    r"\b(?:llc|l\.l\.c|ltd|co|company|trading|general|fze|fzco|dmcc|pjsc|sp|br|branch)\b",
    re.I,
)


def _clean_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).replace("\u00a0", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text or None


def normalize_currency(value: Any, raw_text: str = "") -> str:
    text = f"{value or ''} {raw_text or ''}".lower()
    if any(token in text for token in ("aed", "dirham", "dhs", "د.إ")):
        return "AED"
    match = re.search(r"\b[A-Z]{3}\b", str(value or ""))
    return match.group(0).upper() if match else "AED"


def normalize_money(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = str(value).strip()
    negative = "(" in text and ")" in text
    text = re.sub(r"(?i)\b(?:aed|dhs|dirhams?|د\.إ)\b", "", text)
    text = text.replace(",", "")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    amount = float(match.group(0))
    if negative:
        amount = -abs(amount)
    return round(amount, 2)


def normalize_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        date_value = value.get("dateValue") or value.get("date_value") or value
        try:
            year = int(date_value["year"])
            month = int(date_value["month"])
            day = int(date_value["day"])
            return f"{year:04d}-{month:02d}-{day:02d}"
        except (KeyError, TypeError, ValueError):
            return None
    text = str(value).strip()
    text = text.replace(".", "/")
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y", "%m/%d/%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text[:11].strip(), fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def normalize_invoice_number(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    text = INVOICE_LABEL_RE.sub("", text).strip()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^A-Za-z0-9/_\-.]", "", text)
    return text or None


def normalize_trn(value: Any, raw_text: str = "") -> str | None:
    text = f"{value or ''} {raw_text or ''}"
    match = TRN_RE.search(text)
    return match.group(0) if match else None


def _supplier_key(value: Any) -> str:
    text = str(value or "").lower()
    text = LEGAL_SUFFIX_RE.sub(" ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _supplier_candidates(suppliers: dict | None, supplier_memory: dict | None) -> list[str]:
    candidates: list[str] = []
    for key, value in (suppliers or {}).items():
        if isinstance(value, dict):
            for field in ("name", "company_name"):
                name = _clean_text(value.get(field))
                if name and name not in candidates:
                    candidates.append(name)
        elif key:
            candidates.append(str(key))
    memory_suppliers = (supplier_memory or {}).get("suppliers") if isinstance(supplier_memory, dict) else {}
    if isinstance(memory_suppliers, dict):
        for name in memory_suppliers:
            if name and name not in candidates:
                candidates.append(str(name))
    return candidates


def _feedback_supplier_aliases(feedback_path: Path = GOOGLE_FEEDBACK_JSONL) -> dict[str, str]:
    aliases: dict[str, str] = {}
    if not feedback_path.exists():
        return aliases
    for line in feedback_path.read_text().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        before = record.get("before") if isinstance(record.get("before"), dict) else {}
        after = record.get("after") if isinstance(record.get("after"), dict) else {}
        before_name = _clean_text(before.get("supplier_name"))
        after_name = _clean_text(after.get("supplier_name"))
        if before_name and after_name and before_name != after_name:
            aliases[_supplier_key(before_name)] = after_name
        for mistake in record.get("extraction_mistakes") or []:
            if not isinstance(mistake, dict) or mistake.get("field") != "supplier_name":
                continue
            wrong = _clean_text(mistake.get("before") or mistake.get("old") or before_name)
            correct = _clean_text(mistake.get("after") or mistake.get("new") or after_name)
            if wrong and correct:
                aliases[_supplier_key(wrong)] = correct
    return aliases


def normalize_supplier_name(
    value: Any,
    *,
    suppliers: dict | None = None,
    supplier_memory: dict | None = None,
    feedback_path: Path = GOOGLE_FEEDBACK_JSONL,
) -> tuple[str | None, float, str]:
    text = _clean_text(value)
    if not text:
        return None, 0.0, "missing"
    key = _supplier_key(text)
    aliases = _feedback_supplier_aliases(feedback_path)
    if key in aliases:
        return aliases[key], 1.0, "review_feedback"
    best_name = text
    best_score = 0.0
    for candidate in _supplier_candidates(suppliers, supplier_memory):
        candidate_key = _supplier_key(candidate)
        if not candidate_key:
            continue
        if key == candidate_key:
            return candidate, 1.0, "exact_supplier_memory"
        if key in candidate_key or candidate_key in key:
            score = min(0.98, max(len(key), len(candidate_key)) / max(1, len(key) + len(candidate_key) / 2))
        else:
            score = SequenceMatcher(None, key, candidate_key).ratio()
        if score > best_score:
            best_name = candidate
            best_score = score
    if best_score >= 0.72:
        return best_name, round(best_score, 4), "fuzzy_supplier_memory"
    return text, 0.0, "google_raw"


def _field_with_confidence(fields: dict, confidence: dict, canonical: str) -> tuple[Any, float | None, str | None]:
    for alias in FIELD_ALIASES.get(canonical, (canonical,)):
        value = fields.get(alias)
        if value not in (None, ""):
            conf = confidence.get(alias)
            try:
                conf_value = float(conf) if conf not in (None, "") else None
            except (TypeError, ValueError):
                conf_value = None
            return value, conf_value, alias
    return None, None, None


def _has_field_alias(fields: dict, canonical: str) -> bool:
    return any(alias in fields for alias in FIELD_ALIASES.get(canonical, (canonical,)))


def _normalize_confidence(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score > 1.0:
        score = score / 100.0
    return round(max(0.0, min(1.0, score)), 4)


def normalize_line_items(line_items: list[Any]) -> list[dict]:
    normalized = []
    for item in line_items or []:
        if not isinstance(item, dict):
            continue
        line = {}
        description = _clean_text(item.get("description") or item.get("line_item_description") or item.get("name"))
        if description:
            line["description"] = description
        for field in ("quantity", "unit_price", "amount"):
            value = normalize_money(item.get(field))
            if value is not None:
                line[field] = value
        confidence = _normalize_confidence(item.get("confidence"))
        if confidence is not None:
            line["confidence"] = confidence
        if line:
            normalized.append(line)
    return normalized


def normalize_google_provider_result(
    result: dict,
    *,
    suppliers: dict | None = None,
    supplier_memory: dict | None = None,
    feedback_path: Path = GOOGLE_FEEDBACK_JSONL,
) -> dict:
    base = normalized_provider_result(
        provider=result.get("provider") or "google",
        document_type=result.get("document_type") or "",
        raw_text=result.get("raw_text") or "",
        fields=result.get("fields") or {},
        line_items=result.get("line_items") or [],
        confidence_by_field=result.get("confidence_by_field") or {},
        page_count=result.get("page_count") or 0,
        raw_provider_response=result.get("raw_provider_response") or {},
        needs_llm_normalization=result.get("needs_llm_normalization") or False,
        errors=result.get("errors") or [],
    )
    fields = base["fields"]
    confidence = base["confidence_by_field"]
    canonical: dict[str, Any] = {}
    canonical_confidence: dict[str, float] = {}
    normalization_notes: list[str] = []

    supplier_value, supplier_conf, _ = _field_with_confidence(fields, confidence, "supplier_name")
    supplier_name, supplier_match_score, supplier_source = normalize_supplier_name(
        supplier_value,
        suppliers=suppliers,
        supplier_memory=supplier_memory,
        feedback_path=feedback_path,
    )
    if supplier_name:
        canonical["supplier_name"] = supplier_name
        base_conf = _normalize_confidence(supplier_conf) or 0.0
        if supplier_match_score:
            canonical_confidence["supplier_name"] = round(max(base_conf, supplier_match_score), 4)
            normalization_notes.append(f"supplier matched by {supplier_source}")
        elif base_conf:
            canonical_confidence["supplier_name"] = base_conf

    for canonical_field in ("invoice_number", "invoice_date", "due_date", "subtotal_amount", "tax_amount", "total_amount", "supplier_trn", "currency", "payment_status", "payment_method", "document_type"):
        value, conf, _ = _field_with_confidence(fields, confidence, canonical_field)
        if canonical_field == "invoice_number":
            normalized = normalize_invoice_number(value)
        elif canonical_field in DATE_FIELDS:
            normalized = normalize_date(value)
        elif canonical_field in MONEY_FIELDS:
            normalized = normalize_money(value)
        elif canonical_field == "supplier_trn":
            normalized = normalize_trn(value, base["raw_text"])
        elif canonical_field == "currency":
            normalized = normalize_currency(value, base["raw_text"])
        else:
            normalized = _clean_text(value)
        if normalized not in (None, ""):
            canonical[canonical_field] = normalized
            conf_value = _normalize_confidence(conf)
            if conf_value is not None:
                canonical_confidence[canonical_field] = conf_value

    if "currency" not in canonical:
        canonical["currency"] = normalize_currency(None, base["raw_text"])
    if "supplier_trn" not in canonical:
        trn = normalize_trn(None, base["raw_text"])
        if trn:
            canonical["supplier_trn"] = trn
            canonical_confidence.setdefault("supplier_trn", 0.75)

    line_items = normalize_line_items(base["line_items"])
    if not canonical.get("total_amount") and line_items and not _has_field_alias(fields, "total_amount"):
        line_total = round(sum(float(line.get("amount") or 0.0) for line in line_items), 2)
        if line_total:
            canonical["total_amount"] = line_total
            canonical_confidence["total_amount"] = max(canonical_confidence.get("total_amount", 0.0), 0.55)
            normalization_notes.append("total inferred from line items")
    if canonical.get("tax_amount") is None and canonical.get("subtotal_amount") is not None and canonical.get("total_amount") is not None:
        inferred_tax = round(float(canonical["total_amount"]) - float(canonical["subtotal_amount"]), 2)
        if inferred_tax >= 0:
            canonical["tax_amount"] = inferred_tax
            canonical_confidence["tax_amount"] = max(canonical_confidence.get("tax_amount", 0.0), 0.65)
            normalization_notes.append("VAT inferred from subtotal and total")

    normalized_result = {
        **base,
        "fields": canonical,
        "line_items": line_items,
        "confidence_by_field": canonical_confidence,
        "normalization": {
            "engine": "google_normalizer",
            "notes": normalization_notes,
            "original_fields": fields,
            "original_confidence_by_field": confidence,
        },
    }
    if not normalized_result["document_type"]:
        normalized_result["document_type"] = str(canonical.get("document_type") or "invoice")
    return normalized_result
