from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from document_processing.document_parser import (
    MockDocumentParserProvider,
    normalized_provider_result,
)

FIELD_METRICS = (
    ("supplier", ("supplier_name", "supplier", "vendor_name", "vendor")),
    ("invoice_number", ("invoice_number", "invoice_id", "invoice_no", "receipt_number")),
    ("date", ("invoice_date", "date", "receipt_date")),
    ("subtotal", ("subtotal_amount", "subtotal", "net_amount")),
    ("vat", ("tax_amount", "vat_amount", "total_tax_amount", "vat")),
    ("total", ("total_amount", "invoice_total", "amount_due", "total")),
    ("trn", ("supplier_trn", "trn", "supplier_tax_id")),
)
CORE_ACCURACY_METRICS = (
    "supplier",
    "invoice_number",
    "date",
    "subtotal",
    "vat",
    "total",
    "trn",
    "line_items",
    "document_type",
    "payment_status",
    "confidence_quality",
)
REQUIRED_EXPECTED_FIELDS = (
    "supplier_name",
    "invoice_number",
    "invoice_date",
    "total_amount",
)
DISPLAY_PROVIDERS = (
    ("azure", "Azure"),
    ("google", "Google"),
    ("mistral", "Mistral"),
    ("gemini", "Gemini"),
    ("aws", "AWS"),
    ("local", "Local"),
)


def load_manifest(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def _field(mapping: dict, *names: str) -> Any:
    for name in names:
        value = mapping.get(name)
        if value not in (None, ""):
            return value
    return None


def _money(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return round(float(str(value).replace(",", "").strip()), 2)
    except (TypeError, ValueError):
        return None


def _canonical_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:10]


def _score_exact(actual: Any, expected: Any) -> float:
    if expected in (None, ""):
        return 1.0 if actual in (None, "") else 0.0
    return 1.0 if str(actual).strip().lower() == str(expected).strip().lower() else 0.0


def _score_money(actual: Any, expected: Any) -> float:
    expected_money = _money(expected)
    actual_money = _money(actual)
    if expected_money is None:
        return 1.0 if actual_money is None else 0.0
    if actual_money is None:
        return 0.0
    return 1.0 if abs(actual_money - expected_money) <= 0.004 else 0.0


def _score_date(actual: Any, expected: Any) -> float:
    return _score_exact(_canonical_date(actual), _canonical_date(expected))


def _normalise_line_item(item: dict) -> dict:
    return {
        "description": str(_field(item, "description", "item", "name") or "").strip().lower(),
        "amount": _money(_field(item, "amount", "line_amount", "total")),
    }


def _score_line_items(actual_items: list, expected_items: list) -> float:
    actual = [_normalise_line_item(item) for item in actual_items if isinstance(item, dict)]
    expected = [_normalise_line_item(item) for item in expected_items if isinstance(item, dict)]
    if not expected:
        return 1.0 if not actual else 0.0
    if len(actual) != len(expected):
        return 0.0
    for actual_item, expected_item in zip(actual, expected):
        if actual_item["description"] != expected_item["description"]:
            return 0.0
        if actual_item["amount"] != expected_item["amount"]:
            return 0.0
    return 1.0


def _confidence_value(confidence_by_field: dict, field_names: tuple[str, ...]) -> float | None:
    for name in field_names:
        value = confidence_by_field.get(name)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None
    return None


def _score_confidence_quality(provider_result: dict, expected: dict, metric_scores: dict[str, float]) -> float:
    confidence = provider_result.get("confidence_by_field") or {}
    if not confidence:
        return 0.0
    for metric, names in FIELD_METRICS:
        expected_key = {
            "supplier": "supplier_name",
            "date": "invoice_date",
            "vat": "tax_amount",
            "trn": "supplier_trn",
        }.get(metric, f"{metric}_amount" if metric in {"subtotal", "total"} else metric)
        if expected.get(expected_key) in (None, ""):
            continue
        claimed = _confidence_value(confidence, names)
        if metric_scores.get(metric) == 1.0 and (claimed is None or claimed < 0.8):
            return 0.0
        if metric_scores.get(metric) == 0.0 and claimed is not None and claimed >= 0.8:
            return 0.0
    return 1.0


def _total_reconciles(fields: dict) -> bool:
    subtotal = _money(_field(fields, "subtotal_amount", "subtotal", "net_amount"))
    vat = _money(_field(fields, "tax_amount", "vat_amount", "total_tax_amount", "vat"))
    total = _money(_field(fields, "total_amount", "invoice_total", "amount_due", "total"))
    if total is None:
        return False
    if subtotal is None or vat is None:
        return True
    return abs((subtotal + vat) - total) <= 0.01


def fallback_required(provider_result: dict, expected: dict | None = None) -> bool:
    fields = provider_result.get("fields") or {}
    if provider_result.get("errors"):
        return True
    if not _total_reconciles(fields):
        return True
    expected = expected or {}
    required = expected.get("required_fields") or REQUIRED_EXPECTED_FIELDS
    for field in required:
        if _field(fields, field) in (None, ""):
            return True
    return False


def score_provider_output(provider_result: dict, expected: dict) -> dict:
    normalised = normalized_provider_result(**{key: provider_result.get(key) for key in (
        "provider",
        "document_type",
        "raw_text",
        "fields",
        "line_items",
        "confidence_by_field",
        "page_count",
        "raw_provider_response",
        "needs_llm_normalization",
        "errors",
    )})
    fields = normalised["fields"]
    metric_scores = {}
    for metric, names in FIELD_METRICS:
        expected_key = {
            "supplier": "supplier_name",
            "date": "invoice_date",
            "vat": "tax_amount",
            "trn": "supplier_trn",
        }.get(metric, f"{metric}_amount" if metric in {"subtotal", "total"} else metric)
        actual = _field(fields, *names)
        expected_value = expected.get(expected_key)
        if metric in {"subtotal", "vat", "total"}:
            metric_scores[metric] = _score_money(actual, expected_value)
        elif metric == "date":
            metric_scores[metric] = _score_date(actual, expected_value)
        else:
            metric_scores[metric] = _score_exact(actual, expected_value)
    metric_scores["line_items"] = _score_line_items(normalised["line_items"], expected.get("line_items") or [])
    metric_scores["document_type"] = _score_exact(normalised["document_type"] or fields.get("document_type"), expected.get("document_type"))
    metric_scores["payment_status"] = _score_exact(fields.get("payment_confirmed"), expected.get("payment_confirmed"))
    metric_scores["confidence_quality"] = _score_confidence_quality(normalised, expected, metric_scores)
    fallback = fallback_required(normalised, expected)
    accuracy_values = [metric_scores[name] for name in CORE_ACCURACY_METRICS]
    raw_response = normalised.get("raw_provider_response") or {}
    return {
        "metrics": metric_scores,
        "overall_accuracy_score": round(sum(accuracy_values) / len(accuracy_values), 4),
        "fallback_required": fallback,
        "cost_estimate": raw_response.get("cost_estimate") or {},
        "speed_estimate_ms": raw_response.get("speed_estimate_ms"),
        "provider": normalised["provider"],
    }


def _providers_from_manifest(manifest: dict) -> list[str]:
    names = manifest.get("providers")
    if isinstance(names, list) and names:
        return [str(name).strip() for name in names if str(name).strip()]
    discovered = []
    for sample in manifest.get("samples") or []:
        outputs = sample.get("mock_provider_outputs") or {}
        discovered.extend(outputs.keys())
    return sorted(set(discovered))


def _normalise_provider_output(provider_name: str, output: dict) -> dict:
    return normalized_provider_result(provider=provider_name, **{key: value for key, value in output.items() if key != "provider"})


def run_benchmark(manifest: str | Path | dict, providers: dict[str, Any] | None = None) -> dict:
    manifest_data = load_manifest(manifest) if isinstance(manifest, (str, Path)) else manifest
    provider_names = _providers_from_manifest(manifest_data)
    providers = providers or {}
    rows = []
    summary = {
        name: {
            "sample_count": 0,
            "accuracy_score": 0.0,
            "fallback_rate": 0.0,
            "estimated_cost_aed": 0.0,
            "average_speed_ms": None,
        }
        for name in provider_names
    }
    speed_totals = {name: [] for name in provider_names}
    for sample in manifest_data.get("samples") or []:
        sample_id = sample.get("id") or sample.get("document_path") or "sample"
        expected = sample.get("expected") or {}
        mock_outputs = sample.get("mock_provider_outputs") or {}
        for provider_name in provider_names:
            if provider_name in providers:
                parsed = providers[provider_name].parse(
                    sample.get("document_path") or sample_id,
                    config={"mock_result": mock_outputs.get(provider_name, {})},
                )
            elif provider_name == "mock":
                parsed = MockDocumentParserProvider().parse(
                    sample.get("document_path") or sample_id,
                    config={"mock_result": mock_outputs.get(provider_name, {})},
                )
            else:
                parsed = _normalise_provider_output(provider_name, mock_outputs.get(provider_name, {}))
            scores = score_provider_output(parsed, expected)
            rows.append({
                "sample_id": sample_id,
                "provider": provider_name,
                "scores": scores,
                "provider_result": parsed,
            })
            provider_summary = summary[provider_name]
            provider_summary["sample_count"] += 1
            provider_summary["accuracy_score"] += scores["overall_accuracy_score"]
            provider_summary["fallback_rate"] += 1.0 if scores["fallback_required"] else 0.0
            provider_summary["estimated_cost_aed"] += _money((scores["cost_estimate"] or {}).get("estimated_cost_aed")) or 0.0
            if scores.get("speed_estimate_ms") is not None:
                speed_totals[provider_name].append(float(scores["speed_estimate_ms"]))
    for provider_name, provider_summary in summary.items():
        count = provider_summary["sample_count"] or 1
        provider_summary["accuracy_score"] = round(provider_summary["accuracy_score"] / count, 4)
        provider_summary["fallback_rate"] = round(provider_summary["fallback_rate"] / count, 4)
        provider_summary["estimated_cost_aed"] = round(provider_summary["estimated_cost_aed"], 4)
        speeds = speed_totals[provider_name]
        provider_summary["average_speed_ms"] = round(sum(speeds) / len(speeds), 2) if speeds else None
    return {
        "providers": provider_names,
        "samples": [sample.get("id") for sample in manifest_data.get("samples") or []],
        "results": rows,
        "summary": summary,
    }


def render_markdown_table(results: dict) -> str:
    summary = results.get("summary") or {}
    metric_rows = [
        ("Accuracy score", "accuracy_score"),
        ("Estimated cost AED", "estimated_cost_aed"),
        ("Fallback rate", "fallback_rate"),
        ("Average speed ms", "average_speed_ms"),
    ]
    lines = [
        "| Metric | Azure | Google | Mistral | Gemini | AWS | Local |",
        "| ------ | ----- | ------ | ------- | ------ | --- | ----- |",
    ]
    for label, key in metric_rows:
        cells = [label]
        for provider_key, _display in DISPLAY_PROVIDERS:
            value = (summary.get(provider_key) or {}).get(key)
            cells.append("" if value is None else str(value))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run offline provider benchmark from a mocked manifest.")
    parser.add_argument("manifest", help="Path to benchmark manifest JSON")
    parser.add_argument("--markdown", action="store_true", help="Print markdown comparison table")
    args = parser.parse_args()
    results = run_benchmark(args.manifest)
    if args.markdown:
        print(render_markdown_table(results))
    else:
        print(json.dumps(results["summary"], indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
