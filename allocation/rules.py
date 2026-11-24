import json
from pathlib import Path

from accounting.persistence import record_vendor_history

BASE_DIR = Path(__file__).resolve().parents[1]
VENDOR_RULES_FILE = BASE_DIR / "config/vendor_rules.json"
VENDOR_BRANCH_RULES_FILE = BASE_DIR / "config/vendor_branch_rules.json"
ACCOUNT_RULES_FILE = BASE_DIR / "config/account_rules.json"


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def _save(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


def remember_correction(extracted: dict) -> None:
    supplier = (extracted.get("supplier_name") or "").strip()
    if not supplier:
        return

    vendor_rules = _load(VENDOR_RULES_FILE) or {"vendors": {}}
    vendor_payload = {
        "brand": extracted.get("brand") or extracted.get("business_unit"),
        "allocation_type": extracted.get("allocation_type"),
        "document_type": (extracted.get("accounting_decision") or {}).get("document_type") or extracted.get("decision_document_type"),
        "posting_action": extracted.get("posting_action") or extracted.get("posting_type"),
        "posting_type": extracted.get("posting_action") or extracted.get("posting_type"),
        "qbo_object": extracted.get("qbo_object") or extracted.get("qbo_posting_type"),
        "qbo_posting_type": extracted.get("qbo_posting_type"),
        "vat_treatment": extracted.get("vat_treatment"),
        "payment_status": extracted.get("payment_status"),
        "payment_method": extracted.get("payment_method"),
        "capitalisation_required": extracted.get("capitalisation_required"),
        "supplier_classification": extracted.get("document_category"),
    }
    vendor_rules.setdefault("vendors", {})[supplier] = vendor_payload
    _save(VENDOR_RULES_FILE, vendor_rules)
    record_vendor_history(supplier, vendor_payload)

    if extracted.get("branch"):
        branch_rules = _load(VENDOR_BRANCH_RULES_FILE) or {"vendors": {}}
        branch_rules.setdefault("vendors", {})[supplier] = {
            "brand": extracted.get("brand") or extracted.get("business_unit"),
            "branch": extracted.get("branch"),
            "allocation_type": extracted.get("allocation_type"),
        }
        _save(VENDOR_BRANCH_RULES_FILE, branch_rules)

    lines = extracted.get("line_items") or []
    accounts = [line.get("account_name") for line in lines if line.get("account_name")]
    if accounts:
        account_rules = _load(ACCOUNT_RULES_FILE) or {"vendors": {}, "keywords": {}}
        account_rules.setdefault("vendors", {})[supplier] = {
            "primary_account": accounts[0],
            "accounts": sorted(set(accounts)),
        }
        account_rules.setdefault("keywords", {})
        _save(ACCOUNT_RULES_FILE, account_rules)
