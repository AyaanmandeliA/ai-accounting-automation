"""
Phase 3 — QBO Poster
Reads data/extracted/*.extracted.json, creates a Bill in QuickBooks Online
for each extracted document, uploads the source file as an attachment,
and links the attachment to the transaction.
"""

if __name__ == "__main__":
    print("Direct QBO posting is disabled. Use: python main.py --post --confirm-live-posting")
    raise SystemExit(0)

import json
import logging
import mimetypes
import base64
import hashlib
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from accounting.audit_log import append_qbo_posting_attempt
from accounting.persistence import record_duplicate_fingerprint, record_transaction
from accounting.rules import POSTABLE_DOCUMENT_CATEGORIES, apply_accounting_rules
from allocation.review_router import copy_to_review_folder
from extraction.coa_loader import load_coa, validate_account_for_transaction
from secrets_manager import load_secret_json, secret_path

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover - live posting dependency
    requests = None
    class RequestException(Exception):
        pass
else:
    RequestException = requests.RequestException

try:
    from intuitlib.client import AuthClient
    from intuitlib.enums import Scopes
    from intuitlib.exceptions import AuthClientError
    from quickbooks import QuickBooks
    from quickbooks.exceptions import QuickbooksException
    from quickbooks.objects.account import Account
    from quickbooks.objects.base import LinkedTxn, Ref
    from quickbooks.objects.bill import Bill
    from quickbooks.objects.billpayment import BillPayment, BillPaymentLine, CheckPayment
    from quickbooks.objects.department import Department
    from quickbooks.objects.detailline import AccountBasedExpenseLine, AccountBasedExpenseLineDetail
    from quickbooks.objects.purchase import Purchase
    from quickbooks.objects.trackingclass import Class
    from quickbooks.objects.vendor import Vendor
    from quickbooks.objects.vendorcredit import VendorCredit
except ModuleNotFoundError:  # pragma: no cover - keeps local dry-runs importable without QBO SDK
    class _MissingQboDependency(Exception):
        pass

    class AuthClientError(_MissingQboDependency):
        pass

    class QuickbooksException(_MissingQboDependency):
        pass

    class _MissingQboObject:
        @staticmethod
        def filter(*args, **kwargs):
            raise RuntimeError("QBO SDK is not installed")

        @staticmethod
        def all(*args, **kwargs):
            raise RuntimeError("QBO SDK is not installed")

        @staticmethod
        def get(*args, **kwargs):
            raise RuntimeError("QBO SDK is not installed")

        def __init__(self, *args, **kwargs):
            pass

        def save(self, *args, **kwargs):
            raise RuntimeError("QBO SDK is not installed")

        def to_ref(self):
            ref = Ref()
            ref.value = getattr(self, "Id", None)
            ref.name = getattr(self, "Name", None) or getattr(self, "DisplayName", None)
            return ref

    class Ref:
        pass

    class LinkedTxn:
        pass

    class Scopes:
        ACCOUNTING = "com.intuit.quickbooks.accounting"

    AuthClient = QuickBooks = _MissingQboObject
    Account = Bill = BillPayment = BillPaymentLine = CheckPayment = _MissingQboObject
    AccountBasedExpenseLine = AccountBasedExpenseLineDetail = _MissingQboObject
    Department = Class = Vendor = Purchase = VendorCredit = _MissingQboObject

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[1]
EXTRACTED_DIR = BASE_DIR / "data/extracted"
PROCESSED_DIR = BASE_DIR / "data/processed"
PROCESSED_FILE = PROCESSED_DIR / "qbo_posted.json"
FAILURES_FILE = PROCESSED_DIR / "qbo_post_failures.json"
REVIEW_FILE = PROCESSED_DIR / "review_decisions.json"
QBO_CONFIG_FILE = BASE_DIR / "config/qbo_config.json"
TOKEN_FILE = secret_path("qbo_tokens", BASE_DIR / "config/qbo_tokens.json")

SANDBOX_BASE = "https://sandbox-quickbooks.api.intuit.com"
PROD_BASE = "https://quickbooks.api.intuit.com"
OAUTH_TIMEOUT_SECONDS = 300
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
APPROVED = "approved"
SALON_UNITS = {
    "sample brand a",
    "sample brand a salon",
    "sample brand b",
    "sample brand b salon",
}
HEAD_OFFICE_UNITS = {
    "head office",
    "ho",
    "hq",
}
OUT_OF_SCOPE_UNITS = {
    "sr",
    "sample restaurant",
    "samplerestaurant",
    "sample kitchen",
}
PAYABLE_DOCUMENT_TYPES = {"invoice", "bill", "receipt", "credit note"}
SUPPORTED_QBO_POSTING_TYPES = {"Bill", "Expense", "SupplierCredit", "VendorCredit"}
SUPPORTED_DECISION_POSTING_ACTIONS = {
    "Create Bill",
    "Create Expense",
    "Create Vendor Credit",
}
LIVE_POSTING_FLAGS = {
    "Bill": None,
    "Expense": "ENABLE_QBO_EXPENSE_POSTING",
    "SupplierCredit": "ENABLE_QBO_VENDOR_CREDIT_POSTING",
    "VendorCredit": "ENABLE_QBO_VENDOR_CREDIT_POSTING",
}


class VendorMissingError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Config & token management
# ---------------------------------------------------------------------------

def _load_qbo_config() -> dict:
    return load_secret_json("qbo_config", QBO_CONFIG_FILE)


def _normalise_unit(value) -> str:
    return str(value or "").strip().lower().replace("&", "and")


def _is_company_item(extracted: dict) -> bool:
    unit = _normalise_unit(extracted.get("business_unit") or extracted.get("brand"))
    if unit in OUT_OF_SCOPE_UNITS:
        return False
    return unit in SALON_UNITS or unit in HEAD_OFFICE_UNITS


def _business_unit_block_reason(extracted: dict) -> str | None:
    unit = _normalise_unit(extracted.get("business_unit") or extracted.get("brand"))
    if not unit:
        return "Unknown business unit; reviewer must set brand/business_unit, branch, class, and location before posting."
    if unit in OUT_OF_SCOPE_UNITS:
        return f"Out-of-scope business unit: {extracted.get('business_unit') or extracted.get('brand')}"
    if unit not in SALON_UNITS and unit not in HEAD_OFFICE_UNITS:
        return f"Unsupported business unit: {extracted.get('business_unit') or extracted.get('brand')}"
    return None


def _load_tokens() -> dict | None:
    if not TOKEN_FILE.exists():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _save_tokens(tokens: dict) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2))
    TOKEN_FILE.chmod(0o600)


# ---------------------------------------------------------------------------
# OAuth flow — runs a local server to catch the redirect
# ---------------------------------------------------------------------------

def _run_oauth(config: dict) -> dict:
    auth_client = AuthClient(
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        redirect_uri=config["redirect_uri"],
        environment=config["environment"],
    )

    scopes = [Scopes.ACCOUNTING]
    state_token = secrets.token_urlsafe(24)
    auth_url = auth_client.get_authorization_url(scopes, state_token=state_token)
    log.info("Opening browser for QBO OAuth — you have 5 minutes to complete login in the browser...")
    webbrowser.open(auth_url)
    print("\n>>> ACTION REQUIRED: Complete the QBO login in your browser, then return here <<<\n")

    captured = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            params = parse_qs(urlparse(self.path).query)
            captured["error"] = params.get("error", [None])[0]
            captured["state"] = params.get("state", [None])[0]
            captured["code"] = params.get("code", [None])[0]
            captured["realm_id"] = params.get("realmId", [None])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Auth complete. You can close this tab.")

        def log_message(self, *args):
            pass  # silence request logs

    port = int(urlparse(config["redirect_uri"]).port or 8000)
    server = HTTPServer(("localhost", port), _Handler)
    server.timeout = OAUTH_TIMEOUT_SECONDS
    try:
        server.handle_request()
    finally:
        server.server_close()

    if captured.get("error"):
        raise RuntimeError(f"OAuth failed: {captured['error']}")
    if captured.get("state") != state_token:
        raise RuntimeError("OAuth callback state did not match")
    if not captured.get("code"):
        raise RuntimeError("OAuth callback did not return an auth code")
    if captured.get("realm_id") != config["company_id"]:
        raise RuntimeError(f"OAuth realm {captured.get('realm_id')} did not match configured company")

    auth_client.get_bearer_token(captured["code"], realm_id=captured["realm_id"])

    tokens = {
        "access_token": auth_client.access_token,
        "refresh_token": auth_client.refresh_token,
        "realm_id": captured["realm_id"],
    }
    _save_tokens(tokens)
    log.info("QBO OAuth complete — tokens saved")
    return tokens


def _refresh_tokens(config: dict, tokens: dict) -> dict:
    auth_client = AuthClient(
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        redirect_uri=config["redirect_uri"],
        environment=config["environment"],
    )
    auth_client.refresh(refresh_token=tokens["refresh_token"])
    tokens["access_token"] = auth_client.access_token
    tokens["refresh_token"] = auth_client.refresh_token
    tokens["realm_id"] = tokens.get("realm_id", config["company_id"])
    _save_tokens(tokens)
    log.info("QBO tokens refreshed")
    return tokens


def _get_qbo_client(config: dict) -> tuple[QuickBooks, dict]:
    tokens = _load_tokens()
    if not tokens:
        tokens = _run_oauth(config)
    else:
        try:
            tokens = _refresh_tokens(config, tokens)
        except (AuthClientError, KeyError) as exc:
            log.warning("QBO token refresh failed; reauthenticating: %s", exc)
            tokens = _run_oauth(config)

    auth_client = AuthClient(
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        redirect_uri=config["redirect_uri"],
        environment=config["environment"],
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        realm_id=tokens.get("realm_id", config["company_id"]),
    )

    client = QuickBooks(
        auth_client=auth_client,
        refresh_token=tokens["refresh_token"],
        company_id=tokens.get("realm_id", config["company_id"]),
        sandbox=config["environment"] == "sandbox",
    )
    return client, tokens


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def _load_processed_ledger() -> dict:
    if not PROCESSED_FILE.exists():
        return {"posted": {}, "fingerprints": {}, "statuses": {}}
    try:
        data = json.loads(PROCESSED_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read QBO post ledger; starting fresh: %s", exc)
        return {"posted": {}, "fingerprints": {}, "statuses": {}}
    if "posted" not in data:
        data = {"posted": data}
    data.setdefault("posted", {})
    data.setdefault("fingerprints", {})
    data.setdefault("statuses", {})
    return data


def _load_processed() -> dict[str, str]:
    return _load_processed_ledger().get("posted", {})


def _load_fingerprints() -> dict:
    return _load_processed_ledger().get("fingerprints", {})


def _load_review_decisions() -> dict[str, str]:
    if not REVIEW_FILE.exists():
        return {}
    try:
        data = json.loads(REVIEW_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read review decisions; refusing standalone post: %s", exc)
        return {}
    return {k: str(v) for k, v in data.items()}


def _save_processed(processed: dict[str, str], fingerprints: dict | None = None, statuses: dict | None = None) -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    current = _load_processed_ledger()
    current["posted"] = processed
    if fingerprints is not None:
        current["fingerprints"] = fingerprints
    if statuses is not None:
        current["statuses"] = statuses
    tmp = PROCESSED_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, indent=2, default=str))
    tmp.replace(PROCESSED_FILE)
    for fingerprint, payload in (current.get("fingerprints") or {}).items():
        if isinstance(payload, dict):
            record_duplicate_fingerprint(fingerprint, payload)


def _save_post_status(key: str, status: dict) -> None:
    ledger = _load_processed_ledger()
    ledger.setdefault("statuses", {})[key] = status
    _save_processed(ledger.get("posted", {}), ledger.get("fingerprints", {}), ledger.get("statuses", {}))


def _load_failures() -> dict:
    if not FAILURES_FILE.exists():
        return {}
    try:
        return json.loads(FAILURES_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_failure(key: str, extracted: dict, reason: str) -> None:
    failures = _load_failures()
    failures[key] = {
        "supplier": extracted.get("supplier_name"),
        "invoice_number": extracted.get("invoice_number"),
        "reason": reason,
        "source_file": extracted.get("source_file"),
        "needs_review": True,
    }
    tmp = FAILURES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(failures, indent=2, default=str))
    tmp.replace(FAILURES_FILE)


def _append_reason(existing: str | None, reason: str) -> str:
    existing = (existing or "").strip()
    if not existing:
        return reason
    if reason in existing:
        return existing
    return f"{existing} | {reason}"


# ---------------------------------------------------------------------------
# QBO helpers
# ---------------------------------------------------------------------------

def _record_vendor_creation(extracted: dict, vendor: Vendor) -> None:
    supplier_name = getattr(vendor, "DisplayName", None)
    audit = extracted.setdefault("audit_trail", {})
    qbo_ids = audit.setdefault("qbo_ids", {})
    qbo_ids["vendor_id"] = getattr(vendor, "Id", None)
    history = audit.setdefault("processing_history", [])
    history.append({
        "event": "qbo_vendor_created",
        "vendor_name": supplier_name,
        "vendor_id": getattr(vendor, "Id", None),
    })
    log.info("Audit trail updated for created QBO vendor: %s", supplier_name)


def _save_extracted_audit_update(extracted: dict) -> None:
    path = extracted.get("extracted_file")
    if not path:
        return
    extracted_path = Path(path)
    if not extracted_path.exists() or not extracted_path.is_file():
        return
    tmp = extracted_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(extracted, indent=2, default=str))
    tmp.replace(extracted_path)


def _find_or_create_vendor(
    client: QuickBooks,
    supplier_name: str,
    currency: str,
    extracted: dict,
    *,
    allow_vendor_create: bool = False,
) -> Vendor:
    results = Vendor.filter(DisplayName=supplier_name, qb=client, max_results=1)
    if results:
        return results[0]
    if not allow_vendor_create:
        raise VendorMissingError("Vendor missing in QBO")
    vendor = Vendor()
    vendor.DisplayName = supplier_name[:100]
    vendor.CompanyName = supplier_name[:100]
    vendor.PrintOnCheckName = supplier_name[:100]
    # CurrencyRef omitted — multi-currency not enabled in sandbox; home currency used
    vendor.save(qb=client)
    log.info("Created stub vendor: %s", supplier_name)
    _record_vendor_creation(extracted, vendor)
    _save_extracted_audit_update(extracted)
    return vendor


def _load_account_cache(client: QuickBooks) -> dict[str, Account]:
    all_accounts = Account.all(qb=client, max_results=1000)
    cache: dict[str, Account] = {}
    for acc in all_accounts:
        if getattr(acc, "Active", True) is False:
            continue
        for attr in ("Name", "FullyQualifiedName"):
            val = (getattr(acc, attr, None) or "").strip().lower()
            if val:
                cache.setdefault(val, acc)
    log.info("Loaded %d QBO accounts into cache", len(cache))
    return cache


def _find_account(account_name: str, account_cache: dict[str, Account]) -> Account | None:
    name_lower = (account_name or "").strip().lower()
    if not name_lower:
        return None
    if name_lower in account_cache:
        return account_cache[name_lower]
    for key, acc in account_cache.items():
        if name_lower in key or key in name_lower:
            return acc
    return None


def _load_local_coa() -> dict:
    try:
        return load_coa()
    except (OSError, ValueError) as exc:
        log.warning("Could not load local CoA for account-type preflight: %s", exc)
        return {}


def _find_local_account(account_name: str, coa: dict) -> dict | None:
    name_lower = (account_name or "").strip().lower()
    if not name_lower:
        return None
    if name_lower in coa:
        return coa[name_lower]
    for key, account in coa.items():
        if name_lower in key or key in name_lower:
            return account
    return None


def _account_type_block_reason(extracted: dict, coa: dict) -> str | None:
    for line in extracted.get("line_items") or []:
        account_name = line.get("account_name") or ""
        account = _find_local_account(account_name, coa)
        if not account:
            continue
        reason = validate_account_for_transaction(account, extracted, line)
        if reason:
            return reason
    return None


def _load_class_cache(client: QuickBooks) -> dict[str, Class]:
    cache = {}
    for item in Class.all(qb=client, max_results=1000):
        if getattr(item, "Active", True) is False:
            continue
        for attr in ("Name", "FullyQualifiedName"):
            val = (getattr(item, attr, None) or "").strip().lower()
            if val:
                cache.setdefault(val, item)
    return cache


def _load_location_cache(client: QuickBooks) -> dict[str, Department]:
    cache = {}
    for item in Department.all(qb=client, max_results=1000):
        if getattr(item, "Active", True) is False:
            continue
        for attr in ("Name", "FullyQualifiedName"):
            val = (getattr(item, attr, None) or "").strip().lower()
            if val:
                cache.setdefault(val, item)
    return cache


def _find_named_ref(name: str | None, cache: dict) -> Ref | None:
    name_lower = (name or "").strip().lower()
    if not name_lower:
        return None
    item = cache.get(name_lower)
    if not item:
        for key, candidate in cache.items():
            if name_lower in key or key in name_lower:
                item = candidate
                break
    return item.to_ref() if item else None


def _ref(value: str, name: str | None = None) -> Ref:
    ref = Ref()
    ref.value = value
    ref.name = name or value
    return ref


def _normalise_currency(value: str | None) -> str:
    return (value or "AED").strip().upper()


def _is_payable_document(extracted: dict) -> bool:
    doc_type = (extracted.get("document_type") or "").strip().lower()
    category = (extracted.get("document_category") or "").strip()
    qbo_type = (extracted.get("qbo_posting_type") or "").strip()
    if qbo_type in {"Expense", "SupplierCredit", "VendorCredit"}:
        return True
    if category:
        return category in POSTABLE_DOCUMENT_CATEGORIES
    return doc_type in PAYABLE_DOCUMENT_TYPES


def _is_supported_qbo_posting(extracted: dict) -> bool:
    return (extracted.get("qbo_posting_type") or "").strip() in SUPPORTED_QBO_POSTING_TYPES


def _accounting_decision_block_reason(extracted: dict) -> str | None:
    decision = extracted.get("accounting_decision")
    if not isinstance(decision, dict):
        return "Missing accounting decision engine output; refusing QBO posting."
    if decision.get("decision_engine_version") != 3:
        return "Accounting decision engine output is stale or unsupported; refusing QBO posting."
    action = decision.get("posting_action")
    if not action:
        return "Accounting decision is missing posting_action; refusing QBO posting."
    if action not in SUPPORTED_DECISION_POSTING_ACTIONS:
        return f"Accounting decision action is not postable: {action}"
    if decision.get("requires_review"):
        return "Accounting decision still requires review; refusing QBO posting."
    qbo_object = decision.get("qbo_object")
    qbo_posting_type = extracted.get("qbo_posting_type")
    if qbo_object != qbo_posting_type:
        return f"QBO object mismatch between decision ({qbo_object}) and transaction ({qbo_posting_type})."
    if qbo_object not in SUPPORTED_QBO_POSTING_TYPES:
        return f"Unsupported QBO object from accounting decision: {qbo_object}"
    return None


def _live_posting_block_reason(extracted: dict) -> str | None:
    posting_type = (extracted.get("qbo_posting_type") or "").strip()
    flag = LIVE_POSTING_FLAGS.get(posting_type)
    if not flag:
        return None
    import os
    enabled = os.environ.get(flag, "false").strip().lower() in {"1", "true", "yes", "on"}
    if enabled:
        return None
    return f"{posting_type} live posting disabled; set {flag}=true only after sandbox validation."


def _dedupe_key(extracted: dict) -> str:
    parts = [
        (extracted.get("supplier_name") or "").strip().lower(),
        (extracted.get("invoice_number") or "").strip().lower(),
        (extracted.get("invoice_date") or "").strip(),
        str(extracted.get("total_amount") or "").strip(),
        (extracted.get("brand") or extracted.get("business_unit") or "").strip().lower(),
        (extracted.get("branch") or "").strip().lower(),
    ]
    return "|".join(parts)


def _source_file_hash(extracted: dict) -> str:
    source = extracted.get("source_file")
    if not source:
        return ""
    path = Path(source)
    if not path.exists() or not path.is_file():
        return ""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _duplicate_fingerprint(extracted: dict) -> str:
    raw = _dedupe_key(extracted)
    if not (extracted.get("invoice_number") or "").strip():
        raw += "|" + _source_file_hash(extracted)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _backfill_fingerprints(processed: dict[str, str], fingerprints: dict) -> tuple[dict, int]:
    changed = 0
    for key, bill_id in processed.items():
        path = EXTRACTED_DIR / key
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        fingerprint = _duplicate_fingerprint(data)
        if fingerprint and fingerprint not in fingerprints:
            fingerprints[fingerprint] = {"bill_id": bill_id, "file": key}
            changed += 1
    return fingerprints, changed


def _route_not_posted(extracted: dict, reason: str, *, manual_check: bool = False) -> None:
    extracted["needs_review"] = True
    if manual_check:
        extracted["manual_check"] = True
    extracted["review_reason"] = _append_reason(extracted.get("review_reason"), reason)
    copy_to_review_folder(extracted)


def _request_id(path_name: str) -> str:
    digest = hashlib.sha256(path_name.encode("utf-8")).hexdigest()[:32]
    return f"qb-auto-{digest}"


def _find_existing_bill(client: QuickBooks, extracted: dict, vendor: Vendor) -> Bill | None:
    doc_number = (extracted.get("invoice_number") or "").strip()
    if not doc_number:
        return None
    results = Bill.filter(DocNumber=doc_number, qb=client, max_results=5)
    for bill in results:
        vendor_ref = getattr(bill, "VendorRef", None)
        if getattr(vendor_ref, "value", None) == vendor.Id:
            return bill
    return None


def _find_bank_account(client: QuickBooks, bank_account_name: str | None) -> Account | None:
    if not bank_account_name:
        return None
    results = Account.filter(Name=bank_account_name, qb=client, max_results=1)
    if results:
        return results[0]
    all_accounts = Account.filter(AccountType="Bank", qb=client, max_results=200)
    name_lower = bank_account_name.lower()
    for acc in all_accounts:
        if getattr(acc, "Active", True) is False:
            continue
        names = [acc.Name or "", acc.FullyQualifiedName or ""]
        if any(name_lower in name.lower() or name.lower() in name_lower for name in names):
            return acc
    return None


def _available_bank_account_names(client: QuickBooks) -> list[str]:
    accounts = Account.filter(AccountType="Bank", qb=client, max_results=200)
    names = []
    for acc in accounts:
        if getattr(acc, "Active", True) is False:
            continue
        name = getattr(acc, "FullyQualifiedName", None) or getattr(acc, "Name", None)
        if name:
            names.append(name)
    return sorted(set(names))


def _payment_bank_warning(client: QuickBooks, extracted: dict) -> dict | None:
    if not extracted.get("payment_confirmed"):
        return None
    bank_name = extracted.get("payment_bank_account")
    if not bank_name:
        return {
            "status": "posted_unpaid_due_to_missing_bank_account",
            "required_bank_account": None,
            "available_bank_accounts": _available_bank_account_names(client),
            "warning": "Payment was confirmed, but payment_bank_account is blank.",
        }
    if _find_bank_account(client, bank_name):
        return None
    return {
        "status": "posted_unpaid_due_to_missing_bank_account",
        "required_bank_account": bank_name,
        "available_bank_accounts": _available_bank_account_names(client),
        "warning": f"Payment was confirmed, but QBO bank account was not found: {bank_name}",
    }


def _amount_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid amount for QBO payment: {value}") from exc


def _bill_balance(client: QuickBooks, bill: Bill) -> float | None:
    if not getattr(bill, "Id", None):
        return None
    fresh = Bill.get(bill.Id, qb=client)
    balance = getattr(fresh, "Balance", None)
    if balance in (None, ""):
        return None
    return _amount_float(balance)


def _pay_bill(client: QuickBooks, bill: Bill, extracted: dict, vendor: Vendor) -> BillPayment:
    bank_account_name = extracted.get("payment_bank_account")
    payment_date = extracted.get("payment_date") or extracted.get("invoice_date")
    total_amount = extracted.get("total_amount")
    payment_amount = extracted.get("payment_amount")

    if not total_amount:
        raise ValueError("payment_confirmed but total_amount is missing")
    if payment_amount in (None, ""):
        raise ValueError("payment_confirmed but payment_amount is missing")
    if not payment_date:
        raise ValueError("payment_confirmed but payment_date is missing")
    if not bank_account_name:
        raise ValueError("payment_confirmed but payment_bank_account is missing")

    bank_account = _find_bank_account(client, bank_account_name)
    if not bank_account:
        raise ValueError(f"Could not find bank account in QBO: {bank_account_name}")

    amount = _amount_float(payment_amount)
    bill_total = _amount_float(total_amount)
    if abs(amount - bill_total) > 0.01:
        raise ValueError(
            f"payment_amount {amount:.2f} does not match bill total {bill_total:.2f}"
        )

    linked_txn = LinkedTxn()
    linked_txn.TxnId = bill.Id
    linked_txn.TxnType = "Bill"

    payment_line = BillPaymentLine()
    payment_line.Amount = amount
    payment_line.LinkedTxn = [linked_txn]

    check_payment = CheckPayment()
    check_payment.BankAccountRef = _ref(bank_account.Id, bank_account.Name)

    payment = BillPayment()
    payment.VendorRef = vendor.to_ref()
    payment.PayType = "Check"  # used for bank transfers in QBO API
    payment.CheckPayment = check_payment
    payment.TotalAmt = amount
    payment.TxnDate = payment_date
    payment.Line = [payment_line]

    payment.save(qb=client)
    log.info("Created BillPayment %s — marked Bill %s as PAID via %s",
             payment.Id, bill.Id, bank_account.Name)
    return payment


def _ensure_bill_paid(client: QuickBooks, bill: Bill, extracted: dict, vendor: Vendor) -> BillPayment | None:
    balance = _bill_balance(client, bill)
    if balance is not None and balance <= 0.01:
        log.info("Bill %s already has no open balance — treating as paid", bill.Id)
        return None
    return _pay_bill(client, bill, extracted, vendor)


def _source_path(extracted: dict) -> Path:
    source = (extracted.get("source_file") or "").strip()
    if not source:
        raise ValueError("Extracted JSON missing source_file")
    path = Path(source)
    if not path.is_absolute():
        raise ValueError(f"source_file must be absolute: {source}")
    return path


def _source_paths(extracted: dict) -> list[Path]:
    raw_paths = extracted.get("all_source_files") or [extracted.get("source_file")]
    paths = []
    for raw in raw_paths:
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            raise ValueError(f"source file path must be absolute: {raw}")
        paths.append(path)
    if not paths:
        paths.append(_source_path(extracted))
    return paths


def _ledger_key(extracted: dict) -> str:
    extracted_file = extracted.get("extracted_file")
    if extracted_file:
        return Path(extracted_file).name
    return _source_path(extracted).name + ".extracted.json"


def _multipart_filename(name: str) -> str:
    return name.replace("\\", "_").replace('"', "_")[:250]


def _build_bill(
    extracted: dict,
    vendor: Vendor,
    account_cache: dict,
    class_cache: dict | None = None,
    location_cache: dict | None = None,
) -> Bill:
    bill = Bill()
    bill.VendorRef = vendor.to_ref()
    location_ref = _find_named_ref(extracted.get("qbo_location_name"), location_cache or {})
    if extracted.get("qbo_location_name") and not location_ref:
        raise ValueError(f"No QBO location match for {extracted.get('qbo_location_name')}")
    if location_ref:
        bill.DepartmentRef = location_ref
    bill.TxnDate = extracted.get("invoice_date") or None
    bill.DueDate = extracted.get("due_date") or None
    bill.DocNumber = extracted.get("invoice_number") or None
    note_parts = []
    if extracted.get("business_unit"):
        note_parts.append(f"Business unit: {extracted['business_unit']}")
    if extracted.get("notes"):
        note_parts.append(str(extracted["notes"]))
    bill.PrivateNote = " | ".join(note_parts)

    # CurrencyRef omitted — multi-currency not enabled in sandbox; home currency used

    lines = []
    for item in extracted.get("line_items", []):
        amount = item.get("amount")
        if amount in (None, ""):
            continue

        account_name = item.get("account_name") or ""
        account = _find_account(account_name, account_cache) if account_name else None
        if not account:
            raise ValueError(f"No QBO account match for line account: {account_name}")
        account_type_reason = validate_account_for_transaction(account, extracted, item)
        if account_type_reason:
            raise ValueError(account_type_reason)

        detail = AccountBasedExpenseLineDetail()
        detail.AccountRef = account.to_ref()
        class_ref = _find_named_ref(extracted.get("qbo_class_name"), class_cache or {})
        if extracted.get("qbo_class_name") and not class_ref:
            raise ValueError(f"No QBO class match for {extracted.get('qbo_class_name')}")
        if class_ref:
            detail.ClassRef = class_ref

        line = AccountBasedExpenseLine()
        line.Amount = float(amount)
        line.AccountBasedExpenseLineDetail = detail
        line.Description = item.get("description", "")
        lines.append(line)

    if not lines:
        raise ValueError("No bill lines with valid amounts and QBO account matches")

    bill.Line = lines
    return bill


def _payment_type(value: str | None) -> str:
    method = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if method in {"card", "credit_card", "debit_card"}:
        return "CreditCard"
    if method in {"cheque", "check"}:
        return "Check"
    return "Cash"


def _build_purchase_or_vendor_credit_lines(
    extracted: dict,
    account_cache: dict,
    class_cache: dict | None = None,
) -> list:
    lines = []
    for item in extracted.get("line_items", []):
        amount = item.get("amount")
        if amount in (None, ""):
            continue

        account_name = item.get("account_name") or ""
        account = _find_account(account_name, account_cache) if account_name else None
        if not account:
            raise ValueError(f"No QBO account match for line account: {account_name}")
        account_type_reason = validate_account_for_transaction(account, extracted, item)
        if account_type_reason:
            raise ValueError(account_type_reason)

        detail = AccountBasedExpenseLineDetail()
        detail.AccountRef = account.to_ref()
        class_ref = _find_named_ref(extracted.get("qbo_class_name"), class_cache or {})
        if extracted.get("qbo_class_name") and not class_ref:
            raise ValueError(f"No QBO class match for {extracted.get('qbo_class_name')}")
        if class_ref:
            detail.ClassRef = class_ref

        line = AccountBasedExpenseLine()
        line.Amount = float(amount)
        line.AccountBasedExpenseLineDetail = detail
        line.Description = item.get("description", "")
        lines.append(line)

    if not lines:
        raise ValueError("No QBO lines with valid amounts and account matches")
    return lines


def _build_expense(
    extracted: dict,
    vendor: Vendor,
    account_cache: dict,
    class_cache: dict | None = None,
    location_cache: dict | None = None,
) -> Purchase:
    purchase = Purchase()
    purchase.PaymentType = _payment_type(extracted.get("payment_method"))
    purchase.EntityRef = vendor.to_ref()
    payment_account_name = extracted.get("payment_bank_account") or extracted.get("payment_account_name")
    if not payment_account_name:
        raise ValueError("Expense posting requires payment_bank_account or payment_account_name")
    payment_account = _find_account(payment_account_name, account_cache)
    if not payment_account:
        raise ValueError(f"No QBO payment account match for expense: {payment_account_name}")
    purchase.AccountRef = payment_account.to_ref()
    purchase.TxnDate = extracted.get("payment_date") or extracted.get("invoice_date") or None
    purchase.DocNumber = extracted.get("invoice_number") or None
    purchase.PrivateNote = extracted.get("notes") or None
    location_ref = _find_named_ref(extracted.get("qbo_location_name"), location_cache or {})
    if extracted.get("qbo_location_name") and not location_ref:
        raise ValueError(f"No QBO location match for {extracted.get('qbo_location_name')}")
    if location_ref:
        purchase.DepartmentRef = location_ref
    purchase.Line = _build_purchase_or_vendor_credit_lines(extracted, account_cache, class_cache)
    return purchase


def _build_vendor_credit(
    extracted: dict,
    vendor: Vendor,
    account_cache: dict,
    class_cache: dict | None = None,
    location_cache: dict | None = None,
) -> VendorCredit:
    credit = VendorCredit()
    credit.VendorRef = vendor.to_ref()
    credit.TxnDate = extracted.get("invoice_date") or None
    credit.DocNumber = extracted.get("invoice_number") or None
    credit.PrivateNote = extracted.get("notes") or None
    location_ref = _find_named_ref(extracted.get("qbo_location_name"), location_cache or {})
    if extracted.get("qbo_location_name") and not location_ref:
        raise ValueError(f"No QBO location match for {extracted.get('qbo_location_name')}")
    if location_ref:
        credit.DepartmentRef = location_ref
    credit.Line = _build_purchase_or_vendor_credit_lines(extracted, account_cache, class_cache)
    return credit


# ---------------------------------------------------------------------------
# Attachment upload + link
# ---------------------------------------------------------------------------

def _base_url(config: dict) -> str:
    return SANDBOX_BASE if config["environment"] == "sandbox" else PROD_BASE


def _upload_attachment(
    config: dict,
    tokens: dict,
    source_file: Path,
    entity_type: str,
    entity_id: str,
) -> str:
    if requests is None:
        raise RuntimeError("requests is not installed; QBO attachment upload unavailable")
    if not source_file.exists():
        raise FileNotFoundError(f"Source file missing: {source_file}")
    if source_file.stat().st_size > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"Attachment over {MAX_ATTACHMENT_BYTES} bytes: {source_file}")

    company_id = tokens.get("realm_id", config["company_id"])
    url = f"{_base_url(config)}/v3/company/{company_id}/upload"

    mime_type, _ = mimetypes.guess_type(str(source_file))
    mime_type = mime_type or "application/octet-stream"

    # QBO upload is multipart: one part is the metadata JSON, other is the file
    metadata = {
        "AttachableRef": [
            {
                "EntityRef": {"type": entity_type, "value": entity_id},
                "IncludeOnSend": False,
            }
        ],
        "FileName": source_file.name[:250],
        "ContentType": mime_type,
    }

    boundary = f"qbauto{secrets.token_hex(16)}"
    encoded_file = base64.b64encode(source_file.read_bytes()).decode("ascii")
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file_metadata_01"; filename="attachment.json"\r\n'
        "Content-Type: application/json; charset=UTF-8\r\n"
        "Content-Transfer-Encoding: 8bit\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file_content_01"; filename="{_multipart_filename(source_file.name)}"\r\n'
        f"Content-Type: {mime_type}\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\n"
        f"{encoded_file}\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")

    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {tokens['access_token']}",
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        data=body,
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    attachable = data.get("AttachableResponse", [{}])[0].get("Attachable", {})
    attachable_id = attachable.get("Id")
    if not attachable_id:
        raise RuntimeError(f"QBO upload returned no attachable ID: {data}")
    log.info("Attachment uploaded: %s → attachable ID %s", source_file.name, attachable_id)
    return attachable_id


def auth_target() -> dict:
    config = _load_qbo_config()
    return {
        "environment": config.get("environment"),
        "company_id": config.get("company_id"),
        "redirect_uri": config.get("redirect_uri"),
        "token_file": str(TOKEN_FILE),
    }


def authorize_only() -> dict:
    config = _load_qbo_config()
    _client, tokens = _get_qbo_client(config)
    realm_id = tokens.get("realm_id") or config.get("company_id")
    if realm_id != config.get("company_id"):
        raise RuntimeError(
            f"OAuth realm {realm_id} did not match configured company {config.get('company_id')}"
        )
    return {
        "environment": config.get("environment"),
        "company_id": config.get("company_id"),
        "realm_id": realm_id,
        "token_file": str(TOKEN_FILE),
    }


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

def run(extracted: list[dict] | None = None, *, allow_vendor_create: bool = False) -> list[dict]:
    """
    extracted: approved extracted dicts to post (from review.run()).
    If None, standalone mode posts only files approved in review_decisions.json.
    """
    config = _load_qbo_config()
    processed = _load_processed()
    fingerprints = _load_fingerprints()
    local_coa = _load_local_coa()
    fingerprints, backfilled = _backfill_fingerprints(processed, fingerprints)
    if backfilled:
        log.info("Backfilled %d QBO duplicate fingerprint(s) from existing extracted files.", backfilled)
        _save_processed(processed, fingerprints)

    if extracted is not None:
        items = extracted
    else:
        if not EXTRACTED_DIR.exists():
            log.info("No data/extracted/ directory — nothing to post")
            return []
        decisions = _load_review_decisions()
        if not any(status == APPROVED for status in decisions.values()):
            log.warning("No approved review decisions found — refusing standalone QBO posting.")
            return []
        items = []
        for p in sorted(EXTRACTED_DIR.iterdir()):
            if p.suffix != ".json" or not p.is_file():
                continue
            if decisions.get(p.name) != APPROVED:
                continue
            try:
                data = json.loads(p.read_text())
                if not data.get("extracted_file"):
                    data["extracted_file"] = str(p.resolve())
                items.append(data)
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("Skipping file that could not be read: %s (%s)", p.name, exc)

    unposted = []
    already_done = 0
    seen_dedupe = set()
    for extracted in items:
        extracted.update(apply_accounting_rules(extracted))
        try:
            key = _ledger_key(extracted)
        except ValueError as exc:
            log.warning("Skipping malformed approved item: %s", exc)
            continue
        fingerprint = _duplicate_fingerprint(extracted)
        decision_block_reason = _accounting_decision_block_reason(extracted)
        if decision_block_reason:
            _route_not_posted(extracted, decision_block_reason)
            append_qbo_posting_attempt(
                qbo_action="preflight_block",
                qbo_posting_type=extracted.get("qbo_posting_type"),
                extracted=extracted,
                success=False,
                error_message=decision_block_reason,
                duplicate_fingerprint=fingerprint,
            )
            log.warning("Skipping item blocked by accounting decision engine for %s: %s", key, decision_block_reason)
            continue
        if not _is_supported_qbo_posting(extracted):
            _route_not_posted(extracted, f"Unsupported QBO posting type: {extracted.get('qbo_posting_type')}")
            append_qbo_posting_attempt(
                qbo_action="preflight_block",
                qbo_posting_type=extracted.get("qbo_posting_type"),
                extracted=extracted,
                success=False,
                error_message=f"Unsupported QBO posting type: {extracted.get('qbo_posting_type')}",
                duplicate_fingerprint=fingerprint,
            )
            log.warning(
                "Skipping unsupported QBO posting type %s (accounting_treatment=%s, document_type=%s)",
                extracted.get("qbo_posting_type"),
                extracted.get("accounting_treatment"),
                extracted.get("document_type"),
            )
            continue
        live_block_reason = _live_posting_block_reason(extracted)
        if live_block_reason:
            _route_not_posted(extracted, live_block_reason)
            append_qbo_posting_attempt(
                qbo_action="preflight_block",
                qbo_posting_type=extracted.get("qbo_posting_type"),
                extracted=extracted,
                success=False,
                error_message=live_block_reason,
                duplicate_fingerprint=fingerprint,
            )
            log.warning("Skipping disabled live posting type for %s: %s", key, live_block_reason)
            continue
        if extracted.get("needs_review") or extracted.get("manual_check"):
            _route_not_posted(extracted, "Item still requires review; refusing QBO posting.")
            append_qbo_posting_attempt(
                qbo_action="preflight_block",
                qbo_posting_type=extracted.get("qbo_posting_type"),
                extracted=extracted,
                success=False,
                error_message="Item still requires review; refusing QBO posting.",
                duplicate_fingerprint=fingerprint,
            )
            log.warning("Skipping item still requiring review: %s", key)
            continue
        if not _is_payable_document(extracted):
            _route_not_posted(extracted, f"Non-payable document type: {extracted.get('document_type')}")
            append_qbo_posting_attempt(
                qbo_action="preflight_block",
                qbo_posting_type=extracted.get("qbo_posting_type"),
                extracted=extracted,
                success=False,
                error_message=f"Non-payable document type: {extracted.get('document_type')}",
                duplicate_fingerprint=fingerprint,
            )
            log.warning(
                "Skipping non-payable document %s (document_type=%s)",
                key,
                extracted.get("document_type"),
            )
            continue
        business_unit_reason = _business_unit_block_reason(extracted)
        if business_unit_reason:
            _route_not_posted(extracted, business_unit_reason)
            append_qbo_posting_attempt(
                qbo_action="preflight_block",
                qbo_posting_type=extracted.get("qbo_posting_type"),
                extracted=extracted,
                success=False,
                error_message=business_unit_reason,
                duplicate_fingerprint=fingerprint,
            )
            log.warning(
                "Blocking QBO posting for %s: %s",
                key,
                business_unit_reason,
            )
            continue
        if not extracted.get("qbo_class_name") or not extracted.get("qbo_location_name"):
            _route_not_posted(extracted, "Missing QBO class/location mapping.")
            append_qbo_posting_attempt(
                qbo_action="preflight_block",
                qbo_posting_type=extracted.get("qbo_posting_type"),
                extracted=extracted,
                success=False,
                error_message="Missing QBO class/location mapping.",
                duplicate_fingerprint=fingerprint,
            )
            log.warning("Skipping item missing QBO class/location mapping: %s", key)
            continue
        account_type_reason = _account_type_block_reason(extracted, local_coa)
        if account_type_reason:
            _route_not_posted(extracted, account_type_reason)
            append_qbo_posting_attempt(
                qbo_action="preflight_block",
                qbo_posting_type=extracted.get("qbo_posting_type"),
                extracted=extracted,
                success=False,
                error_message=account_type_reason,
                duplicate_fingerprint=fingerprint,
            )
            log.warning("Skipping item with incompatible account type: %s (%s)", key, account_type_reason)
            continue
        dedupe_key = _dedupe_key(extracted)
        if dedupe_key in seen_dedupe:
            _route_not_posted(extracted, "Duplicate invoice candidate in approved batch.")
            append_qbo_posting_attempt(
                qbo_action="preflight_block",
                qbo_posting_type=extracted.get("qbo_posting_type"),
                extracted=extracted,
                success=False,
                error_message="Duplicate invoice candidate in approved batch.",
                duplicate_fingerprint=fingerprint,
            )
            log.warning("Duplicate approved invoice candidate routed to review: %s", key)
            continue
        seen_dedupe.add(dedupe_key)
        if key in processed:
            already_done += 1
            continue
        if fingerprint in fingerprints:
            already_done += 1
            _route_not_posted(extracted, "Duplicate fingerprint already posted; review before reposting.")
            append_qbo_posting_attempt(
                qbo_action="duplicate_block",
                qbo_posting_type=extracted.get("qbo_posting_type"),
                extracted=extracted,
                success=False,
                error_message="Duplicate fingerprint already posted; review before reposting.",
                qbo_id=fingerprints[fingerprint].get("bill_id") or fingerprints[fingerprint].get("qbo_id"),
                duplicate_fingerprint=fingerprint,
            )
            log.warning(
                "Duplicate fingerprint blocked for %s; existing QBO transaction: %s",
                key,
                fingerprints[fingerprint].get("bill_id") or fingerprints[fingerprint].get("qbo_id"),
            )
            continue
        unposted.append(extracted)

    log.info("Found %d approved transaction(s) to post (%d already done)", len(unposted), already_done)

    if not unposted:
        return []

    try:
        client, tokens = _get_qbo_client(config)
    except (AuthClientError, QuickbooksException, RuntimeError) as exc:
        log.exception("Could not connect to QBO: %s", exc)
        return []

    account_cache = _load_account_cache(client)
    try:
        class_cache = _load_class_cache(client)
        location_cache = _load_location_cache(client)
    except QuickbooksException as exc:
        log.exception("Could not load QBO class/location lists: %s", exc)
        for extracted in unposted:
            _route_not_posted(extracted, "Could not load QBO class/location lists.")
        return []
    results = []

    for extracted in unposted:
        ledger_key = "unknown"
        supplier_name = (extracted.get("supplier_name") or "Unknown Supplier").strip()
        currency = _normalise_currency(extracted.get("currency"))

        try:
            ledger_key = _ledger_key(extracted)
            if ledger_key in processed:
                continue
            all_sources = _source_paths(extracted)
            for sf in all_sources:
                if not sf.exists():
                    raise FileNotFoundError(f"Source file missing: {sf}")

            vendor = _find_or_create_vendor(
                client,
                supplier_name,
                currency,
                extracted,
                allow_vendor_create=allow_vendor_create,
            )
            qbo_type = (extracted.get("qbo_posting_type") or "").strip()
            if qbo_type in {"Expense", "SupplierCredit", "VendorCredit"}:
                if qbo_type == "Expense":
                    txn = _build_expense(extracted, vendor, account_cache, class_cache, location_cache)
                    entity_type = "Purchase"
                    qbo_action = "create_expense"
                else:
                    txn = _build_vendor_credit(extracted, vendor, account_cache, class_cache, location_cache)
                    entity_type = "VendorCredit"
                    qbo_action = "create_vendor_credit"

                txn.save(qb=client, request_id=_request_id(ledger_key))
                qbo_id = txn.Id
                for sf in all_sources:
                    _upload_attachment(config, tokens, sf, entity_type, qbo_id)

                processed[ledger_key] = qbo_id
                fingerprint = _duplicate_fingerprint(extracted)
                fingerprints[fingerprint] = {
                    "qbo_id": qbo_id,
                    "qbo_posting_type": qbo_type,
                    "file": ledger_key,
                }
                _save_processed(processed, fingerprints)
                append_qbo_posting_attempt(
                    qbo_action=qbo_action,
                    qbo_posting_type=qbo_type,
                    extracted=extracted,
                    success=True,
                    qbo_id=qbo_id,
                    attachment_result={"attached_files": [str(sf) for sf in all_sources]},
                    duplicate_fingerprint=fingerprint,
                )
                results.append({
                    "file": ledger_key,
                    "qbo_id": qbo_id,
                    "qbo_posting_type": qbo_type,
                    "supplier": supplier_name,
                })
                continue

            existing_bill = _find_existing_bill(client, extracted, vendor)
            payment_warning = _payment_bank_warning(client, extracted)
            if existing_bill:
                log.warning("Bill already exists for DocNumber %s", extracted.get("invoice_number"))
                if extracted.get("payment_confirmed") and not payment_warning:
                    _ensure_bill_paid(client, existing_bill, extracted, vendor)
                elif payment_warning:
                    log.warning(payment_warning["warning"])
                for sf in all_sources:
                    _upload_attachment(config, tokens, sf, "Bill", existing_bill.Id)
                processed[ledger_key] = existing_bill.Id
                fingerprint = _duplicate_fingerprint(extracted)
                fingerprints[fingerprint] = {"bill_id": existing_bill.Id, "file": ledger_key}
                if payment_warning:
                    _save_post_status(ledger_key, {**payment_warning, "bill_id": existing_bill.Id})
                _save_processed(processed, fingerprints)
                append_qbo_posting_attempt(
                    qbo_action="attach_to_existing_bill",
                    qbo_posting_type=extracted.get("qbo_posting_type"),
                    extracted=extracted,
                    success=True,
                    qbo_id=existing_bill.Id,
                    attachment_result={"attached_files": [str(sf) for sf in all_sources]},
                    duplicate_fingerprint=fingerprint,
                )
                results.append({"file": ledger_key, "bill_id": existing_bill.Id, "supplier": supplier_name})
                continue

            bill = _build_bill(extracted, vendor, account_cache, class_cache, location_cache)
            bill.save(qb=client, request_id=_request_id(ledger_key))
            bill_id = bill.Id
            log.info("Created Bill %s — %s  %s", bill_id, supplier_name,
                     _fmt_total(extracted))

            # mark as paid if a payment proof was attached
            if extracted.get("payment_confirmed") and not payment_warning:
                _ensure_bill_paid(client, bill, extracted, vendor)
            elif payment_warning:
                log.warning(payment_warning["warning"])

            # attach ALL source files (invoice + remittance/receipt)
            for sf in all_sources:
                _upload_attachment(config, tokens, sf, "Bill", bill_id)

            processed[ledger_key] = bill_id
            fingerprint = _duplicate_fingerprint(extracted)
            fingerprints[fingerprint] = {"bill_id": bill_id, "file": ledger_key}
            if payment_warning:
                _save_post_status(ledger_key, {**payment_warning, "bill_id": bill_id})
            _save_processed(processed, fingerprints)
            append_qbo_posting_attempt(
                qbo_action="create_bill",
                qbo_posting_type=extracted.get("qbo_posting_type"),
                extracted=extracted,
                success=True,
                qbo_id=bill_id,
                attachment_result={"attached_files": [str(sf) for sf in all_sources]},
                duplicate_fingerprint=fingerprint,
            )
            results.append({"file": ledger_key, "bill_id": bill_id, "supplier": supplier_name})

        except VendorMissingError as exc:
            log.warning("Blocking QBO posting for %s: %s", ledger_key, exc)
            _save_failure(ledger_key, extracted, str(exc))
            _route_not_posted(extracted, str(exc), manual_check=True)
            append_qbo_posting_attempt(
                qbo_action="create_bill",
                qbo_posting_type=extracted.get("qbo_posting_type"),
                extracted=extracted,
                success=False,
                error_message=str(exc),
                duplicate_fingerprint=_duplicate_fingerprint(extracted),
            )
        except (AuthClientError, QuickbooksException, RequestException,
                OSError, KeyError, ValueError, RuntimeError) as exc:
            log.exception("Failed to post %s: %s", ledger_key, exc)
            _save_failure(ledger_key, extracted, str(exc))
            _route_not_posted(extracted, f"QBO posting or attachment failure: {exc}")
            append_qbo_posting_attempt(
                qbo_action="create_bill",
                qbo_posting_type=extracted.get("qbo_posting_type"),
                extracted=extracted,
                success=False,
                error_message=str(exc),
                duplicate_fingerprint=_duplicate_fingerprint(extracted),
            )

    log.info("Total bills posted this run: %d", len(results))
    return results


def _fmt_total(extracted: dict) -> str:
    amt = extracted.get("total_amount")
    currency = extracted.get("currency", "AED")
    return f"{currency} {float(amt):,.2f}" if amt else "?"
