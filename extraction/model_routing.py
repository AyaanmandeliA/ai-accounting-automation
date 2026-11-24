from __future__ import annotations

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
MODEL_ROUTING_FILE = BASE_DIR / "config/model_routing.json"

DEFAULT_MODEL_ROUTING = {
    "default_model": "claude-sonnet-4-6",
    "routes": {
        "simple_invoice": {
            "model": "claude-sonnet-4-6",
            "enabled": False,
            "notes": "Future route for cheap-model extraction of clear one-page invoices.",
        },
        "complex_or_low_quality": {
            "model": "claude-sonnet-4-6",
            "enabled": False,
            "notes": "Future route for premium extraction of multi-page, low-quality, or ambiguous documents.",
        },
        "low_confidence_retry": {
            "model": "claude-sonnet-4-6",
            "enabled": False,
            "notes": "Future route for premium retry when the first pass has low confidence.",
        },
    },
}


def load_model_routing_config(path: Path | None = None) -> dict:
    config_path = path or MODEL_ROUTING_FILE
    if not config_path.exists():
        return DEFAULT_MODEL_ROUTING
    try:
        data = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError):
        return DEFAULT_MODEL_ROUTING
    if not isinstance(data, dict):
        return DEFAULT_MODEL_ROUTING

    merged = {
        **DEFAULT_MODEL_ROUTING,
        **data,
        "routes": {
            **DEFAULT_MODEL_ROUTING["routes"],
            **(data.get("routes") if isinstance(data.get("routes"), dict) else {}),
        },
    }
    if not str(merged.get("default_model") or "").strip():
        merged["default_model"] = DEFAULT_MODEL_ROUTING["default_model"]
    return merged


def default_extraction_model(path: Path | None = None) -> str:
    return str(load_model_routing_config(path).get("default_model"))
