from __future__ import annotations

import json
from pathlib import Path

from document_processing.document_parser import (
    GoogleDocumentAIParserProvider,
    ProviderAdapterConfigurationError,
    ProviderAdapterNotEnabledError,
    ProviderApiConfirmationRequired,
    estimate_provider_cost,
    load_provider_config,
    validate_provider_configuration,
)

BASE_DIR = Path(__file__).resolve().parents[1]
RAW_DIR = BASE_DIR / "data/raw"
RESULTS_DIR = BASE_DIR / "benchmarks/provider_results"
DOCUMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
MAX_LIVE_BENCHMARK_DOCS = 20
PROVIDER_ADAPTERS = {
    "google": GoogleDocumentAIParserProvider,
}


def _candidate_documents(max_docs: int | None = None) -> list[Path]:
    if not RAW_DIR.exists():
        return []
    docs = sorted(
        path for path in RAW_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in DOCUMENT_EXTENSIONS
    )
    return docs[: max_docs or 20]


def _provider_names(config: dict, requested: str | None = None) -> list[str]:
    providers = config.get("providers") or {}
    names = [name for name in providers if name != "mock"]
    if requested:
        return [requested] if requested in providers else [requested]
    return names


def _benchmark_doc_limit(max_docs: int | None = None) -> int:
    if max_docs is None:
        return MAX_LIVE_BENCHMARK_DOCS
    return max(1, min(int(max_docs), MAX_LIVE_BENCHMARK_DOCS))


def _provider_readiness(config: dict, provider_names: list[str], *, confirm_provider_api: bool) -> dict:
    return {
        name: validate_provider_configuration(
            name,
            config=config,
            confirm_provider_api=confirm_provider_api,
        )
        for name in provider_names
    }


def build_benchmark_plan(
    *,
    provider: str | None = None,
    max_docs: int | None = None,
    config: dict | None = None,
) -> dict:
    config = config or load_provider_config()
    docs = _candidate_documents(_benchmark_doc_limit(max_docs))
    provider_names = _provider_names(config, provider)
    provider_estimates = {}
    for name in provider_names:
        total = 0.0
        for _doc in docs:
            total += estimate_provider_cost(1, provider_name=name, config=config)["estimated_cost_aed"]
        provider_estimates[name] = round(total, 4)
    return {
        "dry_run_only": True,
        "document_count": len(docs),
        "documents": [doc.name for doc in docs],
        "providers": provider_names,
        "provider_estimates_aed": provider_estimates,
        "primary_provider": config.get("primary_provider"),
        "requires_benchmark_before_primary_provider": bool(config.get("require_benchmark_before_primary_provider", True)),
        "max_live_benchmark_docs": MAX_LIVE_BENCHMARK_DOCS,
    }


def run_confirmed_provider_benchmark(plan: dict, *, config: dict | None = None) -> dict:
    config = config or load_provider_config()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    run_result = {
        "live_provider_api_calls_requested": True,
        "document_count": min(int(plan.get("document_count") or 0), MAX_LIVE_BENCHMARK_DOCS),
        "results_dir": str(RESULTS_DIR),
        "providers": {},
    }
    documents = [RAW_DIR / name for name in plan.get("documents", [])[:MAX_LIVE_BENCHMARK_DOCS]]
    for provider_name in plan.get("providers", []):
        readiness = validate_provider_configuration(
            provider_name,
            config=config,
            confirm_provider_api=True,
        )
        provider_report = {
            "ready": readiness["ready"],
            "api_call_attempted": False,
            "errors": readiness["errors"],
            "warnings": readiness["warnings"],
            "result_files": [],
        }
        adapter_cls = PROVIDER_ADAPTERS.get(provider_name)
        if adapter_cls is None:
            provider_report["ready"] = False
            provider_report["errors"].append(f"{provider_name} has no live benchmark adapter implemented.")
            run_result["providers"][provider_name] = provider_report
            continue
        if not readiness["ready"]:
            run_result["providers"][provider_name] = provider_report
            continue
        adapter = adapter_cls()
        for index, doc in enumerate(documents, 1):
            try:
                provider_report["api_call_attempted"] = True
                parsed = adapter.parse(doc, config=config, confirm_provider_api=True)
            except (ProviderApiConfirmationRequired, ProviderAdapterConfigurationError, ProviderAdapterNotEnabledError) as exc:
                provider_report["ready"] = False
                provider_report["errors"].append(str(exc))
                break
            output = RESULTS_DIR / f"{provider_name}_{index:02d}_{doc.stem}.provider_result.json"
            output.write_text(json.dumps(parsed, indent=2, sort_keys=True))
            provider_report["result_files"].append(str(output))
        run_result["providers"][provider_name] = provider_report
    return run_result


def print_benchmark_plan(plan: dict, *, confirm_provider_api: bool = False, readiness: dict | None = None, live_results: dict | None = None) -> None:
    print("\nProvider benchmark plan")
    result_file_count = sum(
        len(report.get("result_files") or [])
        for report in (live_results or {}).get("providers", {}).values()
    )
    api_call_attempted = any(
        bool(report.get("api_call_attempted"))
        for report in (live_results or {}).get("providers", {}).values()
    )
    print("  provider API calls made:", "yes" if api_call_attempted or result_file_count else "no")
    print("  provider API confirmation:", "requested" if confirm_provider_api else "not requested")
    print("  max live benchmark documents:", plan["max_live_benchmark_docs"])
    print("  selected documents:", plan["document_count"])
    print("  primary provider:", plan.get("primary_provider") or "not selected")
    print("  benchmark required before primary provider:", "yes" if plan["requires_benchmark_before_primary_provider"] else "no")
    print("\n  documents to benchmark:")
    for index, name in enumerate(plan["documents"], 1):
        print(f"  {index}. {name}")
    print("\n  estimated provider cost for this benchmark:")
    for provider, cost in plan["provider_estimates_aed"].items():
        print(f"  {provider}: AED {cost:.4f}")
    if readiness:
        print("\n  provider readiness:")
        for provider, report in readiness.items():
            status = "ready" if report["ready"] else "not ready"
            print(f"  {provider}: {status}")
            for error in report["errors"]:
                print(f"    - {error}")
            for warning in report["warnings"]:
                print(f"    - warning: {warning}")
    if live_results:
        print("\n  confirmed benchmark results:")
        for provider, report in live_results["providers"].items():
            print(f"  {provider}: {len(report['result_files'])} normalized result files")
            for error in report["errors"]:
                print(f"    - {error}")
    print("\n  next step: fill ground-truth expected fields, then run mocked benchmark scoring.")


def run(
    *,
    dry_run: bool = False,
    provider: str | None = None,
    max_docs: int | None = None,
    confirm_provider_api: bool = False,
) -> dict:
    config = load_provider_config()
    plan = build_benchmark_plan(provider=provider, max_docs=max_docs, config=config)
    readiness = _provider_readiness(config, plan["providers"], confirm_provider_api=confirm_provider_api)
    live_results = None
    if confirm_provider_api and not dry_run:
        live_results = run_confirmed_provider_benchmark(plan, config=config)
    print_benchmark_plan(plan, confirm_provider_api=confirm_provider_api, readiness=readiness, live_results=live_results)
    if live_results:
        plan["live_results"] = live_results
    plan["provider_readiness"] = readiness
    return plan
