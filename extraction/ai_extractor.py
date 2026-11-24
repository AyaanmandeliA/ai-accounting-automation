"""
Phase 2 — AI Extraction
Sends each document in data/raw/ (PDF/image) and each parsed spreadsheet
in data/parsed/ to Claude. Returns structured transaction data ready for
QBO posting, with source_file preserved for attachment.
"""

if __name__ == "__main__":
    print("Direct live extraction is disabled. Use: python main.py --dry-run --extract")
    print("For intentional live extraction, use: python main.py --extract")
    raise SystemExit(0)

import base64
import json
import logging
import os
import re
import shutil
import sys
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

try:
    import anthropic
except ModuleNotFoundError:  # pragma: no cover - dry-run/local tests without Anthropic SDK
    class _MissingAnthropic:
        def __init__(self, *args, **kwargs):
            self.messages = None

    class anthropic:
        Anthropic = _MissingAnthropic

from accounting.cost_controls import (
    cost_guard_status,
    estimate_model_cost,
    estimate_extraction_cost,
    estimate_tokens_for_file,
    estimate_tokens_for_text,
    extraction_run_cost_guard,
    record_ai_usage,
)
from accounting.rules import apply_accounting_rules
from accounting.persistence import record_transaction
from allocation.branch_resolver import apply_folder_allocation, apply_qbo_mapping
from allocation.confidence import apply_review_gate
from allocation.review_router import copy_to_review_folder
from document_processing.media_optimizer import (
    OPTIMIZED_DIR,
    file_size_mb,
    optimize_media_for_ai,
    optimized_size_estimate_mb,
)
from document_processing.document_parser import (
    GoogleDocumentAIParserProvider,
    ProviderAdapterConfigurationError,
    ProviderAdapterNotEnabledError,
    ProviderApiConfirmationRequired,
    estimate_provider_cost,
    get_primary_provider_name,
    get_provider_settings,
    load_google_provider_runtime_config,
    load_provider_config,
    provider_result_to_transaction,
)
from document_processing.google_normalizer import normalize_google_provider_result
from document_processing.text_extractor import extract_text_preview
from document_processing.template_extractor import load_supplier_templates
from extraction.coa_loader import filter_supplier_bill_candidate_accounts, load_coa, load_supplier_account_map
from extraction.model_routing import default_extraction_model, load_model_routing_config
from extraction.routing import (
    ROUTE_CLAUDE_REQUIRED,
    ROUTE_LOCAL_TEXT_RULES,
    ROUTE_NO_AI_TEMPLATE,
    decision_to_dict,
    route_document_group,
)
from extraction.retrieval_engine import retrieve_context
from extraction.supplier_loader import load_suppliers
from secrets_manager import load_secret_json, secret_path

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[1]
RAW_DIR = BASE_DIR / "data/raw"
PARSED_DIR = BASE_DIR / "data/parsed"
EXTRACTED_DIR = BASE_DIR / "data/extracted"
PROCESSED_DIR = BASE_DIR / "data/processed"
PROCESSED_FILE = PROCESSED_DIR / "ai_extracted.json"
CREDENTIALS_FILE = secret_path("app_config", BASE_DIR / "config/credentials.json")
COA_CATEGORIES_FILE = BASE_DIR / "config/coa_categories.json"
SUPPLIER_CATEGORY_MEMORY_FILE = BASE_DIR / "config/supplier_category_memory.json"
EXTRACTION_COST_CONTROL_FILE = BASE_DIR / "config/extraction_cost_control.json"
CLAUDE_REQUIRED_REPORT = PROCESSED_DIR / "claude_required.json"
PROVIDER_REQUIRED_REPORT = PROCESSED_DIR / "provider_required.json"
ROUTE_PROVIDER_PARSER = "provider_parser"
ROUTE_PROVIDER_REQUIRED = "provider_required"
PROVIDER_RAW_DIRNAME = "provider_raw"
SUPPORTED_GOOGLE_DOCUMENT_TYPES = {
    "invoice",
    "receipt",
    "expense_receipt",
    "credit_note",
    "credit memo",
    "credit_memo",
    "supplier_invoice",
    "tax_invoice",
}

PROCESSED_VERSION = 15
DOCUMENT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
MODEL = default_extraction_model()
CLASSIFIER_MODEL = "claude-haiku-4-5"
MAX_TOKENS = 4096
CLASSIFIER_MAX_TOKENS = 20
CLASSIFIER_PROMPT = (
    "What category is this bill? Pick exactly one word from this list: "
    "consumables, utilities, rent, payroll, maintenance, professional_fees, "
    "marketing, equipment, other. Return only the category."
)
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_SPREADSHEET_ROWS = 120
SUPPLIER_CONTEXT_LIMIT = 80
SUPPLIER_HISTORY_CONTEXT_LIMIT = 20
TEXT_MEDIA_TYPES = {"text"}
COA_CATEGORIES = {
    "consumables",
    "utilities",
    "rent",
    "payroll",
    "maintenance",
    "professional_fees",
    "marketing",
    "equipment",
    "other",
}
ACCOUNTING_TREATMENTS = {
    "supplier_bill_unpaid",
    "supplier_bill_paid",
    "direct_expense_paid",
    "cash_expense_paid",
    "card_expense_paid",
    "merchant_settlement",
    "bank_deposit",
    "bank_transfer",
    "journal_entry",
    "supplier_credit",
    "supplier_payment_only",
    "supporting_only",
    "ignore",
}
PAYMENT_METHODS = {
    "unpaid",
    "cash",
    "bank_transfer",
    "card",
    "merchant_settlement",
    "supplier_credit",
    "mixed",
    "unknown",
}
QBO_POSTING_TYPES = {
    "Bill",
    "Expense",
    "JournalEntry",
    "Deposit",
    "Transfer",
    "BillPayment",
    "SupplierCredit",
    "None",
}
DEFAULT_EXTRACTION_COST_CONTROL = {
    "prefer_text_extraction": True,
    "min_text_chars": 500,
    "min_quality_score": 0.65,
    "send_media_if_text_low_quality": True,
    "max_media_file_mb_without_confirmation": 2.0,
    "max_total_run_cost_aed_without_confirmation": 25.0,
}
ZERO_COST = {
    "estimated_input_tokens": 0,
    "estimated_output_tokens": 0,
    "estimated_cost_usd": 0.0,
    "estimated_cost_aed": 0.0,
}
ALWAYS_INCLUDE_ACCOUNT_SUBSTRINGS = {"vat", "tax"}
SUPPLIER_MATCH_STOPWORDS = {
    "llc",
    "l.l.c",
    "fze",
    "fzco",
    "ltd",
    "co",
    "company",
    "trading",
    "services",
    "service",
    "general",
    "middle",
    "east",
    "uae",
    "dubai",
}
MERCHANT_KEYWORDS = {
    "examplebank",
    "examplepay",
    "merchant settlement",
    "customer settlement",
    "settlement advice",
    "payment advice",
}

TRANSACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "supplier_name":    {"type": ["string", "null"]},
        "business_unit":    {"type": ["string", "null"], "description": "brand/entity receiving the expense, e.g. SR, Brand A, Brand B"},
        "brand":            {"type": ["string", "null"], "description": "brand receiving the expense, e.g. Sample Brand A, Sample Brand B, Head Office"},
        "branch":           {"type": ["string", "null"], "description": "branch receiving the expense, e.g. Branch North, Branch South, Branch East, Branch West"},
        "allocation_type":   {"type": "string", "enum": ["branch", "head_office", "shared", "unknown"]},
        "folder_path_hint":  {"type": ["string", "null"], "description": "source folder path used for branch allocation, if available"},
        "invoice_number":   {"type": ["string", "null"]},
        "invoice_date":     {"type": ["string", "null"], "description": "ISO 8601 date YYYY-MM-DD"},
        "due_date":         {"type": ["string", "null"], "description": "ISO 8601 date YYYY-MM-DD"},
        "currency":         {"type": "string", "description": "e.g. AED, USD"},
        "total_amount":     {"type": ["number", "null"]},
        "tax_amount":       {"type": ["number", "null"]},
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description":    {"type": "string"},
                    "quantity":       {"type": ["number", "null"]},
                    "unit_price":     {"type": ["number", "null"]},
                    "amount":         {"type": ["number", "null"]},
                    "account_name":   {"type": ["string", "null"], "description": "best matching CoA account name"},
                    "account_type":   {"type": ["string", "null"]},
                },
                "required": ["description", "amount", "account_name"],
                "additionalProperties": False,
            },
        },
        "notes": {"type": "string", "description": "anything ambiguous or worth flagging for review"},
        "primary_source_file": {"type": ["string", "null"], "description": "exact filename of the primary invoice/bill"},
        "document_type": {"type": "string", "description": "invoice, bill, receipt, remittance, statement, or supporting_only"},
        "supplier_trn": {"type": ["string", "null"], "description": "15-digit UAE supplier TRN if clearly visible"},
        "service_period_start": {"type": ["string", "null"], "description": "ISO 8601 start date if the document covers a service period"},
        "service_period_end": {"type": ["string", "null"], "description": "ISO 8601 end date if the document covers a service period"},
        "accounting_treatment": {
            "type": "string",
            "enum": sorted(ACCOUNTING_TREATMENTS),
            "description": "real accounting meaning of the transaction, separate from document_type",
        },
        "payment_method": {
            "type": "string",
            "enum": sorted(PAYMENT_METHODS),
            "description": "how this transaction is or should be paid",
        },
        "qbo_posting_type": {
            "type": "string",
            "enum": sorted(QBO_POSTING_TYPES),
            "description": "QuickBooks object this should become; use None when not safe/ready to post",
        },
        "payment_confirmed": {"type": "boolean", "description": "true if a bank transfer receipt or payment proof in the same group confirms this invoice was already paid"},
        "payment_date": {"type": ["string", "null"], "description": "ISO 8601 date of payment, from the bank receipt — null if payment_confirmed is false"},
        "payment_amount": {"type": ["number", "null"], "description": "amount paid for this specific invoice/bill, not the total of a bulk transfer"},
        "payment_bank_account": {"type": ["string", "null"], "description": "name of the bank account used to pay, e.g. 'ExampleBank Current Account' — null if payment_confirmed is false"},
        "extraction_confidence": {"type": ["number", "null"]},
        "vendor_confidence": {"type": ["number", "null"]},
        "amount_confidence": {"type": ["number", "null"]},
        "vat_confidence": {"type": ["number", "null"]},
        "branch_confidence": {"type": ["number", "null"]},
        "account_confidence": {"type": ["number", "null"]},
        "overall_confidence": {"type": ["number", "null"]},
        "confidence_reasons": {"type": "array", "items": {"type": "string"}},
        "needs_review": {"type": ["boolean", "null"]},
        "manual_check": {"type": ["boolean", "null"]},
        "review_reason": {"type": ["string", "null"]},
        "qbo_class_name": {"type": ["string", "null"]},
        "qbo_location_name": {"type": ["string", "null"]},
    },
    "required": [
        "supplier_name",
        "business_unit",
        "invoice_date",
        "currency",
        "total_amount",
        "line_items",
        "notes",
        "primary_source_file",
        "document_type",
        "supplier_trn",
        "service_period_start",
        "service_period_end",
        "accounting_treatment",
        "payment_method",
        "qbo_posting_type",
        "payment_confirmed",
        "payment_date",
        "payment_amount",
        "payment_bank_account",
    ],
    "additionalProperties": False,
}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "transactions": {
            "type": "array",
            "minItems": 1,
            "items": TRANSACTION_SCHEMA,
        },
    },
    "required": ["transactions"],
    "additionalProperties": False,
}

OUTPUT_CONFIG = {"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}}

SYSTEM_PROMPT = """You are an accounting assistant for a salon business in the UAE.
Extract structured transaction data from financial documents.

Document type rules:
- INVOICE / BILL: the primary payable document. Extract fully.
- REMITTANCE ADVICE / PAYMENT SLIP / BANK RECEIPT: supporting only. Do NOT turn it into a bill unless an actual payable amount/invoice is present.
- Return one transaction per DISTINCT payable invoice/bill.
- If two pages/files are the same invoice or one is a payment confirmation for that invoice, return ONE transaction and mention the support doc in notes.
- If a bank transfer receipt / payment slip proves the invoice was already paid, set payment_confirmed=true, payment_date to the transfer date, payment_amount to the amount paid for THIS invoice, and payment_bank_account to the bank name (e.g. "ExampleBank Current Account"). Otherwise payment_confirmed=false and payment fields null.
- If one bank transfer pays several invoices together, only mark an individual invoice paid if the receipt clearly allocates an amount to that invoice or the invoice total is clearly included in the grouped payment; explain this in notes.
- If one PDF/email contains two different payable invoices/bills with different invoice numbers, dates, periods, amounts, or branches, return TWO transactions.
- If an invoice appears once in the PDF and again as a duplicate attachment/copy, return ONE transaction.
- For each transaction, set primary_source_file to the exact filename containing that invoice/bill.
- If no primary invoice/bill exists, return one supporting_only transaction with primary_source_file=null, total_amount=null, line_items=[], and notes="NOT A BILL - ..." with the reason.

Cross-email matching rules (CRITICAL):
- Related emails may arrive separately: invoice/settlement advice in one email, payment receipt/advice in another email.
- If documents share merchant/bank keywords, dates, references, and matching amounts, treat payment advice/receipt as supporting evidence, not a second accounting entry.
- ExampleBank/ExamplePay merchant settlement, customer settlement advice, card settlement, and payment advice are usually settlement/support documents, not supplier Bills.
- Do NOT create a QBO Bill from a merchant settlement/payment receipt unless it is clearly an invoice payable to a vendor.
- If a merchant settlement advice gives branch breakup, capture the breakup in notes. Because this pipeline posts QBO Bills only, classify merchant/card settlements as supporting_only unless there is a clear payable supplier invoice.

Accounting classification rules (CRITICAL):
- document_type is what the file is. accounting_treatment is what the accounting transaction is. qbo_posting_type is what QuickBooks should create.
- Use accounting_treatment="supplier_bill_unpaid" and qbo_posting_type="Bill" when there is a vendor invoice/bill that should be paid later through Accounts Payable.
- Use accounting_treatment="supplier_bill_paid" and qbo_posting_type="Bill" when there is a vendor invoice/bill plus clear proof it was paid; also set payment_confirmed=true and payment_method.
- Use accounting_treatment="direct_expense_paid" and qbo_posting_type="Expense" for small receipts already paid immediately from bank/card/cash, where no supplier payable should remain open.
- Use accounting_treatment="cash_expense_paid" and payment_method="cash" for petty cash receipts.
- Use accounting_treatment="card_expense_paid" and payment_method="card" for card-paid expenses.
- Use accounting_treatment="merchant_settlement" and qbo_posting_type="Deposit" for card/merchant settlement advice, customer settlement advice, ExamplePay/ExampleBank settlement documents, and payout breakdowns.
- Use accounting_treatment="bank_deposit" and qbo_posting_type="Deposit" for bank deposit/customer receipt documents that represent money coming into the business.
- Use accounting_treatment="bank_transfer" and qbo_posting_type="Transfer" only for movement between company bank/cash accounts, not vendor bills.
- Use accounting_treatment="journal_entry" and qbo_posting_type="JournalEntry" only for accounting adjustments, payroll accruals, VAT adjustments, depreciation, owner drawings/capital, or allocations that cannot be represented as a Bill/Expense/Deposit/Transfer.
- Use accounting_treatment="supplier_payment_only" and qbo_posting_type="BillPayment" when the document only proves payment of an existing supplier bill and is not itself the bill.
- Use accounting_treatment="supplier_credit", payment_method="supplier_credit", and qbo_posting_type="SupplierCredit" for credit notes or supplier credits reducing payable balances.
- Use accounting_treatment="supporting_only" and qbo_posting_type="None" for remittances, statements, duplicate copies, delivery notes, screenshots, or payment proofs that only support another transaction.
- Use accounting_treatment="ignore" and qbo_posting_type="None" for spam, non-financial files, personal items, or out-of-scope SR-only documents.
- Current automation can post only qbo_posting_type="Bill". Still classify other types correctly so the reviewer can decide and future modules can use them.
- qbo_posting_type must be one of: Bill, Expense, JournalEntry, Deposit, Transfer, BillPayment, SupplierCredit, None.
- payment_method must be one of: unpaid, cash, bank_transfer, card, merchant_settlement, supplier_credit, mixed, unknown.

Brand allocation rules (CRITICAL):
- Folder path is the PRIMARY source of truth when provided. It beats source email, invoice text, vendor history, and AI reasoning.
- If folder path and invoice text disagree, do not guess. Set needs_review=true and explain the conflict in review_reason.
- Business units: SR = Sample Restaurant, Brand A = Sample Brand A Salon, Brand B = Sample Brand B Salon.
- Supported allocation_type values: branch, head_office, shared, unknown.
- Head Office expenses must use allocation_type="head_office" and branch=null.
- Shared expenses must use allocation_type="shared", needs_review=true, and must not be auto-allocated.
- Unknown branch/business unit must never be ignored. Preserve the transaction with needs_review=true and a clear review_reason.
- Current posting scope is Sample Salon and Head Office only, but do not silently drop SR/Sample Restaurant/Sample Kitchen documents. Preserve them with needs_review=true and review_reason explaining out-of-scope allocation.
- Use BOTH the email body and attachments. If the email body contains a breakup/allocation by brand, follow it.
- If one supplier bill covers multiple brands, return one transaction per Sample Salon brand only, with business_unit set and total_amount/line_items equal to that brand's share only.
- If the PDF total is shared across SR, Brand A, and Brand B but the email gives three amounts, return separate transactions for each Sample Salon brand and mark any shared/unclear portions needs_review=true.
- If the only visible allocation is SR/Sample Restaurant/Sample Kitchen, preserve one transaction with business_unit="SR", needs_review=true, and notes/review_reason starting "OUT OF SCOPE - SR".
- If no brand allocation is visible, set brand/business_unit/branch null, allocation_type="unknown", needs_review=true, and explain the ambiguity.

Confidence/review rules:
- Return confidence scores from 0.0 to 1.0 when possible: extraction_confidence, vendor_confidence, amount_confidence, vat_confidence, branch_confidence, account_confidence, overall_confidence.
- overall_confidence >= 0.90 can be review-eligible. 0.70-0.89 needs review. Below 0.70 requires manual allocation.
- Any conflict, missing branch, missing QBO class/location, shared bill, or unknown allocation needs review.

Account matching rules (CRITICAL):
- You MUST assign a real account name from the Chart of Accounts for every line item. Never leave account_name null.
- Pick the most specific match. If unsure between two accounts, pick the more specific one and note it.
- For a chiller/AC maintenance company: use "Repairs and Maintenance" or "Utilities" type accounts.
- For bank transfer receipts: the expense account is what was paid FOR, not the bank account debited.
- If you genuinely cannot determine the account, pick the closest expense-type account and add a note.

Line item amount rules (CRITICAL):
- Line item amounts must be the AFTER-DISCOUNT values, not pre-discount gross amounts.
- If the invoice has a discount, apply it proportionally across lines so they sum to the net-after-discount subtotal.
- If VAT/tax is listed separately on the invoice, add it as a final line item with description "VAT" and account_name matching a Tax/VAT liability account from the CoA.
- The sum of all line item amounts MUST equal total_amount exactly. Double-check before returning.

Description rules:
- One line, specific, max 60 chars. E.g. "AC chiller maintenance Apr–May 2026" not a paragraph.
- No repetition across line items. Each line must be distinct.
- No "auto-imported", "please review", or boilerplate text in descriptions.

General rules:
- Currency defaults to AED.
- Dates: ISO 8601 (YYYY-MM-DD). Month/year only → use 1st of month.
- Supplier: match exactly to the Supplier List if possible.
- notes: one sentence max. Only flag if something is genuinely ambiguous.
- Return schema-valid JSON only: {"transactions":[...]}."""


def _load_api_key() -> str:
    key = os.environ.get("CE_ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    config = load_secret_json("app_config", BASE_DIR / "config/credentials.json")
    key = str(config.get("anthropic_api_key", "")).strip()
    if not key:
        raise RuntimeError("CE_ANTHROPIC_API_KEY or anthropic_api_key in app config is required")
    return key


def _load_processed_ledger() -> dict:
    if not PROCESSED_FILE.exists():
        return {}
    try:
        return json.loads(PROCESSED_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read extraction ledger; starting fresh: %s", exc)
        return {}


def _load_processed() -> set[str]:
    data = _load_processed_ledger()
    if data.get("version") != PROCESSED_VERSION:
        log.warning(
            "Extraction ledger version %s does not match current version %s. "
            "Previously extracted files are stale and will need re-extraction for the new schema.",
            data.get("version"),
            PROCESSED_VERSION,
        )
        return set()
    return set(data.get("files", []))


def _stale_ledger_info() -> dict:
    data = _load_processed_ledger()
    old_version = data.get("version")
    files = data.get("files", [])
    stale = bool(data) and old_version != PROCESSED_VERSION
    return {
        "stale": stale,
        "old_version": old_version,
        "current_version": PROCESSED_VERSION,
        "files": files if isinstance(files, list) else [],
        "reason": (
            f"extraction ledger version {old_version} != current schema {PROCESSED_VERSION}"
            if stale else ""
        ),
    }


def _save_processed(processed: set[str]) -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PROCESSED_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": PROCESSED_VERSION, "files": sorted(processed)}, indent=2))
    tmp.replace(PROCESSED_FILE)


def _load_coa_categories() -> dict[str, list[str]]:
    try:
        data = json.loads(COA_CATEGORIES_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read CoA categories; using full CoA: %s", exc)
        return {}
    return {
        str(category): [str(term).lower() for term in terms]
        for category, terms in data.items()
        if isinstance(terms, list)
    }


def _load_supplier_category_memory() -> dict[str, str]:
    if not SUPPLIER_CATEGORY_MEMORY_FILE.exists():
        return {}
    try:
        data = json.loads(SUPPLIER_CATEGORY_MEMORY_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read supplier category memory: %s", exc)
        return {}
    return {
        str(supplier).lower(): _normalise_category(str(category))
        for supplier, category in data.items()
        if _normalise_category(str(category)) in COA_CATEGORIES
    }


def _load_extraction_cost_control() -> dict:
    if not EXTRACTION_COST_CONTROL_FILE.exists():
        return dict(DEFAULT_EXTRACTION_COST_CONTROL)
    try:
        data = json.loads(EXTRACTION_COST_CONTROL_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read extraction cost control config: %s", exc)
        return dict(DEFAULT_EXTRACTION_COST_CONTROL)
    if not isinstance(data, dict):
        return dict(DEFAULT_EXTRACTION_COST_CONTROL)
    return {**DEFAULT_EXTRACTION_COST_CONTROL, **data}


def _account_search_text(account: dict) -> str:
    return " ".join(
        str(account.get(key, ""))
        for key in ("name", "account_type", "detail_type")
    ).lower()


def _normalised_search_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _matches_term(text: str, term: str) -> bool:
    clean_text = f" {_normalised_search_text(text)} "
    clean_term = _normalised_search_text(term)
    if not clean_term:
        return False
    return f" {clean_term} " in clean_text


def _filter_coa_by_category(coa: dict, category: str | None) -> dict:
    safe_coa = filter_supplier_bill_candidate_accounts(coa)
    if not safe_coa:
        log.warning("No supplier-bill-safe CoA accounts found; using full CoA for diagnostics")
        safe_coa = coa
    category = (category or "other").strip().lower()
    categories = _load_coa_categories()
    terms = categories.get(category)
    if not terms:
        log.warning("Unknown CoA category %s; using supplier-bill-safe CoA", category)
        return safe_coa

    filtered = {}
    for key, account in safe_coa.items():
        text = _account_search_text(account)
        if any(_matches_term(text, term) for term in terms):
            filtered[key] = account

    if not filtered:
        log.warning("CoA category %s matched no supplier-bill-safe accounts; using safe CoA", category)
        return safe_coa

    log.info("CoA category %s selected %d/%d supplier-bill-safe account(s)", category, len(filtered), len(safe_coa))
    return filtered


def _context_block_parts(coa: dict, suppliers: dict, supplier_account_map: dict | None = None) -> dict:
    coa_lines = "\n".join(
        f"- {v['name']} | {v['account_type']}"
        for v in coa.values()
    )
    supplier_lines = "\n".join(
        f"  - {v['name']}"
        for v in list(suppliers.values())[:SUPPLIER_CONTEXT_LIMIT]
    )
    parts = {
        "coa": f"Allowed Chart of Accounts (you MUST use one of these):\n{coa_lines}",
        "suppliers": f"Known Suppliers:\n{supplier_lines}",
        "history": "",
    }
    if supplier_account_map:
        history_items = list(supplier_account_map.items())[:SUPPLIER_HISTORY_CONTEXT_LIMIT]
        history_lines = "\n".join(
            _supplier_history_line(name, data)
            for name, data in history_items
        )
        parts["history"] = (
            "Historical supplier→account mappings from historical books "
            "(use these as your PRIMARY guide for known suppliers — "
            "only deviate if the invoice content clearly indicates a different account):\n"
            + history_lines
        )
    return parts


def _legacy_suppliers_for_group(suppliers: dict, paths: list[Path]) -> dict:
    text = f" {_text_for_group(paths)} "
    matches = {}
    for key, supplier in suppliers.items():
        name = str(supplier.get("name") or key)
        tokens = _supplier_tokens(name)
        if not tokens:
            continue
        supplier_text = _normalised_search_text(name)
        if f" {supplier_text} " in text or any(f" {token} " in text for token in tokens):
            matches[key] = supplier
    if matches:
        return matches
    return dict(list(suppliers.items())[:SUPPLIER_CONTEXT_LIMIT])


def _legacy_supplier_history_for_group(
    supplier_account_map: dict,
    paths: list[Path],
    suppliers: dict,
) -> dict:
    if not supplier_account_map:
        return {}
    text = f" {_text_for_group(paths)} "
    supplier_names = {
        _normalised_search_text(str(v.get("name") or key))
        for key, v in suppliers.items()
    }
    matches = {}
    for name, data in supplier_account_map.items():
        supplier_text = _normalised_search_text(name)
        if not supplier_text:
            continue
        tokens = _supplier_tokens(name)
        matched_text = f" {supplier_text} " in text or any(f" {token} " in text for token in tokens)
        matched_supplier = supplier_text in supplier_names
        if matched_text or matched_supplier:
            matches[name] = data
    return dict(list(matches.items())[:SUPPLIER_HISTORY_CONTEXT_LIMIT])


def _build_context_block(coa: dict, suppliers: dict, supplier_account_map: dict | None = None) -> str:
    parts = _context_block_parts(coa, suppliers, supplier_account_map)
    return "\n\n".join(part for part in (parts["coa"], parts["suppliers"], parts["history"]) if part)


def _supplier_history_line(name: str, data: dict) -> str:
    primary = data.get("primary") or data.get("primary_account") or "unknown"
    frequency = data.get("frequency") or data.get("account_frequency") or {}
    also_used = [account for account in frequency if account != primary][:2]
    treatment = data.get("primary_treatment")
    qbo_type = data.get("qbo_posting_type")
    confidence = data.get("confidence")
    open_bills = data.get("open_bills") or {}
    extras = []
    if treatment:
        extras.append(f"treatment={treatment}")
    if qbo_type:
        extras.append(f"qbo={qbo_type}")
    if confidence:
        extras.append(f"confidence={confidence}")
    if open_bills:
        extras.append(
            f"open_balance={open_bills.get('open_balance_total')} "
            f"({open_bills.get('open_bill_count')} open bill(s))"
        )
    line = f"  - {name}: {primary}"
    if also_used:
        line += f" (also used: {', '.join(also_used)})"
    if extras:
        line += f" [{'; '.join(extras)}]"
    return line


def _message_id_from_filename(name: str) -> str | None:
    match = re.match(r"^([0-9a-fA-F]{12,32})_.+", name)
    return match.group(1).lower() if match else None


def _email_context_data(msg_id: str) -> dict:
    context_path = RAW_DIR / f"{msg_id}_email_context.json"
    if not context_path.exists():
        return {}
    try:
        return json.loads(context_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read email context %s: %s", context_path.name, exc)
        return {}


def _context_text(data: dict) -> str:
    return " ".join(
        str(data.get(key, ""))
        for key in ("subject", "from", "date", "snippet", "body")
    )


def _context_date(data: dict) -> str | None:
    raw = data.get("date")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).date().isoformat()
    except (TypeError, ValueError, AttributeError):
        return None


def _related_email_key(paths: list[Path]) -> str | None:
    texts = []
    dates = []
    for path in paths:
        msg_id = _message_id_from_filename(path.name)
        if not msg_id:
            continue
        data = _email_context_data(msg_id)
        texts.append(path.name)
        texts.append(_context_text(data))
        date = _context_date(data)
        if date:
            dates.append(date)
    text = " ".join(texts).lower()
    if not any(keyword in text for keyword in MERCHANT_KEYWORDS):
        return None
    merchant = "examplebank-examplepay" if any(k in text for k in ("examplebank", "examplepay")) else "merchant"
    date = sorted(dates)[0] if dates else "unknown-date"
    return f"{merchant}:{date}"


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen = set()
    unique = []
    for path in paths:
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return sorted(unique, key=lambda p: p.name.lower())


def _group_by_message(paths: list[Path]) -> list[list[Path]]:
    groups: dict[str, list[Path]] = {}
    solo = []
    for p in paths:
        if p.name.startswith("drive_"):
            solo.append([p])
            continue
        msg_id = _message_id_from_filename(p.name)
        if msg_id:
            groups.setdefault(msg_id, []).append(p)
        else:
            solo.append([p])
    initial = [sorted(v, key=lambda p: p.name.lower()) for v in groups.values()] + solo
    related: dict[str, list[Path]] = {}
    final = []
    for group in initial:
        key = _related_email_key(group)
        if key:
            related.setdefault(key, []).extend(group)
        else:
            final.append(group)
    grouped = [_unique_paths(v) for v in related.values()] + final
    return sorted(grouped, key=lambda g: g[0].name.lower())


def _email_context_for_group(paths: list[Path]) -> str:
    seen = set()
    blocks = []
    for path in paths:
        msg_id = _message_id_from_filename(path.name)
        if not msg_id or msg_id in seen:
            continue
        seen.add(msg_id)
        data = _email_context_data(msg_id)
        if not data:
            continue
        body = (data.get("body") or data.get("snippet") or "").strip()
        blocks.append(
            "Email context:\n"
            f"Subject: {data.get('subject') or ''}\n"
            f"From: {data.get('from') or ''}\n"
            f"Date: {data.get('date') or ''}\n"
            f"Body:\n{body}"
        )
    return "\n\n".join(blocks)


def _text_for_group(paths: list[Path]) -> str:
    chunks = [path.name for path in paths]
    seen = set()
    for path in paths:
        msg_id = _message_id_from_filename(path.name)
        if not msg_id or msg_id in seen:
            continue
        seen.add(msg_id)
        chunks.append(_context_text(_email_context_data(msg_id)))
    return _normalised_search_text(" ".join(chunks))


def _supplier_tokens(name: str) -> list[str]:
    return [
        token
        for token in _normalised_search_text(name).split()
        if len(token) >= 4 and token not in SUPPLIER_MATCH_STOPWORDS
    ]


def _category_from_supplier_memory(paths: list[Path]) -> str | None:
    text = f" {_text_for_group(paths)} "
    for supplier, category in _load_supplier_category_memory().items():
        supplier_text = _normalised_search_text(supplier)
        if not supplier_text:
            continue
        if f" {supplier_text} " in text:
            log.info("Supplier memory matched %s → CoA category %s", supplier, category)
            return category
    return None


def _category_from_local_text(paths: list[Path], extra_text: str = "") -> str | None:
    text = f" {_text_for_group(paths)} {_normalised_search_text(extra_text)} "
    categories = _load_coa_categories()
    scores: dict[str, int] = {}
    for category, terms in categories.items():
        score = 0
        for term in terms:
            if _matches_term(text, term):
                score += 1
        if score:
            scores[category] = score
    if not scores:
        return None
    return max(scores, key=lambda category: (scores[category], category))


def _filter_suppliers_for_group(suppliers: dict, paths: list[Path]) -> dict:
    context = retrieve_context(
        coa={},
        suppliers=suppliers,
        supplier_account_map={},
        text=_text_for_group(paths),
    )
    matches = context["suppliers"]
    if matches:
        log.info("Supplier context retrieved %d/%d likely supplier(s)", len(matches), len(suppliers))
    return matches


def _filter_supplier_account_map_for_group(
    supplier_account_map: dict,
    paths: list[Path],
    suppliers: dict,
) -> dict:
    context = retrieve_context(
        coa={},
        suppliers=suppliers,
        supplier_account_map=supplier_account_map,
        text=_text_for_group(paths),
    )
    matches = context["supplier_history"]
    if matches:
        log.info("Historical supplier context retrieved %d mapping(s)", len(matches))
    return matches


def _retrieve_context_for_group(
    *,
    coa: dict,
    suppliers: dict,
    supplier_account_map: dict,
    paths: list[Path],
    document_type: str = "",
    category: str | None = None,
    supplier_hint: str | None = None,
    supplier_confidence: float | None = None,
) -> dict:
    filtered_coa = _filter_coa_by_category(coa, category or "other")
    return retrieve_context(
        coa=filtered_coa,
        suppliers=suppliers,
        supplier_account_map=supplier_account_map,
        text=_text_for_group(paths),
        supplier_hint=supplier_hint,
        supplier_confidence=supplier_confidence,
        document_type=document_type,
    )


def _encode_image(path: Path) -> tuple[str, str]:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"{path.name} is over {MAX_FILE_BYTES} bytes")
    ext = path.suffix.lower()
    media_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".pdf": "application/pdf",
    }
    media_type = media_types.get(ext, "application/octet-stream")
    data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    return data, media_type


def _parse_response(response) -> dict:
    for block in response.content:
        if getattr(block, "type", None) in TEXT_MEDIA_TYPES:
            text = getattr(block, "text", "").strip()
            if text:
                return json.loads(_strip_json_fences(text))
    raise ValueError("Claude response did not include a text JSON block")


def _strip_json_fences(text: str) -> str:
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    return match.group(1).strip() if match else text


def _normalise_transactions(payload: dict) -> list[dict]:
    transactions = payload.get("transactions")
    if isinstance(transactions, list):
        return [t for t in transactions if isinstance(t, dict)]
    # Backward-compatible guard for older/manual responses.
    if "line_items" in payload or "document_type" in payload:
        return [payload]
    raise ValueError("Claude response did not include transactions")


def _create_message(client: anthropic.Anthropic, *, content) -> list[dict]:
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
        output_config=OUTPUT_CONFIG,
    )
    return _normalise_transactions(_parse_response(response))


def _file_block(path: Path) -> dict[str, Any]:
    data, media_type = _encode_image(path)
    if media_type == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": media_type, "data": data}}
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def _text_quality_ok(preview: dict, config: dict) -> bool:
    return (
        bool(preview.get("text"))
        and int(len(preview.get("text") or "")) >= int(config.get("min_text_chars", 500))
        and float(preview.get("quality_score") or 0) >= float(config.get("min_quality_score", 0.65))
    )


def _file_route_payload(path: Path, config: dict, *, create_optimized: bool = False) -> dict:
    original_mb = file_size_mb(path)
    preview = {
        "text": "",
        "quality_score": 0.0,
        "page_count": None,
        "method": "not_attempted",
        "warnings": [],
    }
    if config.get("prefer_text_extraction", True) and path.suffix.lower() == ".pdf":
        preview = extract_text_preview(path, min_text_chars=int(config.get("min_text_chars", 500)))
        if _text_quality_ok(preview, config):
            return {
                "original_path": path,
                "ai_path": None,
                "kind": "text",
                "route": "text_only",
                "text": preview["text"],
                "text_preview": preview,
                "original_file_size_mb": original_mb,
                "optimized_file_size_mb": 0.0,
                "reason": "local PDF text quality is sufficient",
                "requires_confirmation": False,
            }

    if not config.get("send_media_if_text_low_quality", True):
        return {
            "original_path": path,
            "ai_path": None,
            "kind": "skip",
            "route": "skip_needs_review",
            "text": preview.get("text") or "",
            "text_preview": preview,
            "original_file_size_mb": original_mb,
            "optimized_file_size_mb": 0.0,
            "reason": "local text was low quality and media fallback is disabled",
            "requires_confirmation": False,
        }

    max_mb = float(config.get("max_media_file_mb_without_confirmation", 2.0) or 0)
    estimated_optimized_mb = optimized_size_estimate_mb(path)
    ai_path = path
    optimized_mb = estimated_optimized_mb
    route = "optimized_media"
    method = "estimated_optimized_media"
    warnings: list[str] = []
    if create_optimized:
        optimized = optimize_media_for_ai(path, output_dir=OPTIMIZED_DIR)
        ai_path = Path(optimized["optimized_path"])
        optimized_mb = float(optimized["optimized_file_size_mb"])
        method = optimized["method"]
        warnings = optimized.get("warnings", [])

    requires_confirmation = bool(max_mb and original_mb > max_mb and optimized_mb > max_mb)
    if requires_confirmation:
        route = "full_media_requires_confirmation"
        ai_path = path
        optimized_mb = 0.0
        method = "full_media_size_over_limit"

    return {
        "original_path": path,
        "ai_path": ai_path,
        "kind": "media",
        "route": route,
        "text": preview.get("text") or "",
        "text_preview": preview,
        "original_file_size_mb": original_mb,
        "optimized_file_size_mb": optimized_mb,
        "reason": (
            "media exceeds optimized confirmation threshold"
            if requires_confirmation else
            "local text was unavailable or below quality threshold; optimized media planned"
        ),
        "requires_confirmation": requires_confirmation,
        "optimization_method": method,
        "warnings": warnings,
    }


def _plan_group_route(paths: list[Path], config: dict, *, create_optimized: bool = False) -> dict:
    files = [_file_route_payload(path, config, create_optimized=create_optimized) for path in paths]
    if any(file["route"] == "skip_needs_review" for file in files):
        route = "skip_needs_review"
    elif all(file["route"] == "text_only" for file in files):
        route = "text_only"
    elif any(file["route"] == "full_media_requires_confirmation" for file in files):
        route = "full_media_requires_confirmation"
    else:
        route = "optimized_media"
    reason = "; ".join(sorted({file["reason"] for file in files}))
    return {
        "route": route,
        "files": files,
        "requires_confirmation": any(file["requires_confirmation"] for file in files),
        "text_quality_score": round(
            max((float(file["text_preview"].get("quality_score") or 0) for file in files), default=0.0),
            4,
        ),
        "original_file_size_mb": round(sum(file["original_file_size_mb"] for file in files), 4),
        "optimized_file_size_mb": round(sum(file["optimized_file_size_mb"] for file in files), 4),
        "reason": reason,
    }


def _template_routing_decision(paths: list[Path], route_plan: dict, templates: list[dict] | None = None) -> dict:
    text = "\n".join(file.get("text") or "" for file in route_plan.get("files", []))
    quality_score = max(
        (float((file.get("text_preview") or {}).get("quality_score") or 0.0) for file in route_plan.get("files", [])),
        default=0.0,
    )
    text_preview = {"text": text, "quality_score": quality_score}
    decision = route_document_group(
        paths,
        text_preview=text_preview,
        folder_path_hint=_folder_path_hint_for_group(paths),
        templates=templates,
    )
    return decision_to_dict(decision)


def _provider_parser_configured() -> bool:
    try:
        return bool(get_primary_provider_name(load_provider_config()))
    except Exception:
        return False


def _effective_provider_name(provider_name: str | None = None) -> str | None:
    if provider_name:
        return str(provider_name).strip().lower() or None
    return get_primary_provider_name(load_provider_config())


def _provider_config_for_name(provider_name: str | None) -> dict:
    if provider_name == "google":
        return load_google_provider_runtime_config(allow_api_calls=True)
    return load_provider_config()


def _google_provider_settings(config: dict | None = None) -> dict:
    return get_provider_settings("google", config or load_google_provider_runtime_config(allow_api_calls=True))


def _money_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return round(float(str(value).replace(",", "").strip()), 2)
    except (TypeError, ValueError):
        return None


def _provider_field(result: dict, *names: str) -> Any:
    fields = result.get("fields") or {}
    for name in names:
        value = fields.get(name)
        if value not in (None, ""):
            return value
    return None


def _provider_confidence(result: dict, *names: str) -> float | None:
    confidence = result.get("confidence_by_field") or {}
    values = []
    for name in names:
        value = confidence.get(name)
        if value not in (None, ""):
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                continue
    return min(values) if values else None


def _provider_supplier_hint(result: dict | None) -> tuple[str | None, float | None]:
    if not result:
        return None, None
    supplier = _provider_field(result, "supplier_name", "supplier", "vendor_name", "vendor")
    confidence = _provider_confidence(result, "supplier_name", "supplier", "vendor_name", "vendor")
    return (str(supplier).strip() if supplier else None), confidence


GOOGLE_VALIDATION_FIELDS = {
    "supplier": ("supplier_name", "supplier", "vendor_name", "vendor"),
    "invoice_number": ("invoice_number", "invoice_id", "invoice_no", "receipt_number"),
    "invoice_date": ("invoice_date", "date", "receipt_date"),
    "total": ("total_amount", "invoice_total", "amount_due", "total"),
    "vat": ("tax_amount", "vat_amount", "total_tax_amount", "vat"),
    "line_items": ("line_items",),
    "payment_status": ("payment_status", "payment_confirmed", "paid"),
    "document_type": ("document_type",),
}

GOOGLE_VALIDATION_FIELD_LABELS = {
    "supplier": "supplier_name",
    "invoice_number": "invoice_number",
    "invoice_date": "invoice_date",
    "total": "total_amount",
    "vat": "tax_amount",
    "line_items": "line_items",
    "payment_status": "payment_status",
    "document_type": "document_type",
}

DEFAULT_GOOGLE_CONFIDENCE_THRESHOLDS = {
    "supplier": {"blocker": 0.5, "warning": 0.65, "required": True},
    "invoice_number": {"blocker": 0.0, "warning": 0.45, "required": False},
    "invoice_date": {"blocker": 0.25, "warning": 0.6, "required": True},
    "total": {"blocker": 0.55, "warning": 0.7, "required": True},
    "vat": {"blocker": 0.0, "warning": 0.5, "required": False},
    "line_items": {"blocker": 0.0, "warning": 0.5, "required": False},
    "payment_status": {"blocker": 0.0, "warning": 0.5, "required": False},
    "document_type": {"blocker": 0.0, "warning": 0.6, "required": True},
}

DEFAULT_GOOGLE_CONFIDENCE_WEIGHTS = {
    "supplier": 0.35,
    "total": 0.30,
    "vat": 0.15,
    "invoice_date": 0.10,
    "document_type": 0.10,
    "invoice_number": 0.0,
    "line_items": 0.0,
    "payment_status": 0.0,
}


def _google_confidence_thresholds(settings: dict) -> dict:
    configured = settings.get("confidence_thresholds")
    thresholds = {
        field: dict(values)
        for field, values in DEFAULT_GOOGLE_CONFIDENCE_THRESHOLDS.items()
    }
    if isinstance(configured, dict):
        for field, values in configured.items():
            if isinstance(values, dict):
                thresholds.setdefault(field, {}).update(values)
    fallback = float(settings.get("confidence_threshold") or 0.85)
    for values in thresholds.values():
        values.setdefault("blocker", fallback)
        values.setdefault("warning", fallback)
        values.setdefault("required", False)
    return thresholds


def _google_confidence_weights(settings: dict) -> dict:
    configured = settings.get("confidence_weights")
    weights = dict(DEFAULT_GOOGLE_CONFIDENCE_WEIGHTS)
    if isinstance(configured, dict):
        for field, value in configured.items():
            try:
                weights[field] = max(0.0, float(value))
            except (TypeError, ValueError):
                continue
    total_weight = sum(weights.values())
    if total_weight <= 0:
        return dict(DEFAULT_GOOGLE_CONFIDENCE_WEIGHTS)
    return {field: value / total_weight for field, value in weights.items()}


def _google_field_value(result: dict, field: str) -> Any:
    if field == "line_items":
        return result.get("line_items") or []
    if field == "document_type":
        return result.get("document_type") or _provider_field(result, "document_type")
    return _provider_field(result, *GOOGLE_VALIDATION_FIELDS[field])


def _google_field_confidence(result: dict, field: str) -> float | None:
    if field == "line_items":
        items = result.get("line_items") or []
        if not items:
            return None
        scores = []
        for item in items:
            if isinstance(item, dict) and item.get("confidence") not in (None, ""):
                try:
                    scores.append(float(item["confidence"]))
                except (TypeError, ValueError):
                    continue
        return min(scores) if scores else 1.0
    if field == "document_type":
        return _provider_confidence(result, "document_type", "type")
    return _provider_confidence(result, *GOOGLE_VALIDATION_FIELDS[field])


def _weighted_google_confidence(result: dict, settings: dict) -> tuple[float, dict[str, float]]:
    weights = _google_confidence_weights(settings)
    field_scores: dict[str, float] = {}
    for field, weight in weights.items():
        if weight <= 0:
            continue
        confidence = _google_field_confidence(result, field)
        value = _google_field_value(result, field)
        if confidence is None:
            if field == "document_type" and value:
                confidence = 1.0
            elif field == "vat" and value in (None, "", []):
                confidence = 0.75
            else:
                confidence = 0.0
        field_scores[field] = max(0.0, min(1.0, float(confidence)))
    weighted = sum(field_scores[field] * weights[field] for field in field_scores)
    return round(weighted, 4), field_scores


def _validate_google_provider_result(result: dict, *, config: dict | None = None) -> dict:
    settings = _google_provider_settings(config)
    thresholds = _google_confidence_thresholds(settings)
    warning_tolerance = float(
        settings.get("amount_reconciliation_warning_tolerance_aed")
        or settings.get("amount_reconciliation_tolerance_aed")
        or 0.05
    )
    blocker_tolerance = float(settings.get("amount_reconciliation_blocker_tolerance_aed") or warning_tolerance)
    blocker_percent = float(settings.get("amount_reconciliation_blocker_percent") or 0.0)
    weighted_confidence, weighted_components = _weighted_google_confidence(result, settings)
    weighted_warning_threshold = float(settings.get("weighted_confidence_warning_threshold") or 0.65)
    blockers: list[str] = []
    warnings: list[str] = []
    informational: list[str] = []
    field_confidences: dict[str, float | None] = {}
    document_type = str(result.get("document_type") or _provider_field(result, "document_type") or "").strip().lower()
    if document_type not in SUPPORTED_GOOGLE_DOCUMENT_TYPES:
        blockers.append("Google result missing supported document type.")
    for field in GOOGLE_VALIDATION_FIELDS:
        label = GOOGLE_VALIDATION_FIELD_LABELS.get(field, field)
        rules = thresholds.get(field, {})
        value = _google_field_value(result, field)
        field_confidence = _google_field_confidence(result, field)
        field_confidences[field] = field_confidence
        if rules.get("required") and value in (None, "", []):
            blockers.append(f"Google result missing required field: {label}.")
            continue
        if field_confidence is None:
            if value in (None, "", []):
                informational.append(f"Google result did not include optional field: {label}.")
            else:
                warnings.append(f"Google result missing confidence for: {label}.")
            continue
        blocker_threshold = float(rules.get("blocker") or 0.0)
        warning_threshold = float(rules.get("warning") or blocker_threshold)
        if field_confidence < blocker_threshold:
            blockers.append(
                f"Google confidence for {label} {field_confidence:.2f} is below threshold {blocker_threshold:.2f} (blocker)."
            )
        elif field_confidence < warning_threshold:
            warnings.append(
                f"Google confidence for {label} {field_confidence:.2f} is below threshold {warning_threshold:.2f} (warning)."
            )
    subtotal = _money_or_none(_provider_field(result, "subtotal_amount", "subtotal", "net_amount"))
    tax = _money_or_none(_provider_field(result, "tax_amount", "vat_amount", "total_tax_amount", "vat"))
    total = _money_or_none(_provider_field(result, "total_amount", "invoice_total", "amount_due", "total"))
    if total is not None and subtotal is not None and tax is not None:
        diff = abs((subtotal + tax) - total)
        materiality = abs(total) * blocker_percent
        if diff > blocker_tolerance and diff > materiality:
            blockers.append(
                f"Google amount reconciliation failed: subtotal + VAT differs from total by AED {diff:.2f} (blocker)."
            )
        elif diff > warning_tolerance:
            warnings.append(
                f"Google amount reconciliation failed: subtotal + VAT differs from total by AED {diff:.2f} (warning)."
            )
    if weighted_confidence < weighted_warning_threshold:
        warnings.append(
            f"Google weighted confidence {weighted_confidence:.2f} is below warning threshold {weighted_warning_threshold:.2f}."
        )
    return {
        "valid": not blockers,
        "errors": blockers,
        "blockers": blockers,
        "warnings": warnings,
        "informational": informational,
        "field_confidences": field_confidences,
        "field_thresholds": thresholds,
        "weighted_confidence": weighted_confidence,
        "weighted_components": weighted_components,
        "confidence_threshold": float(settings.get("confidence_threshold") or 0.85),
        "reconciliation_tolerance_aed": warning_tolerance,
        "reconciliation_blocker_tolerance_aed": blocker_tolerance,
    }


def _explicit_missing_google_required_fields(result: dict, *, config: dict | None = None) -> list[str]:
    settings = _google_provider_settings(config)
    thresholds = _google_confidence_thresholds(settings)
    fields = result.get("fields") or {}
    missing = []
    for field, aliases in GOOGLE_VALIDATION_FIELDS.items():
        if not thresholds.get(field, {}).get("required"):
            continue
        label = GOOGLE_VALIDATION_FIELD_LABELS.get(field, field)
        if any(alias in fields and fields.get(alias) in (None, "", []) for alias in aliases):
            missing.append(f"Google result missing required field: {label}.")
    return missing


def _provider_raw_response_path(provider_result: dict, primary_path: Path) -> Path:
    raw_dir = PROCESSED_DIR / PROVIDER_RAW_DIRNAME
    raw_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_slug(primary_path.stem, "document")
    provider = _safe_slug(provider_result.get("provider") or "provider", "provider")
    path = raw_dir / f"{provider}_{stem}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}.json"
    path.write_text(json.dumps(provider_result.get("raw_provider_response") or {}, indent=2, default=str))
    return path


def _google_provider_to_transaction(provider_result: dict, *, primary_path: Path, all_paths: list[Path]) -> dict:
    raw_path = _provider_raw_response_path(provider_result, primary_path)
    transaction = provider_result_to_transaction(
        provider_result,
        source_file=primary_path,
        folder_path_hint=_folder_path_hint_for_group(all_paths),
    )
    transaction["primary_source_file"] = primary_path.name
    transaction["provider_name"] = "google"
    transaction["provider_raw_response_path"] = str(raw_path.resolve())
    transaction["raw_provider_response_path"] = str(raw_path.resolve())
    transaction["source_attachments"] = [str(path.resolve()) for path in all_paths]
    transaction["provider_validation_status"] = "accepted"
    return transaction


def _apply_google_validation_metadata(transaction: dict, validation: dict) -> dict:
    enriched = dict(transaction)
    weighted = validation.get("weighted_confidence")
    if weighted is not None:
        enriched["provider_weighted_confidence"] = weighted
        enriched["provider_confidence"] = weighted
        enriched["extraction_confidence"] = weighted
    enriched["provider_validation_blockers"] = list(validation.get("blockers") or validation.get("errors") or [])
    enriched["provider_validation_warnings"] = list(validation.get("warnings") or [])
    enriched["provider_validation_informational"] = list(validation.get("informational") or [])
    enriched["provider_field_confidences"] = validation.get("field_confidences") or {}
    enriched["provider_weighted_confidence_components"] = validation.get("weighted_components") or {}
    if enriched["provider_validation_warnings"]:
        existing_reasons = list(enriched.get("confidence_reasons") or [])
        enriched["confidence_reasons"] = existing_reasons + enriched["provider_validation_warnings"]
        existing_review = (enriched.get("review_reason") or "").strip()
        warning_text = " ".join(enriched["provider_validation_warnings"])
        enriched["review_reason"] = f"{existing_review} {warning_text}".strip()
    return enriched


def _google_provider_needs_review_transaction(
    provider_result: dict,
    *,
    primary_path: Path,
    all_paths: list[Path],
    reason: str,
) -> dict:
    transaction = _google_provider_to_transaction(
        provider_result,
        primary_path=primary_path,
        all_paths=all_paths,
    )
    transaction["provider_validation_status"] = "rejected"
    transaction["provider_validation_errors"] = [reason]
    transaction["needs_review"] = True
    transaction["manual_check"] = True
    transaction["review_reason"] = reason
    transaction["confidence_reasons"] = list(transaction.get("confidence_reasons") or []) + [reason]
    return transaction


def _google_provider_failure_transaction(paths: list[Path], route_plan: dict, reason: str) -> dict:
    text = "\n\n".join(
        f"{file['original_path'].name}:\n{(file.get('text') or '').strip()[:4000]}"
        for file in route_plan.get("files") or []
        if (file.get("text") or "").strip()
    )
    notes = "Google provider extraction did not complete. Review and complete fields manually."
    if text:
        notes += "\n\n" + text
    return {
        "supplier_name": None,
        "business_unit": None,
        "brand": None,
        "branch": None,
        "allocation_type": "unknown",
        "folder_path_hint": _folder_path_hint_for_group(paths),
        "invoice_number": None,
        "invoice_date": None,
        "due_date": None,
        "currency": "AED",
        "total_amount": None,
        "tax_amount": None,
        "line_items": [],
        "notes": notes,
        "primary_source_file": paths[0].name,
        "document_type": "provider_extraction_error",
        "supplier_trn": None,
        "service_period_start": None,
        "service_period_end": None,
        "accounting_treatment": "needs_review",
        "payment_method": "unknown",
        "qbo_posting_type": "None",
        "payment_confirmed": False,
        "payment_date": None,
        "payment_amount": None,
        "payment_bank_account": None,
        "extraction_method": "provider_parser",
        "provider_name": "google",
        "provider_validation_status": "failed",
        "provider_validation_errors": [reason],
        "extraction_confidence": 0.0,
        "vendor_confidence": 0.0,
        "amount_confidence": 0.0,
        "vat_confidence": 0.0,
        "account_confidence": 0.0,
        "confidence_reasons": [reason],
        "needs_review": True,
        "manual_check": True,
        "review_reason": reason,
    }


def _parse_google_provider_group(paths: list[Path], route_plan: dict | None = None) -> dict:
    config = load_google_provider_runtime_config(allow_api_calls=True)
    provider = GoogleDocumentAIParserProvider()
    primary_path = paths[0]
    text_preview = None
    if route_plan and route_plan.get("files"):
        text_preview = (route_plan["files"][0] or {}).get("text_preview")
    return provider.parse(
        primary_path,
        text_preview=text_preview,
        config=config,
        confirm_provider_api=True,
    )


def _claude_required_report_path() -> Path:
    return PROCESSED_DIR / "claude_required.json"


def _provider_required_report_path() -> Path:
    return PROCESSED_DIR / "provider_required.json"


def _estimate_provider_cost_for_route(route_plan: dict, provider_name: str | None = None) -> dict:
    config = load_provider_config()
    total_pages = 0
    for file in route_plan.get("files") or []:
        preview = file.get("text_preview") or {}
        total_pages += max(1, int(preview.get("page_count") or 1))
    return estimate_provider_cost(total_pages or 1, provider_name=provider_name, config=config)


def _claude_required_reason(route_plan: dict, routing_decision: dict | None = None, *, no_claude: bool = False) -> str:
    reasons = []
    routing_decision = routing_decision or {}
    if no_claude:
        reasons.append("Claude disabled for this run.")
    if not _provider_parser_configured():
        reasons.append("Provider parser unavailable because no primary provider is configured.")
    if routing_decision.get("reason"):
        reasons.append(str(routing_decision["reason"]))
    for file in route_plan.get("files") or []:
        preview = file.get("text_preview") or {}
        text = preview.get("text") or file.get("text") or ""
        quality = float(preview.get("quality_score") or 0.0)
        if not text.strip():
            reasons.append(f"{file['original_path'].name}: no local text available.")
        elif file.get("route") != "text_only":
            reasons.append(f"{file['original_path'].name}: local text quality {quality:.2f} is too low for safe extraction.")
        if file.get("route") in {"optimized_media", "full_media_requires_confirmation"}:
            reasons.append(f"{file['original_path'].name}: media extraction would require Claude.")
        if file.get("route") == "skip_needs_review":
            reasons.append(f"{file['original_path'].name}: media fallback is disabled.")
    seen = []
    for reason in reasons:
        if reason and reason not in seen:
            seen.append(reason)
    return " ".join(seen) or "Claude is required to extract this document safely."


def _provider_required_reason(route_plan: dict, routing_decision: dict | None = None) -> str:
    reasons = []
    if not _provider_parser_configured():
        reasons.append("No primary low-cost provider configured.")
    routing_decision = routing_decision or {}
    if routing_decision.get("reason"):
        reasons.append(str(routing_decision["reason"]))
    for file in route_plan.get("files") or []:
        preview = file.get("text_preview") or {}
        text = preview.get("text") or file.get("text") or ""
        quality = float(preview.get("quality_score") or 0.0)
        if not text.strip():
            reasons.append(f"{file['original_path'].name}: no local text available.")
        elif file.get("route") != "text_only":
            reasons.append(f"{file['original_path'].name}: local text quality {quality:.2f} is too low for deterministic extraction.")
        else:
            reasons.append(f"{file['original_path'].name}: local text is available but needs provider structuring.")
    seen = []
    for reason in reasons:
        if reason and reason not in seen:
            seen.append(reason)
    return " ".join(seen) or "Low-cost provider extraction required."


def _record_claude_required(paths: list[Path], *, reason: str, route_plan: dict | None = None, routing_decision: dict | None = None) -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    report_path = _claude_required_report_path()
    try:
        report = json.loads(report_path.read_text()) if report_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        report = {}
    items = report.get("items") if isinstance(report.get("items"), list) else []
    file_names = [path.name for path in paths]
    items = [
        item for item in items
        if item.get("files") != file_names
    ]
    items.append({
        "files": file_names,
        "source_files": [str(path.resolve()) for path in paths],
        "reason": reason,
        "route": (route_plan or {}).get("route"),
        "template_id": (routing_decision or {}).get("template_id"),
        "text_quality_score": (route_plan or {}).get("text_quality_score"),
        "provider_available": _provider_parser_configured(),
    })
    report = {
        "description": "Documents queued for future Claude/provider extraction. No live API call was made.",
        "count": len(items),
        "items": items,
    }
    report_path.write_text(json.dumps(report, indent=2, default=str))


def _record_provider_required(paths: list[Path], *, reason: str, route_plan: dict | None = None, routing_decision: dict | None = None) -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    report_path = _provider_required_report_path()
    try:
        report = json.loads(report_path.read_text()) if report_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        report = {}
    items = report.get("items") if isinstance(report.get("items"), list) else []
    file_names = [path.name for path in paths]
    items = [
        item for item in items
        if item.get("files") != file_names
    ]
    cost = _estimate_provider_cost_for_route(route_plan or {"files": []}, get_primary_provider_name(load_provider_config()))
    items.append({
        "files": file_names,
        "source_files": [str(path.resolve()) for path in paths],
        "reason": reason,
        "route": (route_plan or {}).get("route"),
        "template_id": (routing_decision or {}).get("template_id"),
        "text_quality_score": (route_plan or {}).get("text_quality_score"),
        "provider_available": _provider_parser_configured(),
        "estimated_provider_cost_aed": cost["estimated_cost_aed"],
        "provider_estimate": cost,
    })
    report = {
        "description": "Documents that need the configured low-cost extraction provider. No live provider API call was made.",
        "count": len(items),
        "items": items,
    }
    report_path.write_text(json.dumps(report, indent=2, default=str))


def _local_text_review_transaction(paths: list[Path], route_plan: dict, reason: str) -> dict:
    text = "\n\n".join(
        f"{file['original_path'].name}:\n{(file.get('text') or '').strip()[:4000]}"
        for file in route_plan.get("files") or []
        if (file.get("text") or "").strip()
    )
    return {
        "supplier_name": None,
        "business_unit": None,
        "brand": None,
        "branch": None,
        "allocation_type": "unknown",
        "folder_path_hint": _folder_path_hint_for_group(paths),
        "invoice_number": None,
        "invoice_date": None,
        "due_date": None,
        "currency": "AED",
        "total_amount": None,
        "tax_amount": None,
        "line_items": [],
        "notes": "Local text extracted without Claude. Review and complete fields manually.\n\n" + text,
        "primary_source_file": paths[0].name,
        "document_type": "local_text_extraction",
        "supplier_trn": None,
        "service_period_start": None,
        "service_period_end": None,
        "accounting_treatment": "supporting_only",
        "payment_method": "unknown",
        "qbo_posting_type": "None",
        "payment_confirmed": False,
        "payment_date": None,
        "payment_amount": None,
        "payment_bank_account": None,
        "extraction_method": "local_text_extraction",
        "extraction_confidence": 0.0,
        "vendor_confidence": 0.0,
        "amount_confidence": 0.0,
        "vat_confidence": 0.0,
        "account_confidence": 0.0,
        "confidence_reasons": [reason],
        "needs_review": True,
        "manual_check": True,
        "review_reason": reason,
    }


def _media_token_for_file_payload(file_payload: dict, *, use_route: bool) -> int:
    if use_route and file_payload["kind"] == "skip":
        return 0
    if use_route and file_payload["kind"] == "text":
        return 0
    path = file_payload.get("ai_path") or file_payload["original_path"]
    if use_route and file_payload["route"] == "optimized_media" and file_payload.get("optimized_file_size_mb"):
        return max(1_500, int(file_payload["optimized_file_size_mb"] * 1024 * 1024 / 3) + 1_000)
    return estimate_tokens_for_file(Path(path))


def _text_tokens_for_route(route_plan: dict) -> int:
    return sum(
        estimate_tokens_for_text(file["text"])
        for file in route_plan["files"]
        if file["kind"] == "text"
    )


def _content_blocks_for_route(route_plan: dict) -> list[Any]:
    content: list[Any] = []
    for file in route_plan["files"]:
        original = file["original_path"]
        if file["kind"] == "text":
            content.append({
                "type": "text",
                "text": f"Extracted local text from {original.name}:\n{file['text']}",
            })
        elif file["kind"] == "media":
            content.append(_file_block(Path(file.get("ai_path") or original)))
    return content


def _response_text(response) -> str:
    for block in response.content:
        if getattr(block, "type", None) in TEXT_MEDIA_TYPES:
            text = getattr(block, "text", "").strip()
            if text:
                return text
    return ""


def _normalise_category(value: str) -> str:
    text = (value or "").strip().lower()
    compact = re.sub(r"[^a-z_]+", "_", text).strip("_")
    if compact in COA_CATEGORIES:
        return compact
    for category in COA_CATEGORIES:
        if category in compact:
            return category
        if category.replace("_", " ") in text:
            return category
    return "other"


def _classify_bill(client: anthropic.Anthropic, paths: list[Path], route_plan: dict | None = None) -> str:
    remembered = _category_from_supplier_memory(paths)
    if remembered:
        return remembered
    route_plan = route_plan or _plan_group_route(paths, _load_extraction_cost_control(), create_optimized=False)
    preview_text = " ".join(file.get("text") or "" for file in route_plan["files"])
    local_category = _category_from_local_text(paths, preview_text)
    if local_category:
        log.info("Classified document group from local text/filename as CoA category: %s", local_category)
        return local_category

    content: list[Any] = []
    if preview_text.strip():
        content.append({"type": "text", "text": preview_text[:8_000]})
    else:
        content.extend(_content_blocks_for_route(route_plan))
    content.append({"type": "text", "text": CLASSIFIER_PROMPT})
    response = client.messages.create(
        model=CLASSIFIER_MODEL,
        max_tokens=CLASSIFIER_MAX_TOKENS,
        messages=[{"role": "user", "content": content}],
    )
    category = _normalise_category(_response_text(response))
    log.info("Classified document group as CoA category: %s", category)
    return category


def _extraction_instruction(paths: list[Path], context: str) -> str:
    instruction = (
        f"{context}\n\n"
    )
    if len(paths) > 1:
        instruction += (
            f"These {len(paths)} documents are from the same email. "
            "Use judgment: merge duplicate copies and supporting receipts into the related invoice, "
            "but return separate transactions for genuinely separate bills/invoices. "
            "List supporting document filenames in notes when relevant.\n\n"
        )
    instruction += (
        "Return schema-valid JSON with a transactions array. "
        "Set each primary_source_file to one of the exact filenames shown above, or null if no bill/invoice exists."
    )
    return instruction


def _extract_group(
    client: anthropic.Anthropic,
    paths: list[Path],
    context: str,
    route_plan: dict | None = None,
) -> list[dict]:
    route_plan = route_plan or _plan_group_route(paths, _load_extraction_cost_control(), create_optimized=False)
    content: list[Any] = []
    email_context = _email_context_for_group(paths)
    if email_context:
        content.append({"type": "text", "text": email_context})
    for i, path in enumerate(paths):
        if len(paths) > 1:
            content.append({"type": "text", "text": f"Document {i+1} of {len(paths)}: {path.name}"})
    content.extend(_content_blocks_for_route(route_plan))

    content.append({"type": "text", "text": _extraction_instruction(paths, context)})
    return _create_message(client, content=content)


def _spreadsheet_instruction(parsed: dict, context: str) -> str:
    rows = parsed.get("rows", [])
    if len(rows) > MAX_SPREADSHEET_ROWS:
        raise ValueError(
            f"{parsed.get('source_file', 'spreadsheet')} has {len(rows)} rows; "
            f"split before AI extraction or raise MAX_SPREADSHEET_ROWS"
        )
    rows_text = json.dumps(rows, indent=2, ensure_ascii=False)
    return (
        f"{context}\n\n"
        "Below are rows from a financial spreadsheet. Each row may represent "
        "a transaction, invoice line, or expense entry.\n\n"
        f"Rows:\n{rows_text}\n\n"
        "Extract the transaction represented by these rows. If multiple rows form "
        "one invoice, group them into line_items. If the sheet contains multiple "
        "distinct invoices/bills, return multiple transactions. Use null for unknown values. "
        "Return schema-valid JSON with a transactions array only."
    )


def _extract_spreadsheet(client: anthropic.Anthropic, parsed: dict, context: str) -> list[dict]:
    return _create_message(
        client,
        content=_spreadsheet_instruction(parsed, context),
    )


def _choose_primary_path(result: dict, paths: list[Path]) -> Path:
    primary = (result.get("primary_source_file") or "").strip()
    if primary:
        for path in paths:
            if primary in (path.name, str(path.resolve())):
                return path
        log.warning("Claude returned unknown primary_source_file %s; using %s", primary, paths[0].name)
    return paths[0]


def _safe_slug(value: Any, fallback: str) -> str:
    text = str(value or "").strip() or fallback
    text = re.sub(r"[^A-Za-z0-9._ -]+", "_", text)
    text = re.sub(r"\s+", " ", text).strip(" ._")
    return text[:80] or fallback


def _output_name(result: dict, primary_path: Path, index: int, total: int) -> str:
    if total <= 1:
        return primary_path.name + ".extracted.json"
    ref = result.get("invoice_number") or result.get("invoice_date") or result.get("total_amount")
    suffix = _safe_slug(ref, f"transaction-{index + 1}")
    return f"{primary_path.name}.{index + 1:02d}-{suffix}.extracted.json"


def _folder_path_hint_for_path(path: Path) -> str | None:
    metadata_path = RAW_DIR / f"{path.name}.metadata.json"
    if metadata_path.exists():
        try:
            data = json.loads(metadata_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Could not read source metadata %s: %s", metadata_path.name, exc)
        else:
            return data.get("folder_path") or data.get("folder_path_hint")

    try:
        relative = path.resolve().relative_to(BASE_DIR)
    except ValueError:
        return None
    parts = list(relative.parts)
    if len(parts) > 2 and parts[0] != "data":
        return str(relative.parent)
    return None


def _folder_path_hint_for_group(paths: list[Path]) -> str | None:
    hints = []
    for path in paths:
        hint = _folder_path_hint_for_path(path)
        if hint and hint not in hints:
            hints.append(hint)
    if len(hints) == 1:
        return hints[0]
    if len(hints) > 1:
        return " | ".join(hints)
    return None


def _source_upload_date(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
    except OSError:
        return None


def _enrich_result(result: dict, primary_path: Path, all_paths: list[Path]) -> dict:
    payload = {
        **result,
        "source_file": str(primary_path.resolve()),
        "all_source_files": [str(p.resolve()) for p in all_paths],
        "folder_path_hint": result.get("folder_path_hint") or _folder_path_hint_for_group(all_paths),
        "source_upload_date": result.get("source_upload_date") or _source_upload_date(primary_path),
    }
    payload = apply_folder_allocation(payload, payload.get("folder_path_hint"))
    payload = apply_qbo_mapping(payload)
    payload = apply_accounting_rules(payload)
    payload = apply_review_gate(payload)
    return payload


def _unique_archive_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for i in range(2, 1000):
        candidate = path.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not choose archive filename for {path.name}")


def _remove_stale_outputs(paths: list[Path], archive_label: str = "replaced") -> list[Path]:
    archived = []
    if not EXTRACTED_DIR.exists():
        return archived
    names = {p.name for p in paths}
    archive_dir = EXTRACTED_DIR / f"archive_{archive_label}"
    archive_dir.mkdir(parents=True, exist_ok=True)
    for out in EXTRACTED_DIR.iterdir():
        if not out.is_file() or out.suffix != ".json":
            continue
        if any(out.name.startswith(name + ".") and out.name.endswith(".extracted.json") for name in names):
            archived_path = _unique_archive_path(archive_dir / out.name)
            shutil.move(str(out), archived_path)
            archived.append(archived_path)
    if archived:
        log.warning("Archived %d stale extracted JSON file(s) to %s", len(archived), archive_dir)
    return archived


def _save_result(result: dict, primary_path: Path, all_paths: list[Path], index: int = 0, total: int = 1) -> Path:
    EXTRACTED_DIR.mkdir(parents=True, exist_ok=True)
    out_name = _output_name(result, primary_path, index, total)
    out_path = EXTRACTED_DIR / out_name
    tmp = out_path.with_suffix(".tmp")
    payload = _enrich_result(result, primary_path, all_paths)
    payload["extracted_file"] = str(out_path.resolve())
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    tmp.replace(out_path)
    record_transaction(out_name, payload, status="extracted")
    review_folder = copy_to_review_folder(payload)
    if review_folder:
        log.warning("Routed %s to review folder: %s", out_path.name, review_folder)
    return out_path


def _request_cost(model: str, input_components: dict[str, int], output_tokens: int) -> dict:
    input_tokens = sum(int(value or 0) for value in input_components.values())
    cost = estimate_model_cost(model=model, input_tokens=input_tokens, output_tokens=output_tokens)
    return {
        **cost,
        "input_components": input_components,
    }


def _schema_tokens() -> int:
    return estimate_tokens_for_text(json.dumps(OUTPUT_CONFIG, separators=(",", ":")))


def _file_token_breakdown(paths: list[Path]) -> list[dict]:
    files = []
    for path in paths:
        try:
            size_bytes = path.stat().st_size
        except OSError:
            size_bytes = 0
        files.append({
            "name": path.name,
            "size_bytes": size_bytes,
            "estimated_media_tokens": estimate_tokens_for_file(path),
        })
    return files


def _route_file_token_breakdown(route_plan: dict) -> list[dict]:
    files = []
    for file in route_plan["files"]:
        ai_path = file.get("ai_path")
        files.append({
            "name": file["original_path"].name,
            "ai_path": str(Path(ai_path).resolve()) if ai_path else None,
            "route": file["route"],
            "size_bytes": file["original_path"].stat().st_size if file["original_path"].exists() else 0,
            "original_file_size_mb": file["original_file_size_mb"],
            "optimized_file_size_mb": file["optimized_file_size_mb"],
            "text_quality_score": file["text_preview"].get("quality_score"),
            "estimated_media_tokens": _media_token_for_file_payload(file, use_route=True),
            "estimated_old_media_tokens": _media_token_for_file_payload(file, use_route=False),
            "reason": file["reason"],
        })
    return files


def _document_label_tokens(paths: list[Path]) -> int:
    if len(paths) <= 1:
        return 0
    labels = "\n".join(
        f"Document {i + 1} of {len(paths)}: {path.name}"
        for i, path in enumerate(paths)
    )
    return estimate_tokens_for_text(labels)


def _context_token_summary(parts: dict, coa: dict, suppliers: dict, supplier_account_map: dict) -> dict:
    return {
        "coa_context_tokens": estimate_tokens_for_text(parts["coa"]),
        "coa_account_count": len(coa),
        "supplier_context_tokens": estimate_tokens_for_text(parts["suppliers"]),
        "supplier_count": len(suppliers),
        "supplier_history_tokens": estimate_tokens_for_text(parts["history"]),
        "supplier_history_count": len(supplier_account_map),
    }


def _legacy_context_token_summary(
    *,
    coa: dict,
    suppliers: dict,
    supplier_account_map: dict,
    paths: list[Path],
    category: str | None = None,
) -> dict:
    old_coa = _filter_coa_by_category(coa, category or "other")
    old_suppliers = _legacy_suppliers_for_group(suppliers, paths)
    old_history = _legacy_supplier_history_for_group(supplier_account_map, paths, old_suppliers)
    old_parts = _context_block_parts(old_coa, old_suppliers, old_history)
    return {
        "old_coa_context_tokens": estimate_tokens_for_text(old_parts["coa"]),
        "old_coa_account_count": len(old_coa),
        "old_supplier_context_tokens": estimate_tokens_for_text(old_parts["suppliers"]),
        "old_supplier_count": len(old_suppliers),
        "old_supplier_history_tokens": estimate_tokens_for_text(old_parts["history"]),
        "old_supplier_history_count": len(old_history),
    }


def _estimate_group_payload(
    paths: list[Path],
    *,
    coa: dict,
    suppliers: dict,
    supplier_account_map: dict,
    extraction_model: str,
    cost_config: dict | None = None,
    create_optimized: bool = False,
    supplier_templates: list[dict] | None = None,
    no_claude: bool = False,
    allow_claude_media: bool = False,
    provider_name: str | None = None,
) -> dict:
    cost_config = cost_config or _load_extraction_cost_control()
    route_plan = _plan_group_route(paths, cost_config, create_optimized=create_optimized)
    routing_decision = _template_routing_decision(paths, route_plan, supplier_templates)
    files = _route_file_token_breakdown(route_plan)
    old_media_tokens = sum(file["estimated_old_media_tokens"] for file in files)
    routed_media_tokens = sum(file["estimated_media_tokens"] for file in files)
    route_text_tokens = _text_tokens_for_route(route_plan)
    remembered_category = _category_from_supplier_memory(paths)
    local_category = _category_from_local_text(paths, " ".join(file.get("text") or "" for file in route_plan["files"]))
    classifier_required = remembered_category is None and local_category is None
    classifier_media_tokens = 0 if route_text_tokens else routed_media_tokens
    old_classifier_required = remembered_category is None
    classifier = {
        "required": classifier_required,
        "category_source": (
            "supplier_category_memory" if remembered_category else
            "filename_email_or_text_preview" if local_category else
            "classifier_estimate"
        ),
        "category_used_for_context_estimate": remembered_category or local_category or "other",
        "request": _request_cost(
            CLASSIFIER_MODEL,
            {
                "media_file_tokens": classifier_media_tokens if classifier_required else 0,
                "text_preview_tokens": route_text_tokens if classifier_required and route_text_tokens else 0,
                "user_instruction_context_tokens": estimate_tokens_for_text(CLASSIFIER_PROMPT) if classifier_required else 0,
            },
            CLASSIFIER_MAX_TOKENS if classifier_required else 0,
        ),
    }

    category_for_context = remembered_category or local_category or "other"
    retrieved_context = _retrieve_context_for_group(
        coa=coa,
        suppliers=suppliers,
        supplier_account_map=supplier_account_map,
        paths=paths,
        category=category_for_context,
    )
    filtered_coa = retrieved_context["accounts"]
    supplier_context = retrieved_context["suppliers"]
    history_context = retrieved_context["supplier_history"]
    context_parts = _context_block_parts(filtered_coa, supplier_context, history_context)
    context = _build_context_block(filtered_coa, supplier_context, history_context)
    legacy_context_summary = _legacy_context_token_summary(
        coa=coa,
        suppliers=suppliers,
        supplier_account_map=supplier_account_map,
        paths=paths,
        category=category_for_context,
    )
    email_context = _email_context_for_group(paths)
    instruction = _extraction_instruction(paths, context)
    output_tokens = max(len(paths), 1) * 1_200
    extraction = _request_cost(
        extraction_model,
        {
            "system_prompt_tokens": estimate_tokens_for_text(SYSTEM_PROMPT),
            "output_schema_tokens": _schema_tokens(),
            "email_context_tokens": estimate_tokens_for_text(email_context),
            "document_label_tokens": _document_label_tokens(paths),
            "media_file_tokens": routed_media_tokens,
            "local_text_preview_tokens": route_text_tokens,
            "user_instruction_context_tokens": estimate_tokens_for_text(instruction),
        },
        output_tokens,
    )
    old_classifier = _request_cost(
        CLASSIFIER_MODEL,
        {
            "media_file_tokens": old_media_tokens if old_classifier_required else 0,
            "user_instruction_context_tokens": estimate_tokens_for_text(CLASSIFIER_PROMPT) if old_classifier_required else 0,
        },
        CLASSIFIER_MAX_TOKENS if old_classifier_required else 0,
    )
    old_extraction = _request_cost(
        extraction_model,
        {
            "system_prompt_tokens": estimate_tokens_for_text(SYSTEM_PROMPT),
            "output_schema_tokens": _schema_tokens(),
            "email_context_tokens": estimate_tokens_for_text(email_context),
            "document_label_tokens": _document_label_tokens(paths),
            "media_file_tokens": old_media_tokens,
            "user_instruction_context_tokens": estimate_tokens_for_text(instruction),
        },
        output_tokens,
    )
    estimated_cost_before = round(old_classifier["estimated_cost_aed"] + old_extraction["estimated_cost_aed"], 4)
    if routing_decision["route"] == ROUTE_NO_AI_TEMPLATE:
        no_ai_extraction = _request_cost(extraction_model, {}, 0)
        no_ai_classifier = {
            "required": False,
            "category_source": "supplier_template",
            "category_used_for_context_estimate": None,
            "request": _request_cost(CLASSIFIER_MODEL, {}, 0),
        }
        return {
            "type": "document_group",
            "files": files,
            "file_count": len(paths),
            "route_plan": route_plan,
            "progressive_route": routing_decision,
            "extraction_route": ROUTE_NO_AI_TEMPLATE,
            "text_quality_score": route_plan["text_quality_score"],
            "original_file_size_mb": route_plan["original_file_size_mb"],
            "optimized_file_size_mb": route_plan["optimized_file_size_mb"],
            "reason": routing_decision["reason"],
            "template_id": routing_decision["template_id"],
            "supplier_matched_by_template": (routing_decision.get("extracted") or {}).get("supplier_name"),
            "claude_blocked": False,
            "provider_required": False,
            "provider_parser_count": 0,
            "provider_estimate": {},
            "estimated_provider_cost_aed": 0.0,
            "estimated_claude_fallback_cost_aed": 0.0,
            "estimated_blended_cost_aed": 0.0,
            "estimated_cost_if_claude_enabled": 0.0,
            "estimated_cost_if_claude_disabled": 0.0,
            "estimated_cost_before": estimated_cost_before,
            "estimated_cost_after": 0.0,
            "estimated_savings_aed": estimated_cost_before,
            "classifier": no_ai_classifier,
            "extraction": {
                **no_ai_extraction,
                "coa_context_tokens": 0,
                "coa_account_count": 0,
                "supplier_context_tokens": 0,
                "supplier_count": 0,
                "supplier_history_tokens": 0,
                "supplier_history_count": 0,
            },
            "estimated_input_tokens": 0,
            "estimated_output_tokens": 0,
            "estimated_cost_aed": 0.0,
            "estimated_cost_usd": 0.0,
        }
    context_summary = {
        **_context_token_summary(context_parts, filtered_coa, supplier_context, history_context),
        **legacy_context_summary,
    }
    claude_enabled_cost_after = round(classifier["request"]["estimated_cost_aed"] + extraction["estimated_cost_aed"], 4)
    extraction_route = route_plan["route"]
    reason = route_plan["reason"]
    actual_classifier = classifier
    actual_extraction = extraction
    actual_input_tokens = classifier["request"]["estimated_input_tokens"] + extraction["estimated_input_tokens"]
    actual_output_tokens = classifier["request"]["estimated_output_tokens"] + extraction["estimated_output_tokens"]
    actual_cost_aed = claude_enabled_cost_after
    actual_cost_usd = round(classifier["request"]["estimated_cost_usd"] + extraction["estimated_cost_usd"], 4)
    claude_blocked = False
    provider_parser_count = 0
    provider_required = False
    primary_provider = _effective_provider_name(provider_name)
    provider_cost = _estimate_provider_cost_for_route(route_plan, primary_provider)
    provider_cost_aed = float(provider_cost["estimated_cost_aed"])
    provider_cost_usd = float(provider_cost["estimated_cost_usd"])
    fallback_rate = float(load_provider_config().get("target_claude_fallback_rate") or 0.05)
    estimated_claude_fallback_cost = round(claude_enabled_cost_after * fallback_rate, 4)
    estimated_blended_cost = round(provider_cost_aed + estimated_claude_fallback_cost, 4)

    if no_claude and route_plan["route"] == "text_only":
        extraction_route = ROUTE_LOCAL_TEXT_RULES
        reason = _claude_required_reason(route_plan, routing_decision, no_claude=True)
        actual_classifier = {
            "required": False,
            "category_source": "no_claude_local_text",
            "category_used_for_context_estimate": None,
            "request": _request_cost(CLASSIFIER_MODEL, {}, 0),
        }
        actual_extraction = {
            **_request_cost(extraction_model, {}, 0),
            **context_summary,
        }
        actual_input_tokens = 0
        actual_output_tokens = 0
        actual_cost_aed = 0.0
        actual_cost_usd = 0.0
        estimated_blended_cost = 0.0
        estimated_claude_fallback_cost = 0.0
    elif (
        no_claude
        and not primary_provider
        and route_plan["route"] in {"optimized_media", "full_media_requires_confirmation", "skip_needs_review"}
    ):
        extraction_route = ROUTE_CLAUDE_REQUIRED
        reason = _claude_required_reason(route_plan, routing_decision, no_claude=True)
        actual_classifier = {
            "required": False,
            "category_source": "claude_required_queue",
            "category_used_for_context_estimate": None,
            "request": _request_cost(CLASSIFIER_MODEL, {}, 0),
        }
        actual_extraction = {
            **_request_cost(extraction_model, {}, 0),
            **context_summary,
        }
        actual_input_tokens = 0
        actual_output_tokens = 0
        actual_cost_aed = 0.0
        actual_cost_usd = 0.0
        claude_blocked = True
        estimated_blended_cost = 0.0
        estimated_claude_fallback_cost = 0.0
    elif not allow_claude_media:
        extraction_route = ROUTE_PROVIDER_PARSER if primary_provider else ROUTE_PROVIDER_REQUIRED
        reason = (
            f"Primary low-cost provider {primary_provider} selected for extraction."
            if primary_provider else
            _provider_required_reason(route_plan, routing_decision)
        )
        actual_classifier = {
            "required": False,
            "category_source": "low_cost_provider",
            "category_used_for_context_estimate": primary_provider,
            "request": _request_cost(CLASSIFIER_MODEL, {}, 0),
        }
        actual_extraction = {
            **{
                "model": primary_provider or "benchmark_placeholder_provider",
                "estimated_input_tokens": 0,
                "estimated_output_tokens": 0,
                "estimated_cost_usd": provider_cost_usd,
                "estimated_cost_aed": provider_cost_aed,
                "input_components": {},
            },
            **context_summary,
        }
        actual_input_tokens = 0
        actual_output_tokens = 0
        actual_cost_aed = provider_cost_aed
        actual_cost_usd = provider_cost_usd
        provider_parser_count = len(paths) if primary_provider else 0
        provider_required = not bool(primary_provider)
        if no_claude:
            estimated_claude_fallback_cost = 0.0
            estimated_blended_cost = provider_cost_aed

    return {
        "type": "document_group",
        "files": files,
        "file_count": len(paths),
        "route_plan": route_plan,
        "progressive_route": routing_decision,
        "extraction_route": extraction_route,
        "text_quality_score": route_plan["text_quality_score"],
        "original_file_size_mb": route_plan["original_file_size_mb"],
        "optimized_file_size_mb": route_plan["optimized_file_size_mb"],
        "reason": reason,
        "template_id": routing_decision["template_id"],
        "supplier_matched_by_template": None,
        "claude_blocked": claude_blocked,
        "provider_required": provider_required,
        "provider_parser_count": provider_parser_count,
        "provider_name": primary_provider,
        "google_planned_count": len(paths) if primary_provider == "google" else 0,
        "google_accepted_count": 0,
        "google_rejected_count": 0,
        "claude_fallback_required_count": len(paths) if primary_provider == "google" and not no_claude else 0,
        "provider_estimate": provider_cost,
        "estimated_provider_cost_aed": provider_cost_aed,
        "estimated_claude_fallback_cost_aed": estimated_claude_fallback_cost,
        "estimated_blended_cost_aed": estimated_blended_cost,
        "estimated_cost_if_claude_enabled": claude_enabled_cost_after,
        "estimated_cost_if_claude_disabled": provider_cost_aed if primary_provider else 0.0,
        "estimated_cost_before": estimated_cost_before,
        "estimated_cost_after": actual_cost_aed,
        "estimated_savings_aed": round(max(0.0, estimated_cost_before - actual_cost_aed), 4),
        "classifier": actual_classifier,
        "extraction": actual_extraction,
        "estimated_input_tokens": actual_input_tokens,
        "estimated_output_tokens": actual_output_tokens,
        "estimated_cost_aed": actual_cost_aed,
        "estimated_cost_usd": actual_cost_usd,
    }


def _estimate_spreadsheet_payload(
    path: Path,
    *,
    coa: dict,
    suppliers: dict,
    supplier_account_map: dict,
    extraction_model: str,
    no_claude: bool = False,
) -> dict:
    try:
        parsed = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        parsed = {"rows": [], "source_file": str(path)}
    source_path = Path(parsed.get("source_file", str(path)))
    supplier_paths = [source_path] if source_path.exists() else [path]
    retrieved_context = _retrieve_context_for_group(
        coa=coa,
        suppliers=suppliers,
        supplier_account_map=supplier_account_map,
        paths=supplier_paths,
        category="other",
    )
    filtered_coa = retrieved_context["accounts"]
    supplier_context = retrieved_context["suppliers"]
    history_context = retrieved_context["supplier_history"]
    context_parts = _context_block_parts(filtered_coa, supplier_context, history_context)
    context = _build_context_block(filtered_coa, supplier_context, history_context)
    legacy_context_summary = _legacy_context_token_summary(
        coa=coa,
        suppliers=suppliers,
        supplier_account_map=supplier_account_map,
        paths=supplier_paths,
        category="other",
    )
    try:
        instruction = _spreadsheet_instruction(parsed, context)
    except ValueError:
        instruction = context
    request = _request_cost(
        extraction_model,
        {
            "system_prompt_tokens": estimate_tokens_for_text(SYSTEM_PROMPT),
            "output_schema_tokens": _schema_tokens(),
            "email_context_tokens": 0,
            "document_label_tokens": 0,
            "media_file_tokens": 0,
            "user_instruction_context_tokens": estimate_tokens_for_text(instruction),
        },
        1_200,
    )
    route = ROUTE_CLAUDE_REQUIRED if no_claude else "parsed_spreadsheet_text"
    provider_cost = estimate_provider_cost(1, provider_name=get_primary_provider_name(load_provider_config()), config=load_provider_config())
    actual_request = _request_cost(extraction_model, {}, 0) if no_claude else request
    actual_cost = 0.0 if no_claude else request["estimated_cost_aed"]
    actual_cost_usd = 0.0 if no_claude else request["estimated_cost_usd"]
    actual_input = 0 if no_claude else request["estimated_input_tokens"]
    actual_output = 0 if no_claude else request["estimated_output_tokens"]
    return {
        "type": "parsed_spreadsheet",
        "files": _file_token_breakdown([path]),
        "file_count": 1,
        "classifier": {
            "required": False,
            "category_source": "not_used_for_spreadsheets",
            "category_used_for_context_estimate": None,
            "request": _request_cost(CLASSIFIER_MODEL, {}, 0),
        },
        "extraction_route": route,
        "text_quality_score": None,
        "original_file_size_mb": file_size_mb(path),
        "optimized_file_size_mb": 0.0,
        "reason": "Claude disabled; parsed spreadsheet queued for review." if no_claude else "parsed spreadsheet uses local row text",
        "claude_blocked": bool(no_claude),
        "provider_required": False,
        "provider_parser_count": 0,
        "provider_estimate": provider_cost,
        "estimated_provider_cost_aed": 0.0 if no_claude else provider_cost["estimated_cost_aed"],
        "estimated_claude_fallback_cost_aed": 0.0 if no_claude else round(request["estimated_cost_aed"] * float(load_provider_config().get("target_claude_fallback_rate") or 0.05), 4),
        "estimated_blended_cost_aed": 0.0 if no_claude else round(provider_cost["estimated_cost_aed"] + (request["estimated_cost_aed"] * float(load_provider_config().get("target_claude_fallback_rate") or 0.05)), 4),
        "estimated_cost_if_claude_enabled": request["estimated_cost_aed"],
        "estimated_cost_if_claude_disabled": 0.0,
        "estimated_cost_before": request["estimated_cost_aed"],
        "estimated_cost_after": actual_cost,
        "estimated_savings_aed": round(max(0.0, request["estimated_cost_aed"] - actual_cost), 4),
        "extraction": {
            **actual_request,
            **_context_token_summary(context_parts, filtered_coa, supplier_context, history_context),
            **legacy_context_summary,
        },
        "estimated_input_tokens": actual_input,
        "estimated_output_tokens": actual_output,
        "estimated_cost_aed": actual_cost,
        "estimated_cost_usd": actual_cost_usd,
    }


def _estimate_planned_payloads(
    groups: list[list[Path]],
    parsed_files: list[Path],
    *,
    coa: dict | None,
    suppliers: dict | None,
    supplier_account_map: dict | None,
    extraction_model: str,
    cost_config: dict | None = None,
    create_optimized: bool = False,
    supplier_templates: list[dict] | None = None,
    no_claude: bool = False,
    allow_claude_media: bool = False,
    provider_name: str | None = None,
) -> dict:
    coa = coa or {}
    suppliers = suppliers or {}
    supplier_account_map = supplier_account_map or {}
    request_estimates = [
        _estimate_group_payload(
            group,
            coa=coa,
            suppliers=suppliers,
            supplier_account_map=supplier_account_map,
            extraction_model=extraction_model,
            cost_config=cost_config,
            create_optimized=create_optimized,
            supplier_templates=supplier_templates,
            no_claude=no_claude,
            allow_claude_media=allow_claude_media,
            provider_name=provider_name,
        )
        for group in groups
    ]
    request_estimates.extend(
        _estimate_spreadsheet_payload(
            path,
            coa=coa,
            suppliers=suppliers,
            supplier_account_map=supplier_account_map,
            extraction_model=extraction_model,
            no_claude=no_claude,
        )
        for path in parsed_files
    )
    old_supplier_tokens = sum(int((item.get("extraction") or {}).get("old_supplier_context_tokens") or 0) for item in request_estimates)
    new_supplier_tokens = sum(int((item.get("extraction") or {}).get("supplier_context_tokens") or 0) for item in request_estimates)
    old_coa_tokens = sum(int((item.get("extraction") or {}).get("old_coa_context_tokens") or 0) for item in request_estimates)
    new_coa_tokens = sum(int((item.get("extraction") or {}).get("coa_context_tokens") or 0) for item in request_estimates)
    return {
        "request_estimates": request_estimates,
        "estimated_input_tokens": sum(item["estimated_input_tokens"] for item in request_estimates),
        "estimated_output_tokens": sum(item["estimated_output_tokens"] for item in request_estimates),
        "estimated_cost_aed": round(sum(item["estimated_cost_aed"] for item in request_estimates), 4),
        "estimated_cost_usd": round(sum(item["estimated_cost_usd"] for item in request_estimates), 4),
        "estimated_cost_before_aed": round(sum(item["estimated_cost_before"] for item in request_estimates), 4),
        "estimated_cost_after_aed": round(sum(item["estimated_cost_after"] for item in request_estimates), 4),
        "estimated_cost_if_claude_enabled_aed": round(sum(item.get("estimated_cost_if_claude_enabled", item["estimated_cost_after"]) for item in request_estimates), 4),
        "estimated_cost_if_claude_disabled_aed": round(sum(item.get("estimated_cost_if_claude_disabled", 0.0) for item in request_estimates), 4),
        "estimated_provider_cost_aed": round(sum(float(item.get("estimated_provider_cost_aed") or 0.0) for item in request_estimates), 4),
        "estimated_claude_fallback_cost_aed": round(sum(float(item.get("estimated_claude_fallback_cost_aed") or 0.0) for item in request_estimates), 4),
        "estimated_blended_cost_aed": round(sum(float(item.get("estimated_blended_cost_aed") or 0.0) for item in request_estimates), 4),
        "estimated_savings_aed": round(sum(item["estimated_savings_aed"] for item in request_estimates), 4),
        "old_supplier_context_tokens": old_supplier_tokens,
        "new_supplier_context_tokens": new_supplier_tokens,
        "old_coa_context_tokens": old_coa_tokens,
        "new_coa_context_tokens": new_coa_tokens,
        "retrieval_context_tokens_saved": max(0, (old_supplier_tokens + old_coa_tokens) - (new_supplier_tokens + new_coa_tokens)),
        "classifier_request_count": sum(1 for item in request_estimates if item["classifier"]["required"]),
        "no_ai_document_count": sum(item["file_count"] for item in request_estimates if item.get("extraction_route") == ROUTE_NO_AI_TEMPLATE),
        "local_text_document_count": sum(item["file_count"] for item in request_estimates if item.get("extraction_route") in {ROUTE_LOCAL_TEXT_RULES, "text_only"}),
        "provider_document_count": sum(int(item.get("provider_parser_count") or 0) for item in request_estimates),
        "provider_required_count": sum(item["file_count"] for item in request_estimates if item.get("provider_required")),
        "google_document_count": sum(int(item.get("google_planned_count") or 0) for item in request_estimates),
        "google_accepted_count": sum(int(item.get("google_accepted_count") or 0) for item in request_estimates),
        "google_rejected_count": sum(int(item.get("google_rejected_count") or 0) for item in request_estimates),
        "claude_fallback_required_count": sum(int(item.get("claude_fallback_required_count") or 0) for item in request_estimates),
        "claude_required_count": sum(item["file_count"] for item in request_estimates if item.get("extraction_route") == ROUTE_CLAUDE_REQUIRED),
        "claude_blocked_count": sum(item["file_count"] for item in request_estimates if item.get("claude_blocked")),
        "claude_calls_avoided": sum(1 for item in request_estimates if item.get("extraction_route") == ROUTE_NO_AI_TEMPLATE),
        "suppliers_matched_by_template": sorted({
            str(item.get("supplier_matched_by_template"))
            for item in request_estimates
            if item.get("supplier_matched_by_template")
        }),
        "route_counts": {
            route: sum(1 for item in request_estimates if item.get("extraction_route") == route)
            for route in sorted({item.get("extraction_route") for item in request_estimates})
        },
    }


def _apply_max_docs(
    groups: list[list[Path]],
    parsed_files: list[Path],
    max_docs: int | None,
) -> tuple[list[list[Path]], list[Path]]:
    if max_docs is None:
        return groups, parsed_files
    if max_docs < 0:
        raise ValueError("--max-docs must be zero or greater")
    limited_groups = groups[:max_docs]
    remaining = max(0, max_docs - len(limited_groups))
    return limited_groups, parsed_files[:remaining]


def _estimate_api_impact(groups: list[list[Path]], parsed_files: list[Path]) -> str:
    file_count = sum(len(group) for group in groups)
    parsed_count = len(parsed_files)
    return (
        f"{len(groups)} document group(s), {file_count} raw file(s), "
        f"{parsed_count} parsed spreadsheet(s). Cost depends on page/image size."
    )


def _is_reprocessing_old_examples(stale_info: dict, groups: list[list[Path]], parsed_files: list[Path]) -> bool:
    return bool(stale_info.get("stale") and (groups or parsed_files))


def _build_extraction_plan(
    groups: list[list[Path]],
    parsed_files: list[Path],
    stale_info: dict,
    *,
    max_docs: int | None = None,
    coa: dict | None = None,
    suppliers: dict | None = None,
    supplier_account_map: dict | None = None,
    cost_config: dict | None = None,
    max_cost_aed: float | None = None,
    create_optimized: bool = False,
    supplier_templates: list[dict] | None = None,
    no_claude: bool = False,
    allow_claude_media: bool = False,
    low_cost_extract: bool = False,
    provider_name: str | None = None,
    allow_claude_fallback: bool = False,
) -> dict:
    limited_groups, limited_parsed = _apply_max_docs(groups, parsed_files, max_docs)
    model_routing = load_model_routing_config()
    model = str(model_routing.get("default_model") or MODEL)
    cost_config = cost_config or _load_extraction_cost_control()
    estimate = _estimate_planned_payloads(
        limited_groups,
        limited_parsed,
        coa=coa,
        suppliers=suppliers,
        supplier_account_map=supplier_account_map,
        extraction_model=model,
        cost_config=cost_config,
        create_optimized=create_optimized,
        supplier_templates=supplier_templates,
        no_claude=no_claude,
        allow_claude_media=allow_claude_media,
        provider_name=provider_name,
    )
    cost_cap = (
        float(max_cost_aed)
        if max_cost_aed is not None
        else float(cost_config.get("max_total_run_cost_aed_without_confirmation") or 0)
    )
    monthly_guard = cost_guard_status(estimate)
    run_guard = extraction_run_cost_guard(
        estimate,
        controls={"extraction_run_cost_block_aed": cost_cap},
    )
    file_count = sum(len(group) for group in limited_groups)
    return {
        **estimate,
        "documents_processed": file_count + len(limited_parsed),
        "model": model,
        "document_groups_count": len(limited_groups),
        "file_count": file_count,
        "parsed_spreadsheets_count": len(limited_parsed),
        "stale_file_count": len(stale_info.get("files") or []) if stale_info.get("stale") else 0,
        "reprocessing_old_examples": _is_reprocessing_old_examples(stale_info, limited_groups, limited_parsed),
        "stale_reprocessing_driver": (
            stale_info.get("reason") if _is_reprocessing_old_examples(stale_info, limited_groups, limited_parsed) else ""
        ),
        "max_docs": max_docs,
        "limited_by_max_docs": max_docs is not None and (len(limited_groups) < len(groups) or len(limited_parsed) < len(parsed_files)),
        "groups": limited_groups,
        "parsed_files": limited_parsed,
        "guard": monthly_guard,
        "run_guard": run_guard,
        "model_routing": model_routing,
        "cost_config": cost_config,
        "no_claude": no_claude,
        "low_cost_extract": low_cost_extract,
        "allow_claude_media": allow_claude_media,
        "provider_name": _effective_provider_name(provider_name),
        "allow_claude_fallback": allow_claude_fallback,
        "primary_provider": _effective_provider_name(provider_name),
        "no_primary_provider_configured": _effective_provider_name(provider_name) is None,
        "estimator_note": (
            "Local estimate only: text tokens use chars/4; PDF/image media tokens use file-size proxy; "
            "this is not Anthropic tokenizer output."
        ),
    }


def _print_cost_estimate(plan: dict) -> dict:
    guard = plan["guard"]
    run_guard = plan["run_guard"]
    print("  document groups count:", plan["document_groups_count"])
    print("  file count:", plan["file_count"])
    print("  parsed spreadsheets count:", plan["parsed_spreadsheets_count"])
    print("  stale file count:", plan["stale_file_count"])
    print("  reprocessing old examples:", "yes" if plan["reprocessing_old_examples"] else "no")
    if plan["max_docs"] is not None:
        print("  max docs limit:", plan["max_docs"])
        print("  limited by max docs:", "yes" if plan["limited_by_max_docs"] else "no")
    print("  selected model:", plan["model"])
    print("  classifier model:", CLASSIFIER_MODEL)
    print("  classifier requests estimated:", plan["classifier_request_count"])
    if plan.get("no_claude"):
        print("  extraction mode: No Claude diagnostic mode")
    elif plan.get("low_cost_extract"):
        print("  extraction mode: low-cost provider-first mode")
    else:
        print("  extraction mode: balanced provider-first planning")
    print("  primary low-cost provider:", plan.get("primary_provider") or "not configured")
    if plan.get("no_primary_provider_configured") and not plan.get("no_claude"):
        print("  provider warning: No primary low-cost provider configured")
    print("  no-AI document count:", plan["no_ai_document_count"])
    print("  local text document count:", plan["local_text_document_count"])
    print("  provider document count:", plan["provider_document_count"])
    print("  Google-routed document count:", plan.get("google_document_count", 0))
    print("  Google-accepted document count:", plan.get("google_accepted_count", 0))
    print("  Google-rejected/needs review count:", plan.get("google_rejected_count", 0))
    print("  low-cost provider required count:", plan["provider_required_count"])
    print("  Claude fallback required count:", plan.get("claude_fallback_required_count", 0))
    print("  Claude-required document count:", plan["claude_required_count"])
    print("  Claude blocked document count:", plan["claude_blocked_count"])
    print("  Claude calls avoided:", plan["claude_calls_avoided"])
    print("  suppliers matched by template:", ", ".join(plan["suppliers_matched_by_template"]) or "none")
    print("  estimated old cost AED:", f"AED {plan['estimated_cost_before_aed']:.4f}")
    print("  estimated optimized cost AED:", f"AED {plan['estimated_cost_after_aed']:.4f}")
    print("  estimated cheap provider cost:", f"AED {plan['estimated_provider_cost_aed']:.4f}")
    print("  estimated Claude fallback cost:", f"AED {plan['estimated_claude_fallback_cost_aed']:.4f}")
    print("  estimated blended cost:", f"AED {plan['estimated_blended_cost_aed']:.4f}")
    print("  target Claude fallback rate:", f"{float(load_provider_config().get('target_claude_fallback_rate') or 0.05):.0%}")
    print("  estimated cost if Claude disabled:", f"AED {plan['estimated_cost_if_claude_disabled_aed']:.4f}")
    print("  estimated cost if Claude enabled:", f"AED {plan['estimated_cost_if_claude_enabled_aed']:.4f}")
    print("  estimated savings AED:", f"AED {plan['estimated_savings_aed']:.4f}")
    print("  old supplier context tokens:", plan.get("old_supplier_context_tokens", 0))
    print("  new supplier context tokens:", plan.get("new_supplier_context_tokens", 0))
    print("  old CoA context tokens:", plan.get("old_coa_context_tokens", 0))
    print("  new CoA context tokens:", plan.get("new_coa_context_tokens", 0))
    print("  retrieval context tokens saved:", plan.get("retrieval_context_tokens_saved", 0))
    print("  route counts:", ", ".join(f"{route}={count}" for route, count in plan["route_counts"].items()) or "none")
    print("  estimated input tokens:", plan["estimated_input_tokens"])
    print("  estimated output tokens:", plan["estimated_output_tokens"])
    print("  estimated cost AED:", f"AED {plan['estimated_cost_aed']:.4f}")
    print("  estimate note:", plan["estimator_note"])
    if plan.get("stale_reprocessing_driver"):
        print("  stale reprocessing driver:", plan["stale_reprocessing_driver"])
    print("  per-run cost cap:", f"AED {run_guard['run_cost_block_aed']:.2f}")
    if run_guard["block"]:
        print("  high-cost block: estimate exceeds the configured per-run cap")
    print("  model routing config:", "loaded; future routes disabled")
    print("  estimated documents processed:", plan["documents_processed"])
    print("  model:", plan["model"])
    print("  estimated run cost:", f"AED {plan['estimated_cost_aed']:.4f}")
    print("  projected monthly AI cost:", f"AED {guard['monthly_estimated_cost_aed']:.4f}",
          f"/ AED {guard['monthly_limit_aed']:.2f}")
    if guard["warn"]:
        print("  cost warning: projected usage is above the configured warning threshold")
    if guard["block"]:
        print("  cost block: projected usage is above the configured monthly block threshold")
    return plan


def _print_top_expensive_documents(plan: dict) -> None:
    items = sorted(
        plan.get("request_estimates") or [],
        key=lambda item: item.get("estimated_cost_if_claude_enabled", item.get("estimated_cost_after", item.get("estimated_cost_aed", 0))),
        reverse=True,
    )[:5]
    if not items:
        return
    print("\n  top 5 most expensive planned groups:")
    for index, item in enumerate(items, 1):
        names = ", ".join(file["name"] for file in item["files"])
        print(
            f"  {index}. AED {item.get('estimated_cost_if_claude_enabled', item['estimated_cost_after']):.4f} if Claude enabled "
            f"({item['extraction_route']}): {names}"
        )


def _print_component_line(label: str, value: int, suffix: str = "tokens") -> None:
    print(f"      {label}: {value} {suffix}")


def _print_request_breakdown(plan: dict) -> None:
    if not plan.get("request_estimates"):
        return
    print("\n  request group breakdown:")
    for index, item in enumerate(plan["request_estimates"], 1):
        names = ", ".join(file["name"] for file in item["files"])
        print(f"  [{index}] {item['type']}: {names}")
        print(f"      files: {item['file_count']}")
        print(f"      extraction_route: {item['extraction_route']}")
        if item.get("template_id"):
            print(f"      template_id: {item['template_id']}")
        if item.get("claude_blocked"):
            print("      claude status: blocked/queued")
        if item.get("provider_required"):
            print("      provider status: required/not configured")
        print(f"      original_file_size_mb: {item['original_file_size_mb']:.4f}")
        print(f"      optimized_file_size_mb: {item['optimized_file_size_mb']:.4f}")
        print(f"      text_quality_score: {item['text_quality_score']}")
        print(f"      estimated_cost_before: AED {item['estimated_cost_before']:.4f}")
        print(f"      estimated_cost_after: AED {item['estimated_cost_after']:.4f}")
        print(f"      estimated_provider_cost: AED {float(item.get('estimated_provider_cost_aed') or 0.0):.4f}")
        print(f"      estimated_blended_cost: AED {float(item.get('estimated_blended_cost_aed') or 0.0):.4f}")
        print(f"      estimated_cost_if_claude_enabled: AED {item.get('estimated_cost_if_claude_enabled', item['estimated_cost_after']):.4f}")
        print(f"      estimated_savings_aed: AED {item['estimated_savings_aed']:.4f}")
        print(f"      reason: {item['reason']}")
        classifier = item["classifier"]
        print(
            "      classifier pass:",
            "yes" if classifier["required"] else "no",
            f"({classifier['category_source']})",
        )
        if classifier["required"]:
            request = classifier["request"]
            print(
                f"      classifier estimate: {request['model']}, "
                f"{request['estimated_input_tokens']} input, "
                f"{request['estimated_output_tokens']} output, "
                f"AED {request['estimated_cost_aed']:.4f}"
            )
        extraction = item["extraction"]
        print(
            f"      extraction estimate: {extraction['model']}, "
            f"{extraction['estimated_input_tokens']} input, "
            f"{extraction['estimated_output_tokens']} output, "
            f"AED {extraction['estimated_cost_aed']:.4f}"
        )
        components = extraction["input_components"]
        _print_component_line("system prompt", components.get("system_prompt_tokens", 0))
        _print_component_line("output schema", components.get("output_schema_tokens", 0))
        _print_component_line("email context", components.get("email_context_tokens", 0))
        _print_component_line("document labels", components.get("document_label_tokens", 0))
        _print_component_line("media/file estimate", components.get("media_file_tokens", 0))
        _print_component_line("local text preview", components.get("local_text_preview_tokens", 0))
        _print_component_line("user instruction/context", components.get("user_instruction_context_tokens", 0))
        old_context_tokens = (
            int(extraction.get("old_supplier_context_tokens") or 0)
            + int(extraction.get("old_coa_context_tokens") or 0)
        )
        new_context_tokens = (
            int(extraction.get("supplier_context_tokens") or 0)
            + int(extraction.get("coa_context_tokens") or 0)
        )
        _print_component_line(
            "old CoA context",
            extraction.get("old_coa_context_tokens", 0),
            f"tokens across {extraction.get('old_coa_account_count', 0)} account(s)",
        )
        _print_component_line(
            "new CoA context",
            extraction.get("coa_context_tokens", 0),
            f"tokens across {extraction.get('coa_account_count', 0)} account(s)",
        )
        _print_component_line(
            "old supplier context",
            extraction.get("old_supplier_context_tokens", 0),
            f"tokens across {extraction.get('old_supplier_count', 0)} supplier(s)",
        )
        _print_component_line(
            "new supplier context",
            extraction.get("supplier_context_tokens", 0),
            f"tokens across {extraction.get('supplier_count', 0)} supplier(s)",
        )
        _print_component_line(
            "retrieval context saved",
            max(0, old_context_tokens - new_context_tokens),
        )
        _print_component_line(
            "supplier history",
            extraction.get("supplier_history_tokens", 0),
            f"tokens across {extraction.get('supplier_history_count', 0)} mapping(s)",
        )
        for file in item["files"]:
            print(
                f"      file media estimate: {file['estimated_media_tokens']} tokens "
                f"for {file['name']} ({file['size_bytes']} bytes)"
            )
            print(
                f"      file route: {file['route']} | original MB {file['original_file_size_mb']:.4f} | "
                f"optimized MB {file['optimized_file_size_mb']:.4f} | "
                f"text quality {file['text_quality_score']} | {file['reason']}"
            )
        print(
            f"      group total: {item['estimated_input_tokens']} input, "
            f"{item['estimated_output_tokens']} output, "
            f"AED {item['estimated_cost_aed']:.4f}"
        )


def _print_dry_run_summary(plan: dict, stale_info: dict) -> None:
    print("\nAI extraction dry run")
    print("  No Claude call made")
    print("  No QBO action made")
    print("  This is estimate only")
    print(f"  stale ledger: {stale_info['stale']} ({stale_info['reason'] or 'no version mismatch'})")
    print(f"  estimated impact: {_estimate_api_impact(plan['groups'], plan['parsed_files'])}")
    _print_cost_estimate(plan)
    _print_top_expensive_documents(plan)
    _print_request_breakdown(plan)
    if plan["run_guard"]["block"]:
        suggested_cap = min(float(plan["run_guard"]["run_cost_block_aed"] or 10), 10.0) or 10.0
        print(f"  recommended next command: main.py --dry-run --extract --max-docs 10 --max-cost-aed {suggested_cap:g}")
    elif plan["documents_processed"]:
        command = "main.py --extract"
        if plan["max_docs"] is not None:
            command += f" --max-docs {plan['max_docs']}"
        print(f"  recommended next command: {command}")
    else:
        print("  recommended next command: add new test files, then run main.py --dry-run --extract")


def _print_high_cost_block(plan: dict) -> None:
    print("\nExtraction blocked by cost control")
    print(f"  estimated cost AED: AED {plan['estimated_cost_aed']:.4f}")
    print(f"  configured per-run cap: AED {plan['run_guard']['run_cost_block_aed']:.2f}")
    print("  no Claude call made")
    print("  no QBO action made")
    print("  recommended next command: main.py --dry-run --extract --max-docs 10")


def _print_cost_estimate_legacy(groups: list[list[Path]], parsed_files: list[Path]) -> dict:
    estimate = estimate_extraction_cost(groups, parsed_files, model=MODEL)
    guard = cost_guard_status(estimate)
    print("  estimated documents processed:", estimate["documents_processed"])
    print("  model:", estimate["model"])
    print("  estimated input tokens:", estimate["estimated_input_tokens"])
    print("  estimated output tokens:", estimate["estimated_output_tokens"])
    print("  estimated run cost:", f"AED {estimate['estimated_cost_aed']:.4f}")
    print("  projected monthly AI cost:", f"AED {guard['monthly_estimated_cost_aed']:.4f}",
          f"/ AED {guard['monthly_limit_aed']:.2f}")
    if guard["warn"]:
        print("  cost warning: projected usage is above the configured warning threshold")
    if guard["block"]:
        print("  cost block: projected usage is above the configured block threshold")
    return {**estimate, "guard": guard}


def _confirm_stale_reprocessing(
    stale_info: dict,
    groups: list[list[Path]],
    parsed_files: list[Path],
    assume_yes: bool,
    *,
    provider_name: str | None = None,
) -> bool:
    if not stale_info["stale"] or not groups and not parsed_files:
        return True
    summary = _estimate_api_impact(groups, parsed_files)
    effective_provider = _effective_provider_name(provider_name)
    if effective_provider == "google":
        log.warning(
            "Stale extraction ledger detected: %s. Google extraction will regenerate outputs and archive stale JSON files. Planned impact: %s",
            stale_info["reason"],
            summary,
        )
        return True
    log.warning(
        "Stale extraction ledger detected: %s. Re-extraction is necessary to regenerate "
        "required schema fields such as accounting_treatment, qbo_posting_type, branch, "
        "confidence scores, and review routing. Planned impact: %s",
        stale_info["reason"],
        summary,
    )
    print("\nSTALE EXTRACTION LEDGER DETECTED")
    print(f"  reason: {stale_info['reason']}")
    print(f"  stale ledger files: {len(stale_info['files'])}")
    print(f"  planned reprocessing: {summary}")
    print("  old extracted JSON files will be archived before replacement.")
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        log.warning("Refusing stale re-extraction without interactive confirmation or assume_yes=True.")
        return False
    answer = input("Proceed with live Claude re-extraction? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def run(
    dry_run: bool = False,
    assume_yes: bool = False,
    max_docs: int | None = None,
    max_cost_aed: float | None = None,
    confirm_high_cost: bool = False,
    no_claude: bool = False,
    low_cost_extract: bool = False,
    provider_name: str | None = None,
    allow_claude_fallback: bool = False,
) -> list[dict]:
    processed = _load_processed()
    stale_info = _stale_ledger_info()

    coa = load_coa()
    suppliers = load_suppliers()
    supplier_account_map = load_supplier_account_map()
    cost_config = _load_extraction_cost_control()
    supplier_templates = load_supplier_templates()
    log.info("Loaded %d accounts, %d suppliers, %d supplier→account mappings",
             len(coa), len(suppliers), len(supplier_account_map))

    results = []
    groups = []
    new_groups = []
    parsed_files = []

    if RAW_DIR.exists():
        doc_files = sorted(
            p for p in RAW_DIR.iterdir()
            if p.suffix.lower() in DOCUMENT_EXTENSIONS and p.is_file()
        )
        groups = _group_by_message(doc_files)
        # filter groups where all files already processed
        new_groups = [g for g in groups if not all(p.name in processed for p in g)]
        log.info("Found %d document group(s) in data/raw/ (%d files total)",
                 len(new_groups), sum(len(g) for g in new_groups))

    if PARSED_DIR.exists():
        parsed_files = sorted(
            p for p in PARSED_DIR.iterdir()
            if p.suffix.lower() == ".json" and p.is_file() and p.name not in processed
        )
        log.info("Found %d parsed spreadsheet(s) in data/parsed/", len(parsed_files))

    if dry_run:
        plan = _build_extraction_plan(
            new_groups,
            parsed_files,
            stale_info,
            max_docs=max_docs,
            coa=coa,
            suppliers=suppliers,
            supplier_account_map=supplier_account_map,
            cost_config=cost_config,
            max_cost_aed=max_cost_aed,
            supplier_templates=supplier_templates,
            no_claude=no_claude,
            allow_claude_media=confirm_high_cost,
            low_cost_extract=low_cost_extract,
            provider_name=provider_name,
            allow_claude_fallback=allow_claude_fallback,
        )
        _print_dry_run_summary(plan, stale_info)
        return []

    plan = _build_extraction_plan(
        new_groups,
        parsed_files,
        stale_info,
        max_docs=max_docs,
        coa=coa,
        suppliers=suppliers,
        supplier_account_map=supplier_account_map,
        cost_config=cost_config,
        max_cost_aed=max_cost_aed,
        create_optimized=confirm_high_cost,
        supplier_templates=supplier_templates,
        no_claude=no_claude,
        allow_claude_media=confirm_high_cost,
        low_cost_extract=low_cost_extract,
        provider_name=provider_name,
        allow_claude_fallback=allow_claude_fallback,
    )
    new_groups = plan["groups"]
    parsed_files = plan["parsed_files"]
    guard = plan["guard"]
    if plan["run_guard"]["block"] and not confirm_high_cost:
        _print_high_cost_block(plan)
        return []
    if guard["block"]:
        log.error(
            "Refusing extraction: projected monthly AI cost AED %.4f exceeds configured cap AED %.2f",
            guard["monthly_estimated_cost_aed"],
            guard["monthly_limit_aed"],
        )
        return []

    if not _confirm_stale_reprocessing(stale_info, new_groups, parsed_files, assume_yes, provider_name=provider_name):
        return []

    client = None

    def _client() -> anthropic.Anthropic:
        nonlocal client
        if client is None:
            api_key = _load_api_key()
            client = anthropic.Anthropic(api_key=api_key)
        return client

    # --- Raw documents (PDF / images) grouped by message ID ---
    if new_groups:
        archive_label = f"version_{stale_info['old_version']}" if stale_info["stale"] else "replaced"
        effective_provider = _effective_provider_name(provider_name)
        for group in new_groups:
            group_key = group[0].name  # ledger key = first file in group
            try:
                route_plan = _plan_group_route(group, cost_config, create_optimized=confirm_high_cost)
                progressive_decision = _template_routing_decision(group, route_plan, supplier_templates)
                use_claude_after_provider = False
                provider_result_for_context = None
                if progressive_decision["route"] == ROUTE_NO_AI_TEMPLATE:
                    result = progressive_decision["extracted"]
                    if not result:
                        raise ValueError("Template route did not include extracted transaction")
                    transactions = _normalise_transactions(result)
                    if not transactions:
                        raise ValueError("Template route did not include extracted transaction")
                    _remove_stale_outputs(group, archive_label=archive_label)
                    total = len(transactions)
                    for index, transaction in enumerate(transactions):
                        primary_path = _choose_primary_path(transaction, group)
                        out = _save_result(transaction, primary_path, group, index=index, total=total)
                        results.append(json.loads(out.read_text()))
                    for p in group:
                        processed.add(p.name)
                    _save_processed(processed)
                    record_ai_usage({
                        "documents_processed": len(group),
                        "model": "no_ai_template",
                        "estimated_input_tokens": 0,
                        "estimated_output_tokens": 0,
                        "estimated_cost_usd": 0.0,
                        "estimated_cost_aed": 0.0,
                    })
                    log.info(
                        "Extracted group [%s] with supplier template → %d transaction(s)",
                        ", ".join(p.name for p in group),
                        total,
                    )
                    continue
                if effective_provider == "google":
                    try:
                        provider_result = _parse_google_provider_group(group, route_plan)
                    except (
                        ProviderApiConfirmationRequired,
                        ProviderAdapterConfigurationError,
                        ProviderAdapterNotEnabledError,
                        OSError,
                        RuntimeError,
                    ) as exc:
                        reason = f"Google provider extraction failed: {exc}"
                        if no_claude:
                            transaction = _google_provider_failure_transaction(group, route_plan, reason)
                            _remove_stale_outputs(group, archive_label=archive_label)
                            primary_path = _choose_primary_path(transaction, group)
                            out = _save_result(transaction, primary_path, group, index=0, total=1)
                            results.append(json.loads(out.read_text()))
                            for p in group:
                                processed.add(p.name)
                            _save_processed(processed)
                            log.warning("Queued group [%s] for manual review: %s", ", ".join(p.name for p in group), reason)
                            continue
                        _record_claude_required(group, reason=reason, route_plan=route_plan, routing_decision=progressive_decision)
                        log.warning("Queued group [%s] as Claude Required: %s", ", ".join(p.name for p in group), reason)
                        continue
                    explicit_missing_required = _explicit_missing_google_required_fields(provider_result)
                    provider_result = normalize_google_provider_result(
                        provider_result,
                        suppliers=suppliers,
                        supplier_memory=supplier_account_map,
                    )
                    provider_result_for_context = provider_result
                    validation = _validate_google_provider_result(provider_result)
                    if explicit_missing_required:
                        validation["errors"] = list(dict.fromkeys(list(validation.get("errors") or []) + explicit_missing_required))
                        validation["blockers"] = list(dict.fromkeys(list(validation.get("blockers") or []) + explicit_missing_required))
                        validation["valid"] = False
                    if validation["valid"]:
                        _remove_stale_outputs(group, archive_label=archive_label)
                        primary_path = group[0]
                        transaction = _google_provider_to_transaction(
                            provider_result,
                            primary_path=primary_path,
                            all_paths=group,
                        )
                        transaction = _apply_google_validation_metadata(transaction, validation)
                        out = _save_result(transaction, primary_path, group, index=0, total=1)
                        results.append(json.loads(out.read_text()))
                        for p in group:
                            processed.add(p.name)
                        _save_processed(processed)
                        provider_cost = _estimate_provider_cost_for_route(route_plan, "google")
                        record_ai_usage({
                            "documents_processed": len(group),
                            "model": "google_document_ai",
                            "estimated_input_tokens": 0,
                            "estimated_output_tokens": 0,
                            "estimated_cost_usd": provider_cost["estimated_cost_usd"],
                            "estimated_cost_aed": provider_cost["estimated_cost_aed"],
                        })
                        log.info("Extracted group [%s] with Google Document AI", ", ".join(p.name for p in group))
                        continue
                    reason = "Google provider validation failed: " + " ".join(validation["errors"])
                    if no_claude:
                        _remove_stale_outputs(group, archive_label=archive_label)
                        primary_path = group[0]
                        transaction = _google_provider_needs_review_transaction(
                            provider_result,
                            primary_path=primary_path,
                            all_paths=group,
                            reason=reason,
                        )
                        transaction = _apply_google_validation_metadata(transaction, validation)
                        out = _save_result(transaction, primary_path, group, index=0, total=1)
                        results.append(json.loads(out.read_text()))
                        for p in group:
                            processed.add(p.name)
                        _save_processed(processed)
                        provider_cost = _estimate_provider_cost_for_route(route_plan, "google")
                        record_ai_usage({
                            "documents_processed": len(group),
                            "model": "google_document_ai",
                            "estimated_input_tokens": 0,
                            "estimated_output_tokens": 0,
                            "estimated_cost_usd": provider_cost["estimated_cost_usd"],
                            "estimated_cost_aed": provider_cost["estimated_cost_aed"],
                        })
                        log.warning("Queued group [%s] for manual review: %s", ", ".join(p.name for p in group), reason)
                        continue
                    if allow_claude_fallback and not no_claude:
                        log.warning("Google validation failed; using explicit Claude fallback for [%s]: %s", ", ".join(p.name for p in group), reason)
                        use_claude_after_provider = True
                    else:
                        _record_claude_required(group, reason=reason, route_plan=route_plan, routing_decision=progressive_decision)
                        log.warning(
                            "Queued group [%s] as Claude Required: %s",
                            ", ".join(p.name for p in group),
                            reason,
                        )
                        continue
                if no_claude and route_plan["route"] == "text_only":
                    reason = _claude_required_reason(route_plan, progressive_decision, no_claude=True)
                    transaction = _local_text_review_transaction(group, route_plan, reason)
                    _remove_stale_outputs(group, archive_label=archive_label)
                    primary_path = _choose_primary_path(transaction, group)
                    out = _save_result(transaction, primary_path, group, index=0, total=1)
                    results.append(json.loads(out.read_text()))
                    for p in group:
                        processed.add(p.name)
                    _save_processed(processed)
                    record_ai_usage({
                        "documents_processed": len(group),
                        "model": "local_text_no_claude",
                        "estimated_input_tokens": 0,
                        "estimated_output_tokens": 0,
                        "estimated_cost_usd": 0.0,
                        "estimated_cost_aed": 0.0,
                    })
                    log.info(
                        "Extracted local text for group [%s] without Claude; routed to review",
                        ", ".join(p.name for p in group),
                    )
                    continue
                elif not no_claude and not confirm_high_cost and not use_claude_after_provider:
                    reason = _provider_required_reason(route_plan, progressive_decision)
                    _record_provider_required(group, reason=reason, route_plan=route_plan, routing_decision=progressive_decision)
                    log.warning(
                        "Queued group [%s] as Provider Required: %s",
                        ", ".join(p.name for p in group),
                        reason,
                    )
                    continue
                if no_claude:
                    reason = _claude_required_reason(route_plan, progressive_decision, no_claude=True)
                    _record_claude_required(group, reason=reason, route_plan=route_plan, routing_decision=progressive_decision)
                    log.warning(
                        "Queued group [%s] as Claude Required: %s",
                        ", ".join(p.name for p in group),
                        reason,
                    )
                    continue
                active_client = _client()
                category = _classify_bill(active_client, group, route_plan)
                supplier_hint, supplier_confidence = _provider_supplier_hint(provider_result_for_context)
                retrieved_context = _retrieve_context_for_group(
                    coa=coa,
                    suppliers=suppliers,
                    supplier_account_map=supplier_account_map,
                    paths=group,
                    category=category,
                    supplier_hint=supplier_hint,
                    supplier_confidence=supplier_confidence,
                )
                filtered_coa = retrieved_context["accounts"]
                supplier_context = retrieved_context["suppliers"]
                history_context = retrieved_context["supplier_history"]
                context = _build_context_block(filtered_coa, supplier_context, history_context)
                transactions = _extract_group(active_client, group, context, route_plan)
                if not transactions:
                    raise ValueError("Claude returned no transactions")
                _remove_stale_outputs(group, archive_label=archive_label)
                saved = []
                total = len(transactions)
                for index, result in enumerate(transactions):
                    primary_path = _choose_primary_path(result, group)
                    out = _save_result(result, primary_path, group, index, total)
                    saved.append(out.name)
                    results.append(json.loads(out.read_text()))
                for p in group:
                    processed.add(p.name)
                _save_processed(processed)
                usage_estimate = _estimate_group_payload(
                    group,
                    coa=coa,
                    suppliers=suppliers,
                    supplier_account_map=supplier_account_map,
                    extraction_model=MODEL,
                    cost_config=cost_config,
                    supplier_templates=supplier_templates,
                )
                record_ai_usage({
                    "documents_processed": len(group),
                    "model": MODEL,
                    "estimated_input_tokens": usage_estimate["estimated_input_tokens"],
                    "estimated_output_tokens": usage_estimate["estimated_output_tokens"],
                    "estimated_cost_usd": usage_estimate["estimated_cost_usd"],
                    "estimated_cost_aed": usage_estimate["estimated_cost_aed"],
                })
                names = ", ".join(p.name for p in group)
                log.info("Extracted group [%s] → %s", names, ", ".join(saved))
            except (json.JSONDecodeError, anthropic.APIError, OSError, KeyError, ValueError) as exc:
                log.exception("Failed to extract group starting %s: %s", group_key, exc)

    # --- Parsed spreadsheets ---
    if parsed_files:
        archive_label = f"version_{stale_info['old_version']}" if stale_info["stale"] else "replaced"
        for path in parsed_files:
            try:
                parsed = json.loads(path.read_text())
                if not parsed.get("rows"):
                    log.info("Skipping empty parsed file: %s", path.name)
                    continue
                source_path = Path(parsed.get("source_file", str(path)))
                if not source_path.exists():
                    raise ValueError(f"Original source file not found: {source_path}")
                if no_claude:
                    _record_claude_required(
                        [source_path],
                        reason="Claude disabled; parsed spreadsheet requires Claude or a provider parser for structured extraction.",
                        route_plan={"route": ROUTE_CLAUDE_REQUIRED, "text_quality_score": None},
                        routing_decision={"route": ROUTE_CLAUDE_REQUIRED},
                    )
                    log.warning("Queued parsed spreadsheet %s as Claude Required", path.name)
                    continue
                retrieved_context = _retrieve_context_for_group(
                    coa=coa,
                    suppliers=suppliers,
                    supplier_account_map=supplier_account_map,
                    paths=[source_path],
                    category="other",
                )
                context = _build_context_block(
                    retrieved_context["accounts"],
                    retrieved_context["suppliers"],
                    retrieved_context["supplier_history"],
                )
                active_client = _client()
                transactions = _extract_spreadsheet(active_client, parsed, context)
                if not transactions:
                    raise ValueError("Claude returned no transactions")
                _remove_stale_outputs([source_path], archive_label=archive_label)
                saved = []
                total = len(transactions)
                for index, result in enumerate(transactions):
                    out = _save_result(result, source_path, [source_path], index, total)
                    saved.append(out.name)
                    results.append(json.loads(out.read_text()))
                processed.add(path.name)
                _save_processed(processed)
                usage_estimate = _estimate_spreadsheet_payload(
                    path,
                    coa=coa,
                    suppliers=suppliers,
                    supplier_account_map=supplier_account_map,
                    extraction_model=MODEL,
                )
                record_ai_usage({
                    "documents_processed": 1,
                    "model": MODEL,
                    "estimated_input_tokens": usage_estimate["estimated_input_tokens"],
                    "estimated_output_tokens": usage_estimate["estimated_output_tokens"],
                    "estimated_cost_usd": usage_estimate["estimated_cost_usd"],
                    "estimated_cost_aed": usage_estimate["estimated_cost_aed"],
                })
                log.info("Extracted: %s → %s", path.name, ", ".join(saved))
            except (json.JSONDecodeError, anthropic.APIError, OSError, KeyError, ValueError) as exc:
                log.exception("Failed to extract %s: %s", path.name, exc)

    log.info("Total extracted this run: %d", len(results))
    return results
