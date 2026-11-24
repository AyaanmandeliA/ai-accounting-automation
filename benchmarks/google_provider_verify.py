from __future__ import annotations

from typing import Callable

from document_processing.document_parser import (
    google_document_ai_bearer_token,
    google_adc_credentials_available,
    get_provider_settings,
    load_google_provider_runtime_config,
    validate_provider_configuration,
)


def _processor_url(settings: dict) -> str:
    return (
        f"https://{settings['location']}-documentai.googleapis.com/v1/"
        f"projects/{settings['project_id']}/locations/{settings['location']}/"
        f"processors/{settings['processor_id']}"
    )


def _default_http_get(url: str, *, headers: dict, timeout: int):
    import requests

    return requests.get(url, headers=headers, timeout=timeout)


def _line(ok: bool, text: str, detail: str = "") -> str:
    marker = "✓" if ok else "✗"
    return f"{marker} {text}{(': ' + detail) if detail else ''}"


def verify(
    *,
    config: dict | None = None,
    adc_checker: Callable[[], bool] = google_adc_credentials_available,
    token_loader: Callable[[dict], str | None] = google_document_ai_bearer_token,
    http_get: Callable = _default_http_get,
) -> dict:
    runtime_config = config or load_google_provider_runtime_config(allow_api_calls=True)
    settings = get_provider_settings("google", runtime_config)
    readiness = validate_provider_configuration(
        "google",
        config=runtime_config,
        confirm_provider_api=True,
    )
    checks = {
        "adc_authenticated": False,
        "project_loaded": bool(settings.get("project_id")),
        "region_loaded": bool(settings.get("location")),
        "processor_loaded": bool(settings.get("processor_id")),
        "document_ai_reachable": False,
        "credentials_valid": False,
    }
    errors: list[str] = []
    checks["adc_authenticated"] = bool(adc_checker())
    if not checks["adc_authenticated"]:
        errors.append("Application Default Credentials were not found or could not be loaded.")
    errors.extend(readiness["errors"])

    token = None
    if checks["project_loaded"] and checks["region_loaded"] and checks["processor_loaded"] and checks["adc_authenticated"]:
        try:
            token = token_loader(settings)
            checks["credentials_valid"] = bool(token)
        except Exception as exc:
            errors.append(str(exc))
    if token:
        try:
            response = http_get(
                _processor_url(settings),
                headers={"Authorization": f"Bearer {token}"},
                timeout=int(settings.get("timeout_seconds") or 60),
            )
            status_code = getattr(response, "status_code", None)
            if hasattr(response, "raise_for_status"):
                response.raise_for_status()
            data = response.json() if hasattr(response, "json") else {}
            checks["document_ai_reachable"] = status_code in (None, 200)
            checks["processor_loaded"] = checks["processor_loaded"] and bool(
                isinstance(data, dict) and str(settings["processor_id"]) in str(data.get("name", ""))
            )
        except Exception as exc:
            errors.append(str(exc))

    return {
        "checks": checks,
        "errors": errors,
        "settings": {
            "project_id": settings.get("project_id"),
            "location": settings.get("location"),
            "processor_id": settings.get("processor_id"),
        },
    }


def print_verification(result: dict) -> None:
    checks = result["checks"]
    settings = result["settings"]
    print("\nGoogle Document AI provider verification")
    print("  no invoices processed")
    print(_line(checks["adc_authenticated"], "ADC authenticated"))
    print(_line(checks["project_loaded"], "Project loaded", str(settings.get("project_id") or "")))
    print(_line(checks["region_loaded"], "Region loaded", str(settings.get("location") or "")))
    print(_line(checks["processor_loaded"], "Processor loaded", str(settings.get("processor_id") or "")))
    print(_line(checks["document_ai_reachable"], "Document AI reachable"))
    print(_line(checks["credentials_valid"], "Credentials valid"))
    if result.get("errors"):
        print("\nErrors:")
        for error in result["errors"]:
            print(f"  - {error}")


def run() -> dict:
    result = verify()
    print_verification(result)
    return result
