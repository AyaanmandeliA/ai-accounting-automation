"""
Loads the CE Chart of Accounts CSV into a flat lookup dict.
Key: account name (lowercase, stripped)
Value: {name, account_type, detail_type}
"""

import csv
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
REFERENCE_DIR = BASE_DIR / "reference_accounts"
REFERENCE_COA_FILE = REFERENCE_DIR / "company_chart_of_accounts.csv"

DEFAULT_SUPPLIER_BILL_ACCOUNT_TYPES = {
    "expense",
    "expenses",
    "cost of goods sold",
    "costofgoodssold",
    "other expense",
    "otherexpense",
}

DEFAULT_EXCLUDED_ACCOUNT_TYPES = {
    "asset",
    "accounts receivable",
    "accountsreceivable",
    "other current asset",
    "othercurrentasset",
    "fixed asset",
    "fixedasset",
    "liability",
    "accounts payable",
    "accountspayable",
    "other current liability",
    "othercurrentliability",
    "long term liability",
    "longtermliability",
    "income",
    "other income",
    "otherincome",
    "equity",
    "bank",
    "credit card",
    "creditcard",
}

DEFAULT_EXCLUDED_NAME_TERMS = {
    "accounts receivable",
    "accounts payable",
    "a/r",
    "a/p",
    "uncategorized asset",
    "uncategorised asset",
    "uncategorized income",
    "uncategorised income",
    "vat control",
    "tax control",
    "input vat",
    "output vat",
    "vat payable",
    "tax payable",
}


def load_coa(csv_path: str = "config/chart_of_accounts.csv") -> dict:
    if csv_path == "config/chart_of_accounts.csv" and REFERENCE_COA_FILE.exists():
        path = REFERENCE_COA_FILE
    else:
        path = Path(csv_path)
    if not path.is_absolute():
        path = BASE_DIR / path

    accounts = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        required_columns = {"Account name", "Account type", "Detail type"}
        missing = required_columns - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"Chart of accounts is missing columns: {sorted(missing)}")
        for row in reader:
            name = row["Account name"].strip()
            if not name:
                continue
            accounts[name.lower()] = {
                "name": name,
                "account_type": row["Account type"].strip(),
                "detail_type": row["Detail type"].strip(),
            }
    return accounts


def _normalise(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def _compact(value: Any) -> str:
    return _normalise(value).replace(" ", "")


def account_type(account: Any) -> str:
    if isinstance(account, dict):
        return _normalise(account.get("account_type") or account.get("AccountType"))
    return _normalise(getattr(account, "AccountType", None) or getattr(account, "account_type", None))


def account_name(account: Any) -> str:
    if isinstance(account, dict):
        return _normalise(account.get("name") or account.get("Name") or account.get("FullyQualifiedName"))
    return _normalise(getattr(account, "FullyQualifiedName", None) or getattr(account, "Name", None))


def account_detail_type(account: Any) -> str:
    if isinstance(account, dict):
        return _normalise(account.get("detail_type") or account.get("DetailType"))
    return _normalise(getattr(account, "DetailType", None) or getattr(account, "detail_type", None))


def is_tax_control_account(account: Any) -> bool:
    text = " ".join([account_name(account), account_type(account), account_detail_type(account)])
    return any(term in text for term in DEFAULT_EXCLUDED_NAME_TERMS if "tax" in term or "vat" in term)


def is_default_supplier_bill_account(account: Any) -> bool:
    acct_type = account_type(account)
    compact_type = _compact(acct_type)
    if acct_type not in DEFAULT_SUPPLIER_BILL_ACCOUNT_TYPES and compact_type not in DEFAULT_SUPPLIER_BILL_ACCOUNT_TYPES:
        return False
    text = " ".join([account_name(account), acct_type, account_detail_type(account)])
    if any(term in text for term in DEFAULT_EXCLUDED_NAME_TERMS):
        return False
    return True


def is_excluded_default_account(account: Any) -> bool:
    acct_type = account_type(account)
    compact_type = _compact(acct_type)
    text = " ".join([account_name(account), acct_type, account_detail_type(account)])
    if acct_type in DEFAULT_EXCLUDED_ACCOUNT_TYPES or compact_type in DEFAULT_EXCLUDED_ACCOUNT_TYPES:
        return True
    return any(term in text for term in DEFAULT_EXCLUDED_NAME_TERMS)


def filter_supplier_bill_candidate_accounts(coa: dict) -> dict:
    return {
        key: account
        for key, account in coa.items()
        if is_default_supplier_bill_account(account)
    }


def validate_account_for_transaction(account: Any, extracted: dict, line_item: dict | None = None) -> str | None:
    posting_type = str(extracted.get("qbo_posting_type") or "").strip()
    category = str(extracted.get("document_category") or "").strip()
    treatment = str(extracted.get("accounting_treatment") or "").strip()
    line_item = line_item or {}
    if posting_type == "Bill" or treatment.startswith("supplier_bill") or category in {"Supplier Bill", "Head Office Cost"}:
        if not is_default_supplier_bill_account(account):
            name = account_name(account) or line_item.get("account_name") or "unknown"
            acct_type = account_type(account) or "unknown"
            return f"Incompatible account for supplier bill: {name} ({acct_type})"
    return None


def find_best_match(description: str, coa: dict) -> dict | None:
    """Simple substring match — Phase 2 will use Claude for fuzzy matching."""
    desc = (description or "").lower().strip()
    if not desc:
        return None
    for key, account in coa.items():
        if key in desc or desc in key:
            return account
    return None


def load_supplier_account_map(json_path: str = "config/supplier_account_map.json") -> dict:
    """
    Returns {supplier_name: {primary: account_name, frequency: {account: count}}}
    Built from historical QBO expense export.
    """
    import json
    path = Path(json_path)
    if not path.is_absolute():
        path = BASE_DIR / path
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("suppliers", data)


if __name__ == "__main__":
    coa = load_coa()
    print(f"Loaded {len(coa)} accounts from Chart of Accounts")
    test = find_best_match("rent Branch North", coa)
    print(f"Test match for 'rent Branch North': {test}")
    sam = load_supplier_account_map()
    print(f"Loaded {len(sam)} supplier→account mappings")
