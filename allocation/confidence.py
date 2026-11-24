from typing import Any

REVIEW_THRESHOLD = 0.90
MANUAL_THRESHOLD = 0.70


def _score_present(value: Any) -> float:
    return 1.0 if value not in (None, "", []) else 0.0


def _line_account_score(lines: list[dict]) -> float:
    if not lines:
        return 0.0
    scored = [1.0 if line.get("account_name") else 0.0 for line in lines]
    return sum(scored) / len(scored)


def _amount_score(extracted: dict) -> float:
    total = extracted.get("total_amount")
    lines = extracted.get("line_items") or []
    if total in (None, ""):
        return 0.0
    if not lines:
        return 0.75
    try:
        line_total = sum(float(line.get("amount") or 0) for line in lines)
        total_float = float(total)
    except (TypeError, ValueError):
        return 0.25
    return 1.0 if abs(line_total - total_float) <= 0.01 else 0.55


def ensure_confidence_fields(extracted: dict) -> dict:
    enriched = dict(extracted)
    lines = enriched.get("line_items") or []
    enriched.setdefault("extraction_confidence", 0.80)
    enriched.setdefault("vendor_confidence", _score_present(enriched.get("supplier_name")))
    enriched.setdefault("amount_confidence", _amount_score(enriched))
    enriched.setdefault("vat_confidence", 0.80 if enriched.get("tax_amount") in (None, "") else 0.90)
    enriched.setdefault("branch_confidence", _score_present(enriched.get("branch")))
    enriched.setdefault("account_confidence", _line_account_score(lines))

    components = [
        float(enriched.get("extraction_confidence") or 0.0),
        float(enriched.get("vendor_confidence") or 0.0),
        float(enriched.get("amount_confidence") or 0.0),
        float(enriched.get("vat_confidence") or 0.0),
        float(enriched.get("branch_confidence") or 0.0),
        float(enriched.get("account_confidence") or 0.0),
    ]
    enriched["overall_confidence"] = round(sum(components) / len(components), 4)
    return enriched


def apply_review_gate(extracted: dict) -> dict:
    enriched = ensure_confidence_fields(extracted)
    reasons = []
    allocation_type = enriched.get("allocation_type") or "unknown"

    if allocation_type == "unknown":
        reasons.append("Allocation type is unknown.")
    if allocation_type == "shared":
        reasons.append("Shared expense requires manual allocation.")
    if allocation_type == "branch" and not enriched.get("branch"):
        reasons.append("Branch is missing.")
    brand = (enriched.get("brand") or enriched.get("business_unit") or "").strip().lower()
    if brand not in {"", "head office", "shared"} and not enriched.get("branch"):
        reasons.append("Branch is missing.")
    if not enriched.get("brand") and not enriched.get("business_unit"):
        reasons.append("Business unit is missing.")
    if not enriched.get("qbo_class_name") or not enriched.get("qbo_location_name"):
        reasons.append("QBO class/location mapping is missing.")
    if enriched["overall_confidence"] < REVIEW_THRESHOLD:
        reasons.append(f"Overall confidence {enriched['overall_confidence']:.2f} is below 0.90.")

    if reasons:
        enriched["needs_review"] = True
        existing = (enriched.get("review_reason") or "").strip()
        joined = " | ".join(reasons)
        enriched["review_reason"] = f"{existing} | {joined}" if existing else joined
    else:
        enriched.setdefault("needs_review", False)
        enriched.setdefault("review_reason", "")

    if enriched["overall_confidence"] < MANUAL_THRESHOLD:
        enriched["manual_check"] = True
    else:
        enriched.setdefault("manual_check", False)
    return enriched
