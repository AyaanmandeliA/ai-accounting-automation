import json
import re
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
BUSINESS_UNITS_FILE = BASE_DIR / "config/business_units.json"
BRANCH_CLASS_MAP_FILE = BASE_DIR / "config/branch_class_map.json"

ALLOCATION_BRANCH = "branch"
ALLOCATION_HEAD_OFFICE = "head_office"
ALLOCATION_SHARED = "shared"
ALLOCATION_UNKNOWN = "unknown"


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _normalise(value: Any) -> str:
    text = str(value or "").lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _parts_from_hint(path_hint: str | None) -> list[str]:
    if not path_hint:
        return []
    cleaned = str(path_hint).replace("\\", "/")
    return [_normalise(part) for part in cleaned.split("/") if _normalise(part)]


def _contains_alias(parts: list[str], aliases: list[str]) -> bool:
    haystack = " ".join(parts)
    for alias in aliases:
        alias_norm = _normalise(alias)
        if not alias_norm:
            continue
        if alias_norm in parts:
            return True
        if re.search(rf"(^|\s){re.escape(alias_norm)}($|\s)", haystack):
            return True
    return False


def _unique_branch_match(parts: list[str], brands: dict) -> tuple[str, str] | None:
    matches: list[tuple[str, str]] = []
    for brand_name, brand_data in brands.items():
        if brand_name in {"Head Office", "Shared"}:
            continue
        for branch_name, branch_aliases in (brand_data.get("branches") or {}).items():
            aliases = [branch_name, *branch_aliases]
            if _contains_alias(parts, aliases):
                matches.append((brand_name, branch_name))
    unique = sorted(set(matches))
    if len(unique) == 1:
        return unique[0]
    return None


def _branch_key(brand: str | None, branch: str | None, allocation_type: str | None) -> str | None:
    if not brand:
        return None
    if allocation_type == ALLOCATION_HEAD_OFFICE or brand == "Head Office":
        return "Head Office"
    if branch:
        return f"{brand}/{branch}"
    return brand


def resolve_from_folder_path(path_hint: str | None, config: dict | None = None) -> dict:
    config = config or _load_json(BUSINESS_UNITS_FILE)
    parts = _parts_from_hint(path_hint)
    result = {
        "brand": None,
        "business_unit": None,
        "branch": None,
        "allocation_type": ALLOCATION_UNKNOWN,
        "folder_path_hint": path_hint,
        "branch_confidence": 0.0,
        "confidence_reasons": [],
    }
    if not parts:
        result["confidence_reasons"].append("No folder path hint available.")
        return result

    brands = config.get("brands", {})
    for brand_name, brand_data in brands.items():
        aliases = [brand_name, *brand_data.get("aliases", [])]
        if not _contains_alias(parts, aliases):
            continue

        result["brand"] = brand_name
        result["business_unit"] = brand_name
        if brand_name == "Head Office":
            result["allocation_type"] = ALLOCATION_HEAD_OFFICE
            result["branch_confidence"] = 1.0
            result["confidence_reasons"].append("Folder path matched Head Office.")
            return result
        if brand_name == "Shared":
            result["allocation_type"] = ALLOCATION_SHARED
            result["branch_confidence"] = 0.85
            result["confidence_reasons"].append("Folder path matched Shared.")
            return result

        branches = brand_data.get("branches", {})
        for branch_name, branch_aliases in branches.items():
            aliases = [branch_name, *branch_aliases]
            if _contains_alias(parts, aliases):
                result["branch"] = branch_name
                result["allocation_type"] = ALLOCATION_BRANCH
                result["branch_confidence"] = 1.0
                result["confidence_reasons"].append(
                    f"Folder path matched {brand_name}/{branch_name}."
                )
                return result

        result["allocation_type"] = ALLOCATION_UNKNOWN
        result["branch_confidence"] = 0.55
        result["confidence_reasons"].append(
            f"Folder path matched {brand_name} but no configured branch."
        )
        return result

    branch_match = _unique_branch_match(parts, brands)
    if branch_match:
        brand_name, branch_name = branch_match
        result["brand"] = brand_name
        result["business_unit"] = brand_name
        result["branch"] = branch_name
        result["allocation_type"] = ALLOCATION_BRANCH
        result["branch_confidence"] = 0.9
        result["confidence_reasons"].append(
            f"Folder path uniquely matched branch {brand_name}/{branch_name}."
        )
        return result

    result["confidence_reasons"].append("Folder path did not match a configured business unit.")
    return result


def apply_folder_allocation(extracted: dict, folder_path_hint: str | None = None) -> dict:
    enriched = dict(extracted)
    hint = folder_path_hint or extracted.get("folder_path_hint") or extracted.get("source_folder_path")
    resolved = resolve_from_folder_path(hint)

    conflicts = []
    for key in ("brand", "branch", "allocation_type"):
        existing = enriched.get(key)
        new_value = resolved.get(key)
        if existing and new_value and _normalise(existing) != _normalise(new_value):
            conflicts.append(f"{key}: folder={new_value}, extracted={existing}")

    for key in ("brand", "business_unit", "branch", "allocation_type", "folder_path_hint"):
        if resolved.get(key) is not None:
            enriched[key] = resolved[key]
        else:
            enriched.setdefault(key, None if key != "allocation_type" else ALLOCATION_UNKNOWN)

    reasons = list(enriched.get("confidence_reasons") or [])
    reasons.extend(resolved.get("confidence_reasons") or [])
    if conflicts:
        reasons.append("Folder path conflicts with extracted values: " + "; ".join(conflicts))
    enriched["confidence_reasons"] = reasons
    enriched["branch_confidence"] = max(
        float(enriched.get("branch_confidence") or 0.0),
        float(resolved.get("branch_confidence") or 0.0),
    )
    if conflicts:
        enriched["needs_review"] = True
        enriched["review_reason"] = _append_reason(enriched.get("review_reason"), "Folder path conflict.")
    return enriched


def apply_qbo_mapping(extracted: dict) -> dict:
    enriched = dict(extracted)
    mapping = _load_json(BRANCH_CLASS_MAP_FILE)
    key = _branch_key(
        enriched.get("brand") or enriched.get("business_unit"),
        enriched.get("branch"),
        enriched.get("allocation_type"),
    )
    if not key:
        return enriched
    mapped = mapping.get(key)
    if not mapped:
        enriched["needs_review"] = True
        enriched["review_reason"] = _append_reason(
            enriched.get("review_reason"), f"Missing QBO class/location mapping for {key}."
        )
        return enriched
    enriched["qbo_class_name"] = mapped.get("class")
    enriched["qbo_location_name"] = mapped.get("location")
    return enriched


def _append_reason(existing: str | None, reason: str) -> str:
    existing = (existing or "").strip()
    if not existing:
        return reason
    if reason in existing:
        return existing
    return f"{existing} | {reason}"
