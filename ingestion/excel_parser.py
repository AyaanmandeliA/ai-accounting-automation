"""
Phase 1 — Excel/CSV Parser
Reads .xlsx, .xls, and .csv files from data/raw/ and extracts rows of
transaction data into a normalised list of dicts for Phase 2 AI extraction.
"""

import datetime as dt
import decimal
import json
import logging
import re
from pathlib import Path
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[1]
RAW_DIR = BASE_DIR / "data/raw"
PROCESSED_DIR = BASE_DIR / "data/processed"
PROCESSED_FILE = PROCESSED_DIR / "excel_parsed.json"
PARSED_OUTPUT_DIR = BASE_DIR / "data/parsed"

SPREADSHEET_EXTENSIONS = {".xlsx", ".xls", ".csv"}
MAX_ROWS = 5_000


def _load_processed() -> set[str]:
    if not PROCESSED_FILE.exists():
        return set()
    try:
        data = json.loads(PROCESSED_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read Excel parse ledger; starting fresh: %s", exc)
        return set()
    return set(data.get("files", []))


def _save_processed(processed: set[str]) -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PROCESSED_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"files": sorted(processed)}, indent=2))
    tmp.replace(PROCESSED_FILE)


def _clean_header(col: Any) -> str:
    if isinstance(col, tuple):
        col = " ".join(str(part) for part in col if str(part).strip())
    header = re.sub(r"\s+", " ", str(col).strip().lower())
    return header or "column"


def _dedupe_headers(columns) -> list[str]:
    seen = {}
    headers = []
    for col in columns:
        header = _clean_header(col)
        count = seen.get(header, 0)
        seen[header] = count + 1
        headers.append(header if count == 0 else f"{header}_{count + 1}")
    return headers


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, dict, tuple, set)):
        return False
    try:
        result = pd.isna(value)
    except (TypeError, ValueError):
        return False
    try:
        return bool(result)
    except (TypeError, ValueError):
        return False


def _clean_value(value: Any) -> Any:
    if _is_missing(value):
        return None
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (TypeError, ValueError):
            pass
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, float) and value == int(value):
        return int(value)
    return value


def _drop_blank_rows(df: pd.DataFrame) -> pd.DataFrame:
    cleaned = df.map(_clean_value)
    return cleaned.dropna(how="all")


def _read_spreadsheet(path: Path) -> list[dict]:
    ext = path.suffix.lower()
    try:
        if ext == ".csv":
            try:
                df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
            except UnicodeDecodeError:
                df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="cp1252")
        else:
            df = pd.read_excel(path, dtype=str, keep_default_na=False)
    except Exception as exc:
        raise ValueError(f"Could not read file {path.name}: {exc}") from exc

    if df.empty:
        log.info("Empty spreadsheet: %s", path.name)
        return []

    df.columns = _dedupe_headers(df.columns)
    df = _drop_blank_rows(df)

    if len(df) > MAX_ROWS:
        log.warning("Capping %s at %d non-blank rows (has %d)", path.name, MAX_ROWS, len(df))
        df = df.iloc[:MAX_ROWS]

    rows = []
    for _, row in df.iterrows():
        cleaned = {}
        for col, val in row.items():
            cleaned[col] = _clean_value(val)
        if any(v is not None for v in cleaned.values()):
            rows.append(cleaned)

    return rows


def parse_file(path: Path) -> dict:
    rows = _read_spreadsheet(path)
    return {
        "source_file": str(path.resolve()),
        "extension": path.suffix.lower(),
        "row_count": len(rows),
        "rows": rows,
    }


def run() -> list[dict]:
    PARSED_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    processed = _load_processed()

    if not RAW_DIR.exists():
        log.info("No data/raw/ directory found")
        return []

    candidates = [
        p for p in RAW_DIR.iterdir()
        if p.suffix.lower() in SPREADSHEET_EXTENSIONS and p.is_file()
    ]
    candidates = sorted(candidates, key=lambda p: p.name.lower())
    log.info("Found %d spreadsheet(s) in data/raw/", len(candidates))

    all_parsed = []
    for path in candidates:
        key = path.name
        if key in processed:
            log.info("Already parsed: %s", key)
            continue
        try:
            result = parse_file(path)
        except (ValueError, OSError) as exc:
            log.exception("Failed to parse %s: %s", path.name, exc)
            continue

        out_name = path.name + ".json"
        out_path = PARSED_OUTPUT_DIR / out_name
        tmp = out_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, indent=2, default=str))
        tmp.replace(out_path)

        processed.add(key)
        _save_processed(processed)
        log.info("Parsed %s → %d rows → %s", path.name, result["row_count"], out_path.name)
        all_parsed.append(result)

    log.info("Total files parsed this run: %d", len(all_parsed))
    return all_parsed


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    results = run()
    total_rows = sum(r["row_count"] for r in results)
    print(f"\nParsed {len(results)} file(s), {total_rows} total rows → data/parsed/")
