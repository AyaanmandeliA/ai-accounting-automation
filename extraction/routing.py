from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from document_processing.template_extractor import extract_from_templates, load_supplier_templates

ROUTE_NO_AI_TEMPLATE = "no_ai_template"
ROUTE_LOCAL_TEXT_RULES = "local_text_rules"
ROUTE_CLAUDE_REQUIRED = "claude_required"
ROUTE_NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True)
class RoutingDecision:
    route: str
    confidence: float
    reason: str
    template_id: str | None = None
    extracted: dict | None = None


def _minimum_confidence(extracted: dict) -> float:
    return float(extracted.get("template_confidence") or extracted.get("overall_confidence") or 0.0)


def _template_minimum(extracted: dict, templates: list[dict]) -> float:
    template_id = extracted.get("template_id")
    for template in templates:
        if template.get("id") == template_id:
            return float((template.get("confidence_rules") or {}).get("minimum_confidence") or 0.9)
    return 0.9


def route_document_group(
    paths: list[Path],
    *,
    text_preview: dict | None = None,
    folder_path_hint: str | None = None,
    templates: list[dict] | None = None,
) -> RoutingDecision:
    if not paths:
        return RoutingDecision(ROUTE_NEEDS_REVIEW, 0.0, "No source document provided.")
    text_preview = text_preview or {}
    text = str(text_preview.get("text") or "")
    if not text.strip():
        return RoutingDecision(ROUTE_CLAUDE_REQUIRED, 0.0, "No local text available for template extraction.")

    templates = templates if templates is not None else load_supplier_templates()
    extracted = extract_from_templates(
        text,
        source_file=paths[0],
        folder_path_hint=folder_path_hint,
        templates=templates,
    )
    if extracted:
        confidence = _minimum_confidence(extracted)
        minimum = _template_minimum(extracted, templates)
        if confidence >= minimum:
            return RoutingDecision(
                ROUTE_NO_AI_TEMPLATE,
                confidence,
                f"Supplier template {extracted.get('template_id')} matched with high confidence.",
                template_id=extracted.get("template_id"),
                extracted=extracted,
            )
        return RoutingDecision(
            ROUTE_CLAUDE_REQUIRED,
            confidence,
            f"Supplier template {extracted.get('template_id')} confidence {confidence:.2f} below {minimum:.2f}.",
            template_id=extracted.get("template_id"),
            extracted=extracted,
        )

    if float(text_preview.get("quality_score") or 0.0) >= 0.8:
        return RoutingDecision(ROUTE_LOCAL_TEXT_RULES, float(text_preview.get("quality_score")), "Good local text but no supplier template matched.")
    return RoutingDecision(ROUTE_CLAUDE_REQUIRED, float(text_preview.get("quality_score") or 0.0), "No supplier template matched.")


def decision_to_dict(decision: RoutingDecision) -> dict[str, Any]:
    return {
        "route": decision.route,
        "confidence": decision.confidence,
        "reason": decision.reason,
        "template_id": decision.template_id,
        "extracted": decision.extracted,
    }
