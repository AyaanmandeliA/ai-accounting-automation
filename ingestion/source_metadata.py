from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
SOURCE_METADATA_INDEX_FILE = PROCESSED_DIR / "source_metadata_index.json"


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default
    return data if isinstance(data, type(default)) else default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    tmp.replace(path)


def _normalise_raw_path(raw_path: Path | str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.resolve()


def _raw_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(BASE_DIR.resolve()))
    except ValueError:
        return str(path.resolve())


def _sidecar_path(raw_path: Path) -> Path:
    return raw_path.with_name(f"{raw_path.name}.metadata.json")


def _source_path_hint(metadata: dict) -> str:
    return (
        metadata.get("original_relative_source_path")
        or metadata.get("source_path_hint")
        or metadata.get("folder_path_hint")
        or metadata.get("folder_path")
        or ""
    )


def load_source_metadata_index() -> dict:
    data = _read_json(SOURCE_METADATA_INDEX_FILE, {})
    return data if isinstance(data, dict) else {}


def save_source_metadata_index(index: dict) -> None:
    _write_json(SOURCE_METADATA_INDEX_FILE, index)


def write_source_metadata(raw_path: Path | str, metadata: dict[str, Any]) -> dict:
    path = _normalise_raw_path(raw_path)
    payload = {
        **metadata,
        "raw_path": str(path),
        "raw_relative_path": _raw_relative(path),
        "raw_filename": path.name,
    }
    if not payload.get("source_path_hint"):
        payload["source_path_hint"] = _source_path_hint(payload)

    _write_json(_sidecar_path(path), payload)

    index = load_source_metadata_index()
    entries = index.setdefault("files", {})
    for key in {path.name, str(path), _raw_relative(path)}:
        entries[key] = payload
    _write_json(SOURCE_METADATA_INDEX_FILE, index)
    return payload


def read_source_metadata(raw_path: Path | str) -> dict:
    path = _normalise_raw_path(raw_path)
    sidecar = _sidecar_path(path)
    data = _read_json(sidecar, {})
    if data:
        return data

    entries = load_source_metadata_index().get("files") or {}
    for key in (str(path), _raw_relative(path), path.name):
        entry = entries.get(key)
        if isinstance(entry, dict):
            return entry
    return {}
