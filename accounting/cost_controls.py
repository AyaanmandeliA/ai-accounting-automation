from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
COST_CONTROLS_FILE = BASE_DIR / "config/cost_controls.json"
AI_USAGE_FILE = BASE_DIR / "data/processed/ai_usage.json"

AED_PER_USD = 3.6725
MODEL_PRICE_USD_PER_MILLION = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 0.8, "output": 4.0},
}


def load_cost_controls(path: Path | None = None) -> dict:
    config_path = path or COST_CONTROLS_FILE
    defaults = {
        "monthly_ai_cost_limit_aed": 100,
        "warn_at_percent": 70,
        "block_at_percent": 100,
        "extraction_run_cost_block_aed": 25,
    }
    if not config_path.exists():
        return defaults
    try:
        data = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError):
        return defaults
    return {**defaults, **data}


def _estimate_tokens_for_file(path: Path) -> int:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    # Conservative local estimate: OCR/document payloads are much larger than filename text.
    return max(1_500, int(size / 3) + 1_000)


def estimate_tokens_for_file(path: Path) -> int:
    return _estimate_tokens_for_file(path)


def estimate_tokens_for_text(text: str) -> int:
    # Local proxy only; Anthropic bills from its own tokenizer and media pipeline.
    return max(0, (len(text or "") + 3) // 4)


def estimate_model_cost(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> dict:
    prices = MODEL_PRICE_USD_PER_MILLION.get(model, MODEL_PRICE_USD_PER_MILLION["claude-sonnet-4-6"])
    cost_usd = (input_tokens / 1_000_000 * prices["input"]) + (output_tokens / 1_000_000 * prices["output"])
    return {
        "model": model,
        "estimated_input_tokens": int(input_tokens),
        "estimated_output_tokens": int(output_tokens),
        "estimated_cost_usd": round(cost_usd, 4),
        "estimated_cost_aed": round(cost_usd * AED_PER_USD, 4),
    }


def estimate_extraction_cost(
    document_groups: list[list[Path]],
    parsed_files: list[Path],
    *,
    model: str = "claude-sonnet-4-6",
    output_tokens_per_document: int = 1_200,
) -> dict:
    documents_processed = sum(len(group) for group in document_groups) + len(parsed_files)
    input_tokens = sum(_estimate_tokens_for_file(path) for group in document_groups for path in group)
    input_tokens += sum(_estimate_tokens_for_file(path) for path in parsed_files)
    output_tokens = max(documents_processed, 1) * output_tokens_per_document if documents_processed else 0
    cost = estimate_model_cost(model=model, input_tokens=input_tokens, output_tokens=output_tokens)
    return {
        "documents_processed": documents_processed,
        **cost,
    }


def _load_usage(path: Path | None = None) -> dict:
    usage_path = path or AI_USAGE_FILE
    if not usage_path.exists():
        return {"runs": []}
    try:
        data = json.loads(usage_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"runs": []}
    data.setdefault("runs", [])
    return data


def monthly_estimated_cost_aed(month: str | None = None, usage_path: Path | None = None) -> float:
    month = month or datetime.now(timezone.utc).strftime("%Y-%m")
    usage = _load_usage(usage_path)
    return round(
        sum(float(run.get("estimated_cost_aed") or 0) for run in usage.get("runs", []) if str(run.get("month")) == month),
        4,
    )


def cost_guard_status(
    estimate: dict,
    *,
    controls: dict | None = None,
    usage_path: Path | None = None,
) -> dict:
    controls = controls or load_cost_controls()
    limit = float(controls.get("monthly_ai_cost_limit_aed") or 0)
    current_month_cost = monthly_estimated_cost_aed(usage_path=usage_path)
    run_cost = float(estimate.get("estimated_cost_aed") or 0)
    projected = current_month_cost + run_cost
    percent = (projected / limit * 100) if limit else 0
    return {
        "run_total_cost_aed": round(run_cost, 4),
        "monthly_estimated_cost_aed": round(projected, 4),
        "monthly_limit_aed": limit,
        "percent_of_limit": round(percent, 2),
        "warn": bool(limit and percent >= float(controls.get("warn_at_percent", 70))),
        "block": bool(limit and percent >= float(controls.get("block_at_percent", 100))),
    }


def extraction_run_cost_guard(estimate: dict, *, controls: dict | None = None) -> dict:
    controls = controls or load_cost_controls()
    threshold = float(controls.get("extraction_run_cost_block_aed") or 0)
    run_cost = float(estimate.get("estimated_cost_aed") or 0)
    return {
        "run_total_cost_aed": round(run_cost, 4),
        "run_cost_block_aed": threshold,
        "block": bool(threshold and run_cost > threshold),
    }


def record_ai_usage(estimate: dict, usage_path: Path | None = None) -> dict:
    path = usage_path or AI_USAGE_FILE
    usage = _load_usage(path)
    now = datetime.now(timezone.utc)
    run = {
        "recorded_at": now.isoformat(timespec="seconds"),
        "month": now.strftime("%Y-%m"),
        **estimate,
    }
    usage.setdefault("runs", []).append(run)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(usage, indent=2))
    tmp.replace(path)
    return run
