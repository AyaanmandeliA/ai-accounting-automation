from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
SUPPLIER_STATEMENTS_DIR = BASE_DIR / "data/reconciliation/supplier_statements"


def _amount(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def classify_supplier_statement(extracted: dict) -> dict:
    text = " ".join(
        str(extracted.get(key) or "")
        for key in ("document_type", "document_category", "notes", "file_text")
    ).lower()
    is_statement = "statement" in text or extracted.get("document_category") == "Supplier Statement"
    return {
        "document_category": "Supplier Statement" if is_statement else extracted.get("document_category", "Unknown"),
        "qbo_posting_type": "None",
        "posting_type": "Reconciliation Only" if is_statement else "Review Required",
        "not_postable": True,
        "requires_reconciliation_review": True,
    }


def extract_supplier_statement(data: dict) -> dict:
    lines = data.get("line_items") or data.get("statement_lines") or []
    invoice_numbers = set(data.get("invoice_numbers") or [])
    credit_notes = []
    payments = []
    normalised_lines = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        description = str(line.get("description") or "")
        reference = line.get("reference") or line.get("invoice_number")
        if reference:
            invoice_numbers.add(str(reference))
        if "credit" in description.lower() or str(line.get("type") or "").lower() in {"credit", "credit note"}:
            credit_notes.append(line)
        if "payment" in description.lower() or str(line.get("type") or "").lower() == "payment":
            payments.append(line)
        normalised_lines.append({
            "date": line.get("date"),
            "description": description,
            "reference": reference,
            "debit": _amount(line.get("debit")),
            "credit": _amount(line.get("credit")),
            "amount": _amount(line.get("amount")),
        })

    text = str(data.get("file_text") or "")
    for match in re.findall(r"\b(?:INV|TAX|CN|CR)[-\s]?[A-Z0-9-]{3,}\b", text, re.IGNORECASE):
        invoice_numbers.add(match)

    summary = {
        **classify_supplier_statement(data),
        "supplier": data.get("supplier_name") or data.get("supplier"),
        "statement_date": data.get("statement_date") or data.get("invoice_date"),
        "opening_balance": _amount(data.get("opening_balance")),
        "closing_balance": _amount(data.get("closing_balance") or data.get("total_amount")),
        "line_items": normalised_lines,
        "invoice_numbers": sorted(invoice_numbers),
        "credit_notes": credit_notes,
        "payments": payments,
        "statement_vs_books_summary": {
            "status": "framework_only",
            "matched_in_books": [],
            "missing_from_books": [],
            "book_items_missing_from_statement": [],
            "difference_amount": None,
        },
    }
    return summary


def compare_supplier_statement_to_books(statement: dict, book_items: list[dict]) -> dict:
    statement_refs = [str(ref) for ref in statement.get("invoice_numbers") or [] if ref]
    statement_ref_set = set(statement_refs)
    duplicate_refs = sorted({ref for ref in statement_refs if statement_refs.count(ref) > 1})
    book_ref_map = {
        str(item.get("invoice_number") or item.get("reference")): item
        for item in book_items
        if item.get("invoice_number") or item.get("reference")
    }
    missing_from_books = sorted(ref for ref in statement_ref_set if ref not in book_ref_map)
    book_items_missing_from_statement = sorted(ref for ref in book_ref_map if ref not in statement_ref_set)
    credit_note_refs = sorted(
        str(item.get("reference") or item.get("invoice_number"))
        for item in statement.get("credit_notes") or []
        if item.get("reference") or item.get("invoice_number")
    )
    statement_balance = _amount(statement.get("closing_balance"))
    books_balance = sum(
        _amount(item.get("open_amount") or item.get("balance") or item.get("total_amount")) or 0.0
        for item in book_items
    )
    difference = None
    if statement_balance is not None:
        difference = round(statement_balance - books_balance, 2)
    return {
        "status": "needs_review" if missing_from_books or book_items_missing_from_statement or duplicate_refs or difference not in (None, 0) else "matched",
        "matched_in_books": sorted(statement_ref_set & set(book_ref_map)),
        "missing_from_books": missing_from_books,
        "book_items_missing_from_statement": book_items_missing_from_statement,
        "duplicate_statement_references": duplicate_refs,
        "credit_note_references": credit_note_refs,
        "books_open_balance": round(books_balance, 2),
        "statement_closing_balance": statement_balance,
        "difference_amount": difference,
    }


def store_supplier_statement(statement: dict, output_dir: Path | None = None) -> Path:
    target_dir = output_dir or SUPPLIER_STATEMENTS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    supplier = re.sub(r"[^A-Za-z0-9._-]+", "_", str(statement.get("supplier") or "unknown_supplier")).strip("_")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = target_dir / f"{stamp}_{supplier or 'unknown_supplier'}.json"
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(statement, indent=2, default=str))
    tmp.replace(out_path)
    return out_path
