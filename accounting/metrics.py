from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from accounting.persistence import record_metrics

BASE_DIR = Path(__file__).resolve().parents[1]
METRICS_FILE = BASE_DIR / "data/processed/review_metrics.json"


def _load_metrics() -> dict:
    if not METRICS_FILE.exists():
        return {
            "total_transactions": 0,
            "auto_approved_candidates": 0,
            "automatic_decisions": 0,
            "reviewed": 0,
            "corrected": 0,
            "rejected": 0,
            "manual_corrections": 0,
            "review_required": 0,
            "unknown_decisions": 0,
            "correction_percentage": 0.0,
            "review_percentage": 0.0,
            "unknown_percentage": 0.0,
            "runs": [],
        }
    try:
        data = json.loads(METRICS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {
            "total_transactions": 0,
            "auto_approved_candidates": 0,
            "automatic_decisions": 0,
            "reviewed": 0,
            "corrected": 0,
            "rejected": 0,
            "manual_corrections": 0,
            "review_required": 0,
            "unknown_decisions": 0,
            "correction_percentage": 0.0,
            "review_percentage": 0.0,
            "unknown_percentage": 0.0,
            "runs": [],
        }
    data.setdefault("runs", [])
    return data


def _is_auto_approved_candidate(item: dict) -> bool:
    decision = item.get("accounting_decision") or {}
    try:
        confidence = float(decision.get("confidence", item.get("overall_confidence") or 0))
    except (TypeError, ValueError):
        confidence = 0.0
    return not item.get("needs_review") and not item.get("manual_check") and not decision.get("requires_review") and confidence >= 0.90


def record_review_metrics(candidates: dict[str, dict], decisions: dict[str, str]) -> dict:
    reviewed_count = sum(1 for key in candidates if key in decisions)
    corrected_count = sum(
        1
        for item in candidates.values()
        if item.get("human_override_decision") or item.get("_review_corrected")
    )
    review_required_count = sum(
        1
        for item in candidates.values()
        if item.get("needs_review") or item.get("manual_check") or (item.get("accounting_decision") or {}).get("requires_review")
    )
    unknown_count = sum(
        1
        for item in candidates.values()
        if (item.get("accounting_decision") or {}).get("document_type") == "Unknown"
        or (item.get("accounting_decision") or {}).get("posting_action") == "Unknown"
    )
    run = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "total_transactions": len(candidates),
        "auto_approved_candidates": sum(1 for item in candidates.values() if _is_auto_approved_candidate(item)),
        "automatic_decisions": sum(1 for item in candidates.values() if not (item.get("accounting_decision") or {}).get("requires_review")),
        "reviewed": reviewed_count,
        "corrected": corrected_count,
        "rejected": sum(
            1
            for key in candidates
            if decisions.get(key) in {"needs_review", "manual_check", "skipped"}
        ),
        "manual_corrections": corrected_count,
        "review_required": review_required_count,
        "unknown_decisions": unknown_count,
        "correction_percentage": round(corrected_count / reviewed_count, 4) if reviewed_count else 0.0,
        "review_percentage": round(review_required_count / len(candidates), 4) if candidates else 0.0,
        "unknown_percentage": round(unknown_count / len(candidates), 4) if candidates else 0.0,
    }
    metrics = _load_metrics()
    for key in (
        "total_transactions",
        "auto_approved_candidates",
        "automatic_decisions",
        "reviewed",
        "corrected",
        "rejected",
        "manual_corrections",
        "review_required",
        "unknown_decisions",
    ):
        metrics[key] = int(metrics.get(key, 0)) + run[key]
    metrics["correction_percentage"] = round(metrics["manual_corrections"] / metrics["reviewed"], 4) if metrics["reviewed"] else 0.0
    metrics["review_percentage"] = round(metrics["review_required"] / metrics["total_transactions"], 4) if metrics["total_transactions"] else 0.0
    metrics["unknown_percentage"] = round(metrics["unknown_decisions"] / metrics["total_transactions"], 4) if metrics["total_transactions"] else 0.0
    metrics["runs"].append(run)
    METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = METRICS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(metrics, indent=2))
    tmp.replace(METRICS_FILE)
    record_metrics("review_run", run)
    return run
