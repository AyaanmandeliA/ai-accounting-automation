from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any


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


def _normalise(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def normalize_bank_transaction(row: dict) -> dict:
    debit = _amount(row.get("debit"))
    credit = _amount(row.get("credit"))
    amount = _amount(row.get("amount"))
    if amount is None:
        if debit is not None:
            amount = -abs(debit)
        elif credit is not None:
            amount = abs(credit)
    parsed = _parse_date(row.get("date"))
    return {
        "date": parsed.isoformat() if parsed else None,
        "description": row.get("description") or "",
        "amount": amount,
        "debit": debit,
        "credit": credit,
        "reference": row.get("reference"),
        "bank_account": row.get("bank_account"),
        "raw": row,
    }


def normalize_bank_transactions(rows: list[dict]) -> list[dict]:
    return [normalize_bank_transaction(row) for row in rows]


def _candidate_amount(candidate: dict) -> float | None:
    return _amount(candidate.get("payment_amount") or candidate.get("total_amount") or candidate.get("amount"))


def _score_match(bank_txn: dict, candidate: dict) -> tuple[float, list[str]]:
    score = 0.0
    reasons = []
    bank_amount = abs(_amount(bank_txn.get("amount")) or 0)
    candidate_amount = abs(_candidate_amount(candidate) or 0)
    if bank_amount and candidate_amount and abs(bank_amount - candidate_amount) <= 0.01:
        score += 0.35
        reasons.append("amount matched")
    bank_text = _normalise(" ".join(str(bank_txn.get(key) or "") for key in ("description", "reference")))
    supplier = _normalise(candidate.get("supplier_name") or candidate.get("vendor"))
    invoice = _normalise(candidate.get("invoice_number"))
    if supplier and supplier in bank_text:
        score += 0.25
        reasons.append("supplier found in bank text")
    if invoice and invoice in bank_text:
        score += 0.25
        reasons.append("invoice/reference found in bank text")
    bank_date = _parse_date(bank_txn.get("date"))
    candidate_date = _parse_date(candidate.get("payment_date") or candidate.get("invoice_date"))
    if bank_date and candidate_date:
        distance = abs((bank_date - candidate_date).days)
        if distance <= 3:
            score += 0.10
            reasons.append("date within 3 days")
        elif distance <= 30:
            score += 0.05
            reasons.append("date within 30 days")
    return min(score, 1.0), reasons


def suggest_bank_matches(bank_transactions: list[dict], candidates: list[dict]) -> list[dict]:
    suggestions = []
    for raw_txn in bank_transactions:
        bank_txn = normalize_bank_transaction(raw_txn)
        scored = []
        for candidate in candidates:
            score, reasons = _score_match(bank_txn, candidate)
            if score > 0:
                scored.append({
                    "match_confidence": round(score, 4),
                    "candidate": candidate,
                    "match_reason": "; ".join(reasons),
                })
        scored.sort(key=lambda item: item["match_confidence"], reverse=True)
        suggestions.append({
            "bank_transaction": bank_txn,
            "status": "matched_candidate" if scored else "unmatched",
            "requires_review": not scored or scored[0]["match_confidence"] < 0.80 or (len(scored) > 1 and scored[0]["match_confidence"] == scored[1]["match_confidence"]),
            "suggested_matches": scored,
        })
    return suggestions
