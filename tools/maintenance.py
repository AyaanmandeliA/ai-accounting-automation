from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
ARCHIVE_DIR = DATA_DIR / "archive"


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _archive_path(label: str) -> Path:
    return ARCHIVE_DIR / f"{label}_{_timestamp()}"


def _archive_root(label: str) -> Path:
    return ARCHIVE_DIR / f"{label}_{_timestamp()}"


def _archive_children(source: Path, label: str, dry_run: bool = False) -> list[str]:
    if not source.exists():
        return []
    items = [p for p in source.iterdir() if p.name != ".DS_Store"]
    if not items:
        return []
    destination = _archive_path(label)
    archived = []
    if not dry_run:
        destination.mkdir(parents=True, exist_ok=True)
    for item in items:
        archived.append(str(item.relative_to(BASE_DIR)))
        if not dry_run:
            shutil.move(str(item), destination / item.name)
    return archived


def _archive_into_root(source: Path, archive_root: Path, dry_run: bool = False) -> list[str]:
    if not source.exists():
        return []
    archived = [str(source.relative_to(BASE_DIR))]
    if dry_run:
        return archived

    destination = archive_root / source.relative_to(DATA_DIR)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), destination)
    return archived


def archive_old_raw_data(dry_run: bool = False) -> dict:
    if dry_run:
        archived = [str(p.relative_to(BASE_DIR)) for p in (DATA_DIR / "raw").iterdir()] if (DATA_DIR / "raw").exists() else []
    else:
        archived = _archive_children(DATA_DIR / "raw", "raw")
    return {"command": "archive_old_raw_data", "dry_run": dry_run, "archived": archived}


def archive_old_processed_data(dry_run: bool = False) -> dict:
    archived = []
    for name in ("processed", "extracted", "parsed"):
        if dry_run:
            folder = DATA_DIR / name
            archived.extend(str(p.relative_to(BASE_DIR)) for p in folder.iterdir()) if folder.exists() else None
        else:
            archived.extend(_archive_children(DATA_DIR / name, name))
    return {"command": "archive_old_processed_data", "dry_run": dry_run, "archived": archived}


def reset_review_queue(dry_run: bool = False) -> dict:
    archived = []
    review_decisions = DATA_DIR / "processed/review_decisions.json"
    if dry_run:
        review_dir = DATA_DIR / "review"
        if review_dir.exists():
            archived.extend(str(p.relative_to(BASE_DIR)) for p in review_dir.rglob("*") if p.is_file())
        if review_decisions.exists():
            archived.append(str(review_decisions.relative_to(BASE_DIR)))
        return {"command": "reset_review_queue", "dry_run": True, "archived": archived}

    archived.extend(_archive_children(DATA_DIR / "review", "review_queue"))
    if review_decisions.exists():
        destination = _archive_path("review_decisions")
        destination.mkdir(parents=True, exist_ok=True)
        shutil.move(str(review_decisions), destination / review_decisions.name)
        archived.append(str(review_decisions.relative_to(BASE_DIR)))
    (DATA_DIR / "review").mkdir(parents=True, exist_ok=True)
    review_decisions.parent.mkdir(parents=True, exist_ok=True)
    review_decisions.write_text(json.dumps({}, indent=2))
    return {"command": "reset_review_queue", "dry_run": False, "archived": archived}


def reset_test_data(dry_run: bool = False) -> dict:
    archived = []
    for name in ("raw", "parsed", "extracted", "review"):
        if dry_run:
            folder = DATA_DIR / name
            archived.extend(str(p.relative_to(BASE_DIR)) for p in folder.rglob("*") if p.is_file()) if folder.exists() else None
        else:
            archived.extend(_archive_children(DATA_DIR / name, f"test_reset_{name}"))
    return {"command": "reset_test_data", "dry_run": dry_run, "archived": archived}


def reset_sandbox_data(dry_run: bool = False, confirm: bool = False) -> dict:
    archive_root = _archive_root("sandbox_reset")
    targets = [
        DATA_DIR / "raw",
        DATA_DIR / "extracted",
        DATA_DIR / "review",
        DATA_DIR / "processed/ai_extracted.json",
        DATA_DIR / "processed/review_decisions.json",
    ]
    protected = [
        DATA_DIR / "processed/qbo_posted.json",
        BASE_DIR / "config",
        BASE_DIR / "tests",
    ]
    archived = []

    if not dry_run and not confirm:
        raise ValueError("reset_sandbox_data requires --dry-run or --confirm")

    for target in targets:
        archived.extend(_archive_into_root(target, archive_root, dry_run=dry_run))

    if not dry_run:
        for folder_name in ("raw", "extracted", "review", "processed"):
            (DATA_DIR / folder_name).mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "processed/ai_extracted.json").write_text(json.dumps({"version": None, "files": []}, indent=2))
        (DATA_DIR / "processed/review_decisions.json").write_text(json.dumps({}, indent=2))

    return {
        "command": "reset_sandbox_data",
        "dry_run": dry_run,
        "confirmed": confirm,
        "archive_root": str(archive_root.relative_to(BASE_DIR)),
        "archived": archived,
        "protected": [str(path.relative_to(BASE_DIR)) for path in protected],
    }


COMMANDS = {
    "archive_old_raw_data": archive_old_raw_data,
    "archive_old_processed_data": archive_old_processed_data,
    "reset_review_queue": reset_review_queue,
    "reset_test_data": reset_test_data,
    "reset_sandbox_data": reset_sandbox_data,
}


def run_command(command: str, dry_run: bool = False, confirm: bool = False) -> dict:
    if command not in COMMANDS:
        raise ValueError(f"Unknown maintenance command: {command}")
    if command == "reset_sandbox_data":
        result = COMMANDS[command](dry_run=dry_run, confirm=confirm)
    else:
        result = COMMANDS[command](dry_run=dry_run)
    print(f"\nMaintenance command: {command}")
    print(f"  dry run: {dry_run}")
    if result.get("archive_root"):
        print(f"  archive location: {result['archive_root']}")
    print(f"  items archived/planned: {len(result['archived'])}")
    for item in result["archived"][:20]:
        print(f"  - {item}")
    if len(result["archived"]) > 20:
        print(f"  ... {len(result['archived']) - 20} more")
    if result.get("protected"):
        print("  protected/not reset:")
        for item in result["protected"]:
            print(f"  - {item}")
    return result
