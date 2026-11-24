from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any


def _normalise(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _amount(value: Any) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _date_distance_days(left: Any, right: Any) -> int | None:
    left_date = _parse_date(left)
    right_date = _parse_date(right)
    if not left_date or not right_date:
        return None
    return abs((left_date - right_date).days)


def _text_blob(item: dict) -> str:
    parts = [
        item.get("supplier_name"),
        item.get("vendor"),
        item.get("invoice_number"),
        item.get("bank_reference"),
        item.get("email_subject"),
        item.get("email_body"),
        item.get("file_text"),
        item.get("notes"),
        item.get("duplicate_fingerprint"),
    ]
    return _normalise(" ".join(str(part or "") for part in parts))


def _score(payment_proof: dict, bill: dict) -> tuple[float, list[str]]:
    score = 0.0
    reasons = []
    proof_supplier = _normalise(payment_proof.get("supplier_name") or payment_proof.get("vendor"))
    bill_supplier = _normalise(bill.get("supplier_name") or bill.get("vendor"))
    proof_text = _text_blob(payment_proof)
    bill_text = _text_blob(bill)

    if proof_supplier and bill_supplier and (proof_supplier == bill_supplier or proof_supplier in bill_supplier or bill_supplier in proof_supplier):
        score += 0.30
        reasons.append("supplier matched")
    elif proof_supplier and proof_supplier in bill_text:
        score += 0.20
        reasons.append("supplier found in bill context")

    proof_amount = _amount(payment_proof.get("payment_amount") or payment_proof.get("total_amount") or payment_proof.get("amount"))
    bill_amount = _amount(bill.get("open_amount") or bill.get("total_amount") or bill.get("amount"))
    if proof_amount is not None and bill_amount is not None:
        if abs(proof_amount - bill_amount) <= 0.01:
            score += 0.25
            reasons.append("amount matched")
        elif 0 < proof_amount < bill_amount:
            score += 0.12
            reasons.append("partial payment amount")
        elif proof_amount > bill_amount:
            score += 0.06
            reasons.append("overpayment amount")

    invoice_number = _normalise(bill.get("invoice_number"))
    if invoice_number and invoice_number in proof_text:
        score += 0.20
        reasons.append("invoice number found")
    elif invoice_number and invoice_number in _normalise(payment_proof.get("payment_reference")):
        score += 0.20
        reasons.append("invoice number found in payment reference")

    distance = _date_distance_days(
        payment_proof.get("payment_date") or payment_proof.get("invoice_date"),
        bill.get("invoice_date") or bill.get("due_date"),
    )
    if distance is not None:
        if distance <= 7:
            score += 0.10
            reasons.append("payment date near invoice date")
        elif distance <= 45:
            score += 0.05
            reasons.append("payment date within 45 days")

    bank_ref = _normalise(payment_proof.get("bank_reference") or payment_proof.get("payment_reference"))
    if bank_ref and bank_ref in bill_text:
        score += 0.10
        reasons.append("bank reference matched")

    proof_thread = _normalise(payment_proof.get("email_thread_id") or payment_proof.get("thread_id"))
    bill_thread = _normalise(bill.get("email_thread_id") or bill.get("thread_id"))
    if proof_thread and bill_thread and proof_thread == bill_thread:
        score += 0.10
        reasons.append("email thread matched")

    proof_group = _normalise(payment_proof.get("attachment_group_id") or payment_proof.get("document_group_id"))
    bill_group = _normalise(bill.get("attachment_group_id") or bill.get("document_group_id"))
    if proof_group and bill_group and proof_group == bill_group:
        score += 0.10
        reasons.append("attachment group matched")

    fingerprint = payment_proof.get("duplicate_fingerprint")
    if fingerprint and fingerprint == bill.get("duplicate_fingerprint"):
        score += 0.20
        reasons.append("duplicate fingerprint matched")

    return min(score, 1.0), reasons


def suggest_payment_matches(
    payment_proof: dict,
    unpaid_bills: list[dict],
    *,
    high_confidence_threshold: float = 0.80,
) -> list[dict]:
    suggestions = []
    for bill in unpaid_bills:
        score, reasons = _score(payment_proof, bill)
        if score <= 0:
            continue
        requires_review = score < high_confidence_threshold
        suggestions.append({
            "match_confidence": round(score, 4),
            "matched_bill_candidate": bill,
            "match_reason": "; ".join(reasons),
            "requires_review": requires_review,
            "match_status": "Matched" if not requires_review else "Needs Review",
        })
    suggestions.sort(key=lambda item: item["match_confidence"], reverse=True)
    if len(suggestions) > 1 and suggestions[0]["match_confidence"] == suggestions[1]["match_confidence"]:
        suggestions[0]["requires_review"] = True
        suggestions[0]["match_status"] = "Needs Review"
        suggestions[0]["match_reason"] += "; ambiguous top match"
    return suggestions
