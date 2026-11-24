from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]

INVOICE_KEYWORDS = {
    "invoice",
    "bill",
    "tax invoice",
    "trn",
    "vat",
    "amount",
    "total",
    "date",
    "supplier",
    "due date",
}
AMOUNT_RE = re.compile(r"\b(?:AED|USD|د\.إ)?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?\b", re.IGNORECASE)
DATE_RE = re.compile(r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2})\b")
TRN_RE = re.compile(r"\b\d{15}\b")


def _run_text_command(command: list[str], timeout: int = 15) -> tuple[str, str | None]:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", str(exc)
    if result.returncode != 0:
        return "", (result.stderr or result.stdout or f"exit {result.returncode}").strip()
    return result.stdout or "", None


def _pdf_page_count(path: Path) -> tuple[int | None, str | None]:
    output, error = _run_text_command(["pdfinfo", str(path)], timeout=10)
    if error:
        return None, error
    for line in output.splitlines():
        if line.lower().startswith("pages:"):
            try:
                return int(line.split(":", 1)[1].strip()), None
            except ValueError:
                return None, "Could not parse pdfinfo page count"
    return None, "pdfinfo did not report page count"


def score_text_quality(text: str, *, min_text_chars: int = 500) -> tuple[float, list[str]]:
    warnings: list[str] = []
    clean = " ".join((text or "").split())
    lower = clean.lower()
    score = 0.0

    length_ratio = min(len(clean) / max(min_text_chars, 1), 1.0)
    score += length_ratio * 0.35
    if len(clean) < min_text_chars:
        warnings.append(f"text shorter than {min_text_chars} chars")

    keyword_hits = [keyword for keyword in INVOICE_KEYWORDS if keyword in lower]
    score += min(len(keyword_hits) / 5, 1.0) * 0.35
    if not keyword_hits:
        warnings.append("no invoice-like keywords detected")

    field_hits = 0
    if DATE_RE.search(clean):
        field_hits += 1
    else:
        warnings.append("no date-like value detected")
    if AMOUNT_RE.search(clean):
        field_hits += 1
    else:
        warnings.append("no amount-like value detected")
    if TRN_RE.search(clean) or "trn" in lower or "vat" in lower:
        field_hits += 1
    else:
        warnings.append("no VAT/TRN signal detected")
    score += (field_hits / 3) * 0.30

    return round(min(score, 1.0), 4), warnings


def extract_text_preview(
    file_path: str | Path,
    *,
    min_text_chars: int = 500,
    max_chars: int = 20_000,
) -> dict[str, Any]:
    path = Path(file_path)
    warnings: list[str] = []
    page_count: int | None = None

    if path.suffix.lower() != ".pdf":
        return {
            "text": "",
            "quality_score": 0.0,
            "page_count": None,
            "method": "unsupported_non_pdf",
            "warnings": ["local text extraction supports PDFs only"],
        }

    page_count, page_error = _pdf_page_count(path)
    if page_error:
        warnings.append(page_error)

    text, error = _run_text_command(["pdftotext", "-layout", "-nopgbrk", str(path), "-"], timeout=20)
    if error:
        warnings.append(error)
        return {
            "text": "",
            "quality_score": 0.0,
            "page_count": page_count,
            "method": "pdftotext_failed",
            "warnings": warnings,
        }

    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars]
        warnings.append(f"text preview truncated to {max_chars} chars")

    quality_score, quality_warnings = score_text_quality(text, min_text_chars=min_text_chars)
    warnings.extend(quality_warnings)
    method = "text_extract_ok" if quality_score >= 0.65 and len(text) >= min_text_chars else "text_extract_low_quality"
    return {
        "text": text,
        "quality_score": quality_score,
        "page_count": page_count,
        "method": method,
        "warnings": warnings,
    }

