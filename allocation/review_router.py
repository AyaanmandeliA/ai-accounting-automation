import json
import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
REVIEW_ROOT = BASE_DIR / "data/review"


def _safe_part(value: str | None, fallback: str) -> str:
    text = str(value or "").strip() or fallback
    for char in '<>:"\\|?*':
        text = text.replace(char, "_")
    return text.strip(" .") or fallback


def review_bucket(extracted: dict) -> str:
    if extracted.get("manual_check") or float(extracted.get("overall_confidence") or 0) < 0.70:
        return "Manual Allocation Required"
    return "Needs Review"


def review_folder_for(extracted: dict) -> Path:
    brand = _safe_part(extracted.get("brand") or extracted.get("business_unit"), "Unknown")
    branch = _safe_part(extracted.get("branch"), "Head Office" if brand == "Head Office" else "Unknown Branch")
    return REVIEW_ROOT / brand / branch / review_bucket(extracted)


def copy_to_review_folder(extracted: dict) -> Path | None:
    if not extracted.get("needs_review") and not extracted.get("manual_check"):
        return None

    folder = review_folder_for(extracted)
    folder.mkdir(parents=True, exist_ok=True)
    sources = extracted.get("all_source_files") or [extracted.get("source_file")]
    copied = []
    for raw in sources:
        if not raw:
            continue
        source = Path(raw)
        if not source.exists():
            continue
        destination = folder / source.name
        if not destination.exists():
            shutil.copy2(source, destination)
        copied.append(str(destination.resolve()))

    stem = Path(extracted.get("extracted_file") or extracted.get("source_file") or "invoice").name
    extraction_path = folder / f"{stem}.review_extraction.json"
    reason_path = folder / f"{stem}.review_reason.json"
    extraction_path.write_text(json.dumps(extracted, indent=2, default=str))
    reason_path.write_text(json.dumps({
        "review_reason": extracted.get("review_reason"),
        "needs_review": extracted.get("needs_review"),
        "manual_check": extracted.get("manual_check"),
        "overall_confidence": extracted.get("overall_confidence"),
        "copied_files": copied,
    }, indent=2, default=str))
    return folder

