from __future__ import annotations

import copy
import base64
import json
import mimetypes
import os
from pathlib import Path
from typing import Any, Protocol

BASE_DIR = Path(__file__).resolve().parents[1]
PROVIDER_CONFIG_FILE = BASE_DIR / "config/extraction_providers.json"
GOOGLE_PROVIDER_SECRETS_FILE = BASE_DIR / "config/secrets.local/google_document_ai.json"

DEFAULT_PROVIDER_CONFIG = {
    "default_strategy": "benchmark_first",
    "enabled_providers": ["mock"],
    "primary_provider": None,
    "fallback_provider": "claude",
    "require_benchmark_before_primary_provider": True,
    "target_claude_fallback_rate": 0.05,
    "max_claude_fallback_rate": 0.1,
    "placeholder_provider_estimate_aed_per_doc": 0.25,
    "providers": {
        "azure": {
            "display_name": "Azure Document Intelligence",
            "enabled": False,
            "requires_credentials": True,
            "api_calls_enabled": False,
            "estimated_cost_aed_per_page": 0.20,
        },
        "google": {
            "display_name": "Google Document AI Invoice Parser",
            "enabled": False,
            "requires_credentials": True,
            "api_calls_enabled": False,
            "estimated_cost_aed_per_page": 0.04,
            "project_id": None,
            "location": None,
            "processor_id": None,
            "bearer_token_env_var": "GOOGLE_DOCUMENT_AI_BEARER_TOKEN",
            "credentials_path_env_var": "GOOGLE_APPLICATION_CREDENTIALS",
            "credentials_path": None,
        },
        "mistral": {
            "display_name": "Mistral OCR / Document AI",
            "enabled": False,
            "requires_credentials": True,
            "api_calls_enabled": False,
            "estimated_cost_aed_per_page": 0.05,
        },
        "gemini": {
            "display_name": "Gemini Flash",
            "enabled": False,
            "requires_credentials": True,
            "api_calls_enabled": False,
            "estimated_cost_aed_per_page": 0.03,
        },
        "aws": {
            "display_name": "Amazon Textract",
            "enabled": False,
            "requires_credentials": True,
            "api_calls_enabled": False,
            "estimated_cost_aed_per_page": 0.06,
        },
        "local": {
            "display_name": "Local OCR / Text Extraction",
            "enabled": False,
            "requires_credentials": False,
            "api_calls_enabled": False,
            "estimated_cost_aed_per_page": 0.0,
        },
        "mock": {
            "display_name": "Mock Provider",
            "enabled": True,
            "requires_credentials": False,
            "api_calls_enabled": False,
            "estimated_cost_aed_per_page": 0.0,
        },
    },
}

PROVIDER_RESULT_KEYS = (
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
)


class DocumentParserProvider(Protocol):
    name: str

    def parse(
        self,
        file_path: str | Path,
        *,
        text_preview: dict | None = None,
        config: dict | None = None,
        confirm_provider_api: bool = False,
    ) -> dict:
        ...

    def estimate_cost(self, page_count: int, *, config: dict | None = None) -> dict:
        ...


def normalized_provider_result(
    *,
    provider: str = "",
    document_type: str = "",
    raw_text: str = "",
    fields: dict | None = None,
    line_items: list | None = None,
    confidence_by_field: dict | None = None,
    page_count: int = 0,
    raw_provider_response: dict | None = None,
    needs_llm_normalization: bool = False,
    errors: list | None = None,
) -> dict:
    return {
        "provider": provider or "",
        "document_type": document_type or "",
        "raw_text": raw_text or "",
        "fields": fields if isinstance(fields, dict) else {},
        "line_items": line_items if isinstance(line_items, list) else [],
        "confidence_by_field": confidence_by_field if isinstance(confidence_by_field, dict) else {},
        "page_count": int(page_count or 0),
        "raw_provider_response": raw_provider_response if isinstance(raw_provider_response, dict) else {},
        "needs_llm_normalization": bool(needs_llm_normalization),
        "errors": errors if isinstance(errors, list) else [],
    }


def load_provider_config(path: str | Path | None = None) -> dict:
    config_path = Path(path) if path else PROVIDER_CONFIG_FILE
    if not config_path.is_absolute():
        config_path = BASE_DIR / config_path
    if not config_path.exists():
        return copy.deepcopy(DEFAULT_PROVIDER_CONFIG)
    try:
        data = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError):
        return copy.deepcopy(DEFAULT_PROVIDER_CONFIG)
    if not isinstance(data, dict):
        return copy.deepcopy(DEFAULT_PROVIDER_CONFIG)
    config = {**copy.deepcopy(DEFAULT_PROVIDER_CONFIG), **data}
    default_providers = copy.deepcopy(DEFAULT_PROVIDER_CONFIG.get("providers") or {})
    configured_providers = data.get("providers") if isinstance(data.get("providers"), dict) else {}
    config["providers"] = {
        **default_providers,
        **{
            name: {
                **default_providers.get(name, {}),
                **settings,
            }
            for name, settings in configured_providers.items()
            if isinstance(settings, dict)
        },
    }
    return config


def _read_json_object(path: str | Path | None) -> dict:
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def google_provider_env_config() -> dict:
    env_map = {
        "project_id": "GOOGLE_DOCUMENT_AI_PROJECT_ID",
        "location": "GOOGLE_DOCUMENT_AI_LOCATION",
        "processor_id": "GOOGLE_DOCUMENT_AI_PROCESSOR_ID",
        "bearer_token": "GOOGLE_DOCUMENT_AI_BEARER_TOKEN",
        "credentials_path": "GOOGLE_DOCUMENT_AI_CREDENTIALS_PATH",
    }
    config = {
        key: os.environ[env_name]
        for key, env_name in env_map.items()
        if os.environ.get(env_name)
    }
    if "credentials_path" not in config and os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        config["credentials_path"] = os.environ["GOOGLE_APPLICATION_CREDENTIALS"]
    if os.environ.get("GOOGLE_DOCUMENT_AI_TIMEOUT_SECONDS"):
        config["timeout_seconds"] = os.environ["GOOGLE_DOCUMENT_AI_TIMEOUT_SECONDS"]
    if os.environ.get("GOOGLE_DOCUMENT_AI_ESTIMATED_COST_AED_PER_PAGE"):
        config["estimated_cost_aed_per_page"] = os.environ["GOOGLE_DOCUMENT_AI_ESTIMATED_COST_AED_PER_PAGE"]
    return config


def google_adc_credentials_available() -> bool:
    try:
        import google.auth

        google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        return True
    except Exception:
        return False


def google_provider_configuration_status(config: dict | None = None) -> dict:
    runtime_config = config or load_google_provider_runtime_config()
    settings = get_provider_settings("google", runtime_config)
    token_env = settings.get("bearer_token_env_var")
    credentials_env = settings.get("credentials_path_env_var")
    has_token = bool(settings.get("bearer_token") or (token_env and os.environ.get(str(token_env))))
    has_credentials_path = bool(
        settings.get("credentials_path")
        or (credentials_env and os.environ.get(str(credentials_env)))
    )
    has_adc = google_adc_credentials_available()
    missing = []
    if not settings.get("project_id"):
        missing.append("GOOGLE_DOCUMENT_AI_PROJECT_ID")
    if not settings.get("location"):
        missing.append("GOOGLE_DOCUMENT_AI_LOCATION")
    if not settings.get("processor_id"):
        missing.append("GOOGLE_DOCUMENT_AI_PROCESSOR_ID")
    if not (has_token or has_credentials_path or has_adc):
        missing.append("GOOGLE_DOCUMENT_AI_BEARER_TOKEN or GOOGLE_APPLICATION_CREDENTIALS or ADC")
    if has_token:
        credential_source = "bearer token"
    elif has_credentials_path:
        credential_source = "credentials file"
    elif has_adc:
        credential_source = "ADC"
    else:
        credential_source = ""
    return {
        "project_id": settings.get("project_id"),
        "location": settings.get("location"),
        "processor_id": settings.get("processor_id"),
        "provider_enabled": bool(settings.get("enabled")),
        "provider_api_allowed": bool(settings.get("api_calls_enabled")),
        "credential_source": credential_source,
        "missing": missing,
        "loaded": not missing,
        "ready": not missing and bool(settings.get("enabled")) and bool(settings.get("api_calls_enabled")),
    }


def load_google_provider_runtime_config(
    *,
    allow_api_calls: bool = False,
    config: dict | None = None,
    secrets_path: str | Path | None = None,
) -> dict:
    runtime_config = copy.deepcopy(config) if config is not None else load_provider_config()
    providers = copy.deepcopy(runtime_config.get("providers") or {})
    google_settings = {
        **copy.deepcopy(DEFAULT_PROVIDER_CONFIG["providers"]["google"]),
        **(providers.get("google") or {}),
    }
    secrets = _read_json_object(secrets_path or GOOGLE_PROVIDER_SECRETS_FILE)
    google_settings.update({key: value for key, value in secrets.items() if value not in (None, "")})
    google_settings.update({key: value for key, value in google_provider_env_config().items() if value not in (None, "")})
    if allow_api_calls:
        google_settings["enabled"] = True
        google_settings["api_calls_enabled"] = True
    google_settings["requires_credentials"] = True
    providers["google"] = google_settings
    runtime_config["providers"] = providers
    runtime_config["primary_provider"] = runtime_config.get("primary_provider")
    return runtime_config


def get_enabled_provider_names(config: dict | None = None) -> list[str]:
    config = config or load_provider_config()
    names = config.get("enabled_providers") or []
    if not isinstance(names, list):
        return []
    return [str(name).strip() for name in names if str(name).strip()]


def get_primary_provider_name(config: dict | None = None) -> str | None:
    config = config or load_provider_config()
    name = config.get("primary_provider")
    if name in (None, ""):
        return None
    return str(name).strip() or None


def get_provider_settings(provider_name: str | None, config: dict | None = None) -> dict:
    config = config or load_provider_config()
    providers = config.get("providers") or {}
    if not provider_name:
        return {}
    return providers.get(str(provider_name).strip(), {}) if isinstance(providers, dict) else {}


def estimate_provider_cost(
    page_count: int,
    *,
    provider_name: str | None = None,
    config: dict | None = None,
) -> dict:
    config = config or load_provider_config()
    settings = get_provider_settings(provider_name, config)
    pages = max(1, int(page_count or 1))
    if settings:
        rate = float(settings.get("estimated_cost_aed_per_page") or 0.0)
        provider = provider_name or ""
        display_name = settings.get("display_name") or provider
        placeholder = False
    else:
        rate = float(config.get("placeholder_provider_estimate_aed_per_doc") or 0.25)
        provider = "benchmark_placeholder"
        display_name = "Benchmark placeholder provider"
        placeholder = True
    estimated = round(rate * pages, 4)
    return {
        "provider": provider,
        "display_name": display_name,
        "page_count": pages,
        "estimated_cost_aed": estimated,
        "estimated_cost_usd": round(estimated / 3.6725, 4),
        "placeholder": placeholder,
    }


def _field(fields: dict, *names: str) -> Any:
    for name in names:
        if name in fields and fields[name] not in (None, ""):
            return fields[name]
    return None


def _money(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return round(float(str(value).replace(",", "").strip()), 2)
    except ValueError:
        return None


def _confidence(confidence_by_field: dict, *names: str) -> float:
    values = []
    for name in names:
        value = confidence_by_field.get(name)
        if value not in (None, ""):
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                continue
    return max(values) if values else 0.0


def _max_confidence(confidence_by_field: dict) -> float:
    values = []
    for value in confidence_by_field.values():
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    return max(values) if values else 0.0


def _normalise_line_items(line_items: list, *, total_amount: float | None, supplier_name: str | None) -> tuple[list[dict], bool]:
    mapped = []
    missing_account = False
    for item in line_items:
        if not isinstance(item, dict):
            continue
        amount = _money(_field(item, "amount", "line_amount", "total"))
        account_name = _field(item, "account_name", "account")
        if not account_name:
            missing_account = True
        mapped.append({
            "description": str(_field(item, "description", "item", "name") or "Provider line item").strip(),
            "quantity": _field(item, "quantity", "qty"),
            "unit_price": _money(_field(item, "unit_price", "price")),
            "amount": amount,
            "account_name": account_name,
            "account_type": _field(item, "account_type"),
        })
    if not mapped and total_amount is not None:
        missing_account = True
        mapped.append({
            "description": f"{supplier_name or 'Provider'} invoice",
            "quantity": None,
            "unit_price": None,
            "amount": total_amount,
            "account_name": None,
            "account_type": None,
        })
    return mapped, missing_account


def _default_accounting(document_type: str) -> tuple[str, str, str]:
    text = (document_type or "").strip().lower()
    if text in {"supporting_only", "statement", "remittance", "payment_proof"}:
        return "supporting_only", "unknown", "None"
    if text in {"receipt", "expense_receipt"}:
        return "direct_expense_paid", "unknown", "Expense"
    return "supplier_bill_unpaid", "unpaid", "Bill"


def provider_result_to_transaction(
    result: dict,
    *,
    source_file: str | Path,
    folder_path_hint: str | None = None,
) -> dict:
    normalised = normalized_provider_result(**{key: result.get(key) for key in PROVIDER_RESULT_KEYS})
    fields = normalised["fields"]
    confidence = normalised["confidence_by_field"]
    supplier_name = _field(fields, "supplier_name", "supplier", "vendor_name", "vendor")
    invoice_number = _field(fields, "invoice_number", "invoice_id", "invoice_no", "receipt_number")
    invoice_date = _field(fields, "invoice_date", "date", "receipt_date")
    total_amount = _money(_field(fields, "total_amount", "invoice_total", "amount_due", "total"))
    subtotal_amount = _money(_field(fields, "subtotal_amount", "subtotal", "net_amount"))
    tax_amount = _money(_field(fields, "tax_amount", "vat_amount", "total_tax_amount", "vat"))
    document_type = normalised["document_type"] or str(_field(fields, "document_type") or "invoice")
    accounting_treatment, payment_method, qbo_posting_type = _default_accounting(document_type)
    payment_confirmed = bool(_field(fields, "payment_confirmed", "paid"))
    if payment_confirmed and accounting_treatment == "supplier_bill_unpaid":
        accounting_treatment = "supplier_bill_paid"
        payment_method = str(_field(fields, "payment_method") or "unknown")
    line_items, missing_account = _normalise_line_items(
        normalised["line_items"],
        total_amount=total_amount,
        supplier_name=str(supplier_name or ""),
    )
    reasons = []
    if missing_account:
        reasons.append("Provider output did not include account mapping; review required before posting.")
    if normalised["errors"]:
        reasons.append("Provider reported errors: " + "; ".join(str(error) for error in normalised["errors"]))
    provider_confidence = _max_confidence(confidence)
    transaction = {
        "supplier_name": supplier_name,
        "business_unit": None,
        "brand": None,
        "branch": None,
        "allocation_type": "unknown",
        "folder_path_hint": folder_path_hint,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "due_date": _field(fields, "due_date"),
        "currency": _field(fields, "currency") or "AED",
        "total_amount": total_amount,
        "tax_amount": tax_amount,
        "line_items": line_items,
        "notes": "Extracted using document parser provider.",
        "primary_source_file": Path(source_file).name,
        "document_type": document_type,
        "supplier_trn": _field(fields, "supplier_trn", "trn", "supplier_tax_id"),
        "service_period_start": _field(fields, "service_period_start"),
        "service_period_end": _field(fields, "service_period_end"),
        "accounting_treatment": accounting_treatment,
        "payment_method": payment_method,
        "qbo_posting_type": qbo_posting_type,
        "payment_confirmed": payment_confirmed,
        "payment_date": _field(fields, "payment_date"),
        "payment_amount": _money(_field(fields, "payment_amount")),
        "payment_bank_account": _field(fields, "payment_bank_account"),
        "extraction_method": "provider_parser",
        "provider_name": normalised["provider"],
        "provider_confidence": provider_confidence,
        "provider_needs_llm_normalization": normalised["needs_llm_normalization"],
        "provider_page_count": normalised["page_count"],
        "provider_raw_text": normalised["raw_text"],
        "provider_raw_response": normalised["raw_provider_response"],
        "extraction_confidence": provider_confidence,
        "vendor_confidence": _confidence(confidence, "supplier_name", "supplier", "vendor_name", "vendor"),
        "amount_confidence": _confidence(confidence, "total_amount", "invoice_total", "amount_due", "total"),
        "vat_confidence": _confidence(confidence, "tax_amount", "vat_amount", "total_tax_amount", "vat"),
        "account_confidence": 0.0 if missing_account else 1.0,
        "confidence_reasons": reasons[:],
        "needs_review": bool(reasons),
        "manual_check": bool(reasons),
        "review_reason": " ".join(reasons),
    }
    if subtotal_amount is not None:
        transaction["subtotal_amount"] = subtotal_amount
    return transaction


class MockDocumentParserProvider:
    name = "mock"

    def __init__(self, fixtures: dict | None = None, default_result: dict | None = None):
        self.fixtures = fixtures or {}
        self.default_result = default_result

    def parse(
        self,
        file_path: str | Path,
        *,
        text_preview: dict | None = None,
        config: dict | None = None,
    ) -> dict:
        path = Path(file_path)
        mock_result = (config or {}).get("mock_result")
        if isinstance(mock_result, dict):
            return normalized_provider_result(provider=self.name, **{k: v for k, v in mock_result.items() if k != "provider"})
        fixture = self.fixtures.get(path.name) or self.fixtures.get(str(path))
        if isinstance(fixture, dict):
            return normalized_provider_result(provider=self.name, **{k: v for k, v in fixture.items() if k != "provider"})
        if isinstance(self.default_result, dict):
            return normalized_provider_result(provider=self.name, **{k: v for k, v in self.default_result.items() if k != "provider"})
        text = (text_preview or {}).get("text") or ""
        return normalized_provider_result(
            provider=self.name,
            document_type="invoice",
            raw_text=text,
            fields={
                "supplier_name": path.stem,
                "currency": "AED",
            },
            confidence_by_field={"supplier_name": 0.5},
            page_count=int((text_preview or {}).get("page_count") or 1),
            raw_provider_response={"source": "mock_default"},
            needs_llm_normalization=True,
        )

    def estimate_cost(self, page_count: int, *, config: dict | None = None) -> dict:
        return {
            "provider": self.name,
            "page_count": int(page_count or 0),
            "estimated_cost_usd": 0.0,
            "estimated_cost_aed": 0.0,
            "currency": "AED",
        }


class ProviderAdapterNotEnabledError(RuntimeError):
    pass


class ProviderApiConfirmationRequired(RuntimeError):
    pass


class ProviderAdapterConfigurationError(RuntimeError):
    pass


def _full_provider_config(config: dict | None = None) -> dict:
    if not config:
        return load_provider_config()
    if "providers" in config:
        return {**copy.deepcopy(DEFAULT_PROVIDER_CONFIG), **config}
    return {
        **copy.deepcopy(DEFAULT_PROVIDER_CONFIG),
        "providers": {
            **copy.deepcopy(DEFAULT_PROVIDER_CONFIG["providers"]),
            "google": {
                **copy.deepcopy(DEFAULT_PROVIDER_CONFIG["providers"]["google"]),
                **config,
            },
        },
    }


def validate_provider_configuration(
    provider_name: str,
    *,
    config: dict | None = None,
    confirm_provider_api: bool = False,
) -> dict:
    full_config = _full_provider_config(config)
    settings = get_provider_settings(provider_name, full_config)
    errors: list[str] = []
    warnings: list[str] = []
    if not settings:
        errors.append(f"Unknown provider: {provider_name}")
    if not confirm_provider_api:
        errors.append("Live provider API call not confirmed. Pass --confirm-provider-api to run a live benchmark.")
    if settings and not settings.get("enabled"):
        errors.append(f"{provider_name} provider is disabled in config.")
    if settings and not settings.get("api_calls_enabled"):
        errors.append(f"{provider_name} provider API calls are disabled in config.")
    if provider_name == "google" and settings:
        for key in ("project_id", "location", "processor_id"):
            if not settings.get(key):
                errors.append(f"google provider missing config value: {key}")
        token_env = settings.get("bearer_token_env_var")
        credentials_env = settings.get("credentials_path_env_var")
        token_present = bool(settings.get("bearer_token") or (token_env and os.environ.get(str(token_env))))
        credentials_present = bool(
            settings.get("credentials_path")
            or (credentials_env and os.environ.get(str(credentials_env)))
            or settings.get("adc_verified")
            or google_adc_credentials_available()
        )
        if not token_present and not credentials_present:
            errors.append("google provider credentials not present in env, ADC, or config/secrets.local.")
    return {
        "provider": provider_name,
        "ready": not errors,
        "confirmed": bool(confirm_provider_api),
        "settings": settings,
        "errors": errors,
        "warnings": warnings,
    }


class SkeletonDocumentParserProvider:
    name = "skeleton"

    def parse(
        self,
        file_path: str | Path,
        *,
        text_preview: dict | None = None,
        config: dict | None = None,
        confirm_provider_api: bool = False,
    ) -> dict:
        raise ProviderAdapterNotEnabledError(
            f"{self.name} provider adapter is a skeleton; enable credentials and explicit API confirmation before use."
        )

    def estimate_cost(self, page_count: int, *, config: dict | None = None) -> dict:
        return estimate_provider_cost(page_count, provider_name=self.name, config=config)


class AzureDocumentParserProvider(SkeletonDocumentParserProvider):
    name = "azure"


class GoogleDocumentAIParserProvider(SkeletonDocumentParserProvider):
    name = "google"

    def __init__(self, http_post=None):
        self._http_post = http_post

    def parse(
        self,
        file_path: str | Path,
        *,
        text_preview: dict | None = None,
        config: dict | None = None,
        confirm_provider_api: bool = False,
    ) -> dict:
        readiness = validate_provider_configuration(
            self.name,
            config=config,
            confirm_provider_api=confirm_provider_api,
        )
        if not confirm_provider_api:
            raise ProviderApiConfirmationRequired("; ".join(readiness["errors"]))
        if not readiness["ready"]:
            raise ProviderAdapterConfigurationError("; ".join(readiness["errors"]))

        settings = readiness["settings"]
        token = google_document_ai_bearer_token(settings)
        if not token:
            raise ProviderAdapterConfigurationError("google provider credentials could not produce a bearer token.")

        path = Path(file_path)
        mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        endpoint = (
            f"https://{settings['location']}-documentai.googleapis.com/v1/"
            f"projects/{settings['project_id']}/locations/{settings['location']}/"
            f"processors/{settings['processor_id']}:process"
        )
        payload = {
            "rawDocument": {
                "content": base64.b64encode(path.read_bytes()).decode("ascii"),
                "mimeType": mime_type,
            }
        }
        post = self._http_post
        if post is None:
            import requests

            post = requests.post
        response = post(
            endpoint,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            timeout=int(settings.get("timeout_seconds") or 60),
        )
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        response_json = response.json() if hasattr(response, "json") else response
        return normalize_google_document_ai_response(
            response_json,
            page_count=int((text_preview or {}).get("page_count") or 0),
            cost_estimate=self.estimate_cost(int((text_preview or {}).get("page_count") or 1), config=config),
        )


def google_document_ai_bearer_token(settings: dict) -> str | None:
    token = settings.get("bearer_token")
    if token:
        return str(token)
    token_env = settings.get("bearer_token_env_var")
    if token_env and os.environ.get(str(token_env)):
        return os.environ[str(token_env)]
    credentials_path = settings.get("credentials_path")
    credentials_env = settings.get("credentials_path_env_var")
    if not credentials_path and credentials_env:
        credentials_path = os.environ.get(str(credentials_env))
    if not credentials_path:
        try:
            import google.auth
            from google.auth.transport.requests import Request
        except ImportError as exc:
            raise ProviderAdapterConfigurationError(
                "google-auth is required to use Application Default Credentials."
            ) from exc
        credentials, _project_id = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials.refresh(Request())
        return credentials.token
    try:
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
    except ImportError as exc:
        raise ProviderAdapterConfigurationError(
            "google-auth is required to use a service-account credentials file."
        ) from exc
    credentials = service_account.Credentials.from_service_account_file(
        str(credentials_path),
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    credentials.refresh(Request())
    return credentials.token


def _entity_value(entity: dict) -> Any:
    normalised = entity.get("normalizedValue") or entity.get("normalized_value") or {}
    if isinstance(normalised, dict):
        money = normalised.get("moneyValue") or normalised.get("money_value")
        if isinstance(money, dict):
            units = money.get("units") or 0
            nanos = money.get("nanos") or 0
            try:
                return round(float(units) + (float(nanos) / 1_000_000_000), 2)
            except (TypeError, ValueError):
                pass
        date_value = normalised.get("dateValue") or normalised.get("date_value")
        if isinstance(date_value, dict) and all(date_value.get(part) for part in ("year", "month", "day")):
            return f"{int(date_value['year']):04d}-{int(date_value['month']):02d}-{int(date_value['day']):02d}"
        if normalised.get("text"):
            return normalised.get("text")
    return entity.get("mentionText") or entity.get("mention_text") or ""


def _entity_confidence(entity: dict) -> float:
    try:
        return float(entity.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _normalise_entity_type(entity_type: str) -> str:
    return str(entity_type or "").strip().lower().replace("-", "_").replace("/", "_")


def _line_item_from_entity(entity: dict) -> dict:
    item: dict[str, Any] = {}
    property_map = {
        "line_item_description": "description",
        "description": "description",
        "line_item_quantity": "quantity",
        "quantity": "quantity",
        "line_item_unit_price": "unit_price",
        "unit_price": "unit_price",
        "line_item_amount": "amount",
        "amount": "amount",
        "total_amount": "amount",
    }
    for prop in entity.get("properties") or []:
        prop_type = _normalise_entity_type(prop.get("type") or prop.get("type_"))
        field_name = property_map.get(prop_type)
        if field_name:
            item[field_name] = _entity_value(prop)
    if not item and _entity_value(entity):
        item["description"] = str(_entity_value(entity))
    return item


def normalize_google_document_ai_response(
    response: dict,
    *,
    page_count: int = 0,
    cost_estimate: dict | None = None,
) -> dict:
    document = (response or {}).get("document") or {}
    fields: dict[str, Any] = {}
    confidence: dict[str, float] = {}
    line_items: list[dict] = []
    field_map = {
        "supplier_name": "supplier_name",
        "vendor_name": "supplier_name",
        "invoice_id": "invoice_number",
        "invoice_number": "invoice_number",
        "invoice_date": "invoice_date",
        "due_date": "due_date",
        "net_amount": "subtotal_amount",
        "subtotal_amount": "subtotal_amount",
        "total_tax_amount": "tax_amount",
        "vat_amount": "tax_amount",
        "tax_amount": "tax_amount",
        "total_amount": "total_amount",
        "invoice_total": "total_amount",
        "supplier_tax_id": "supplier_trn",
        "supplier_trn": "supplier_trn",
        "currency": "currency",
    }
    for entity in document.get("entities") or []:
        entity_type = _normalise_entity_type(entity.get("type") or entity.get("type_"))
        if entity_type == "line_item" or entity_type.startswith("line_item_") and entity.get("properties"):
            item = _line_item_from_entity(entity)
            if item:
                line_items.append(item)
            continue
        field_name = field_map.get(entity_type)
        if not field_name:
            continue
        value = _entity_value(entity)
        fields[field_name] = value
        confidence[field_name] = _entity_confidence(entity)
    raw_provider_response = {
        "provider_response": response,
        "cost_estimate": cost_estimate or {},
    }
    pages = page_count or len(document.get("pages") or []) or 1
    return normalized_provider_result(
        provider="google",
        document_type="invoice",
        raw_text=document.get("text") or "",
        fields=fields,
        line_items=line_items,
        confidence_by_field=confidence,
        page_count=pages,
        raw_provider_response=raw_provider_response,
        needs_llm_normalization=True,
    )


class MistralDocumentParserProvider(SkeletonDocumentParserProvider):
    name = "mistral"


class GeminiFlashDocumentParserProvider(SkeletonDocumentParserProvider):
    name = "gemini"


class AmazonTextractDocumentParserProvider(SkeletonDocumentParserProvider):
    name = "aws"


class LocalOCRDocumentParserProvider(SkeletonDocumentParserProvider):
    name = "local"
