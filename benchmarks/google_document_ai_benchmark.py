from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from document_processing.document_parser import (
    GoogleDocumentAIParserProvider,
    ProviderAdapterConfigurationError,
    ProviderAdapterNotEnabledError,
    ProviderApiConfirmationRequired,
    estimate_provider_cost,
    google_provider_configuration_status,
    load_google_provider_runtime_config,
)
from document_processing import document_parser

BASE_DIR = Path(__file__).resolve().parents[1]
RAW_DIR = BASE_DIR / "data/raw"
RESULTS_ROOT = BASE_DIR / "benchmarks/results"
DOCUMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
MAX_BENCHMARK_DOCS = 20
SUMMARY_FIELDS = [
    "file",
    "supplier",
    "invoice_number",
    "invoice_date",
    "subtotal",
    "vat_amount",
    "total",
    "trn",
    "line_item_count",
    "raw_text_chars",
    "max_confidence",
    "errors",
]


def _candidate_documents(max_docs: int | None = None) -> list[Path]:
    if not RAW_DIR.exists():
        return []
    limit = MAX_BENCHMARK_DOCS if max_docs is None else max(1, min(int(max_docs), MAX_BENCHMARK_DOCS))
    docs = sorted(
        path for path in RAW_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in DOCUMENT_EXTENSIONS
    )
    return docs[:limit]


def build_plan(*, max_docs: int | None = None, allow_api_calls: bool = False) -> dict:
    config = load_google_provider_runtime_config(allow_api_calls=allow_api_calls)
    status = google_provider_configuration_status(config)
    if status.get("credential_source") == "ADC":
        config.setdefault("providers", {}).setdefault("google", {})["adc_verified"] = True
    docs = _candidate_documents(max_docs)
    estimate = estimate_provider_cost(len(docs) or 1, provider_name="google", config=config)
    return {
        "provider": "google",
        "document_count": len(docs),
        "documents": [doc.name for doc in docs],
        "estimated_cost_aed": estimate["estimated_cost_aed"],
        "estimated_cost_usd": estimate["estimated_cost_usd"],
        "max_docs": MAX_BENCHMARK_DOCS,
        "primary_provider": config.get("primary_provider"),
        "config": config,
    }


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _safe_stem(path: Path) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in path.stem)[:90]


def _summary_row(file_name: str, normalized: dict, errors: list[str] | None = None) -> dict:
    fields = normalized.get("fields") or {}
    confidence_values = []
    for value in (normalized.get("confidence_by_field") or {}).values():
        try:
            confidence_values.append(float(value))
        except (TypeError, ValueError):
            continue
    return {
        "file": file_name,
        "supplier": fields.get("supplier_name") or "",
        "invoice_number": fields.get("invoice_number") or "",
        "invoice_date": fields.get("invoice_date") or "",
        "subtotal": fields.get("subtotal_amount") or "",
        "vat_amount": fields.get("tax_amount") or "",
        "total": fields.get("total_amount") or "",
        "trn": fields.get("supplier_trn") or "",
        "line_item_count": len(normalized.get("line_items") or []),
        "raw_text_chars": len(normalized.get("raw_text") or ""),
        "max_confidence": round(max(confidence_values), 4) if confidence_values else "",
        "errors": "; ".join(errors or normalized.get("errors") or []),
    }


def _write_summary(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _write_errors(path: Path, errors: list[dict]) -> None:
    with path.open("w") as handle:
        for error in errors:
            handle.write(json.dumps(error, sort_keys=True) + "\n")


def run_live_benchmark(
    plan: dict,
    *,
    adapter: GoogleDocumentAIParserProvider | None = None,
    timestamp: str | None = None,
) -> dict:
    config = plan["config"]
    readiness = document_parser.validate_provider_configuration("google", config=config, confirm_provider_api=True)
    results_dir = RESULTS_ROOT / f"google_{timestamp or _timestamp()}"
    rows: list[dict] = []
    errors: list[dict] = []
    result_files: list[str] = []
    if not readiness["ready"]:
        return {
            "blocked": True,
            "api_calls_made": False,
            "results_dir": None,
            "readiness": readiness,
            "errors": readiness["errors"],
            "result_files": [],
        }

    results_dir.mkdir(parents=True, exist_ok=True)
    adapter = adapter or GoogleDocumentAIParserProvider()
    for index, doc in enumerate(_candidate_documents(plan["document_count"]), 1):
        stem = f"{index:02d}_{_safe_stem(doc)}"
        try:
            normalized = adapter.parse(doc, config=config, confirm_provider_api=True)
            raw_response = (normalized.get("raw_provider_response") or {}).get("provider_response") or {}
            raw_path = results_dir / f"{stem}.raw_google_response.json"
            normalized_path = results_dir / f"{stem}.normalized.json"
            raw_path.write_text(json.dumps(raw_response, indent=2, sort_keys=True))
            normalized_path.write_text(json.dumps(normalized, indent=2, sort_keys=True))
            result_files.extend([str(raw_path), str(normalized_path)])
            rows.append(_summary_row(doc.name, normalized))
        except (
            ProviderApiConfirmationRequired,
            ProviderAdapterConfigurationError,
            ProviderAdapterNotEnabledError,
            OSError,
            RuntimeError,
        ) as exc:
            error = {"file": doc.name, "error": str(exc)}
            errors.append(error)
            rows.append(_summary_row(doc.name, {"fields": {}, "line_items": [], "raw_text": ""}, [str(exc)]))

    summary_path = results_dir / "summary.csv"
    errors_path = results_dir / "errors.jsonl"
    _write_summary(summary_path, rows)
    _write_errors(errors_path, errors)
    return {
        "blocked": False,
        "api_calls_made": bool(rows),
        "results_dir": str(results_dir),
        "readiness": readiness,
        "errors": errors,
        "result_files": result_files + [str(summary_path), str(errors_path)],
        "summary_csv": str(summary_path),
        "errors_file": str(errors_path),
    }


def print_plan(plan: dict, *, dry_run: bool, confirm_provider_api: bool, live_result: dict | None = None) -> None:
    live_readiness = document_parser.validate_provider_configuration(
        "google",
        config=plan["config"],
        confirm_provider_api=confirm_provider_api,
    )
    config_status = google_provider_configuration_status(plan["config"])
    api_calls_made = bool(live_result and live_result.get("api_calls_made"))
    print("\nGoogle Document AI invoice benchmark")
    print("  extraction testing only: no review, no accounting entries, no QBO posting")
    print("  provider API calls made:", "yes" if api_calls_made else "no")
    print("  provider API confirmation:", "requested" if confirm_provider_api else "not requested")
    print("  dry run:", "yes" if dry_run else "no")
    print("  selected documents:", plan["document_count"])
    print("  max documents:", plan["max_docs"])
    print("  primary provider:", plan.get("primary_provider") or "not selected")
    print("  estimated Google cost:", f"AED {plan['estimated_cost_aed']:.4f}")
    print("  provider configuration:", "loaded" if config_status["loaded"] else "incomplete")
    print("  project:", config_status.get("project_id") or "missing")
    print("  region:", config_status.get("location") or "missing")
    print("  processor:", config_status.get("processor_id") or "missing")
    print("  credential source:", config_status.get("credential_source") or "missing")
    print("\n  benchmark provider status:")
    print("  ✓ provider enabled" if config_status["provider_enabled"] else "  ✗ provider enabled")
    print("  ✓ provider API allowed" if config_status["provider_api_allowed"] else "  ✗ provider API allowed")
    print("  ✓ Google ready" if config_status["ready"] else "  ✗ Google ready")
    print("\n  files:")
    for index, name in enumerate(plan["documents"], 1):
        print(f"  {index}. {name}")
    print("\n  live call gate:")
    print("  provider API confirmation:", "requested" if confirm_provider_api else "not requested")
    for error in live_readiness["errors"]:
        if "Live provider API call not confirmed" not in error:
            print(f"    - {error}")
    if not confirm_provider_api:
        print("    - live provider API call not confirmed; dry-run made no API calls")
    if live_result and live_result.get("results_dir"):
        print("\n  results:")
        print("  folder:", live_result["results_dir"])
        print("  summary CSV:", live_result["summary_csv"])
        print("  errors file:", live_result["errors_file"])
    elif live_result and live_result.get("blocked"):
        print("\n  blocked:")
        for error in live_result.get("errors") or []:
            print(f"    - {error}")
    if config_status["missing"]:
        print("\n  missing setup values:")
        for item in config_status["missing"]:
            print(f"  {item}")


def run(
    *,
    dry_run: bool = False,
    max_docs: int | None = None,
    confirm_provider_api: bool = False,
    adapter: GoogleDocumentAIParserProvider | None = None,
) -> dict:
    allow_api_calls = bool(confirm_provider_api and not dry_run)
    plan = build_plan(max_docs=max_docs, allow_api_calls=allow_api_calls)
    live_result = None
    if not dry_run and confirm_provider_api:
        live_result = run_live_benchmark(plan, adapter=adapter)
    elif not dry_run and not confirm_provider_api:
        live_result = {
            "blocked": True,
            "api_calls_made": False,
            "results_dir": None,
            "errors": ["Live benchmark requires --confirm-provider-api."],
            "result_files": [],
        }
    print_plan(plan, dry_run=dry_run, confirm_provider_api=confirm_provider_api, live_result=live_result)
    return {
        "plan": plan,
        "live_result": live_result,
    }
