"""
Builds supplier/account memory from QBO exports.
Run after replacing config/reference/company_*. files.
"""

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_REFERENCE_DIR = BASE_DIR / "reference_accounts"
CONFIG_REFERENCE_DIR = BASE_DIR / "config/reference"
REFERENCE_DIR = WORKSPACE_REFERENCE_DIR if WORKSPACE_REFERENCE_DIR.exists() else CONFIG_REFERENCE_DIR
EXPENSES_FILE = REFERENCE_DIR / ("Expenses.xls" if WORKSPACE_REFERENCE_DIR.exists() else "company_expenses.xls")
COA_FILE = REFERENCE_DIR / ("company_chart_of_accounts.csv" if WORKSPACE_REFERENCE_DIR.exists() else "company_coa.csv")
SUPPLIERS_FILE = REFERENCE_DIR / ("Suppliers (1).xls" if WORKSPACE_REFERENCE_DIR.exists() else "company_suppliers.xls")
UNPAID_BILLS_FILE = REFERENCE_DIR / "Unpaid Bills.xls"
PROFIT_AND_LOSS_FILE = REFERENCE_DIR / "Company_Profit and Loss.xlsx"
BALANCE_SHEET_FILE = REFERENCE_DIR / "Company_Balance Sheet.xlsx"
GENERAL_LEDGER_FILE = REFERENCE_DIR / "Company_General Ledger.xlsx"
JOURNAL_FILE = REFERENCE_DIR / "Company_Journal.xlsx"
TRANSACTION_LIST_FILE = REFERENCE_DIR / "Company_Transaction List by Date.xlsx"
SUPPLIER_ACCOUNT_MAP_FILE = BASE_DIR / "config/supplier_account_map.json"
ACCOUNTING_MEMORY_FILE = BASE_DIR / "config/accounting_memory.json"

IGNORED_CATEGORIES = {"", "--Split--"}
TYPE_TO_TREATMENT = {
    "Bill": ("supplier_bill_unpaid", "Bill", "unpaid"),
    "Expense": ("direct_expense_paid", "Expense", "unknown"),
    "Supplier Credit": ("supplier_credit", "SupplierCredit", "supplier_credit"),
    "Bill Payment (Cheque)": ("supplier_payment_only", "BillPayment", "bank_transfer"),
}


def _clean(value) -> str:
    return str(value or "").strip()


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _load_coa_lookup() -> tuple[set[str], dict[str, str]]:
    df = pd.read_csv(COA_FILE, dtype=str, keep_default_na=False)
    names = {_clean(v) for v in df["Account name"] if _clean(v)}
    leaf_lookup = {}
    for name in names:
        leaf = _clean(name.split(":")[-1])
        leaf_lookup.setdefault(_normalise(leaf), name)
        leaf_lookup.setdefault(_normalise(name), name)
    return names, leaf_lookup


def _resolve_account(category: str, coa_names: set[str], leaf_lookup: dict[str, str]) -> str | None:
    category = _clean(category)
    if category in IGNORED_CATEGORIES:
        return None
    if category in coa_names:
        return category
    return leaf_lookup.get(_normalise(category))


def _confidence(account_total: int, account_count: int, treatment_total: int, treatment_count: int) -> str:
    if account_total <= 0:
        account_share = 1.0
    else:
        account_share = account_count / account_total
    if treatment_total <= 0:
        return "low"
    treatment_share = treatment_count / treatment_total
    total = max(account_total, treatment_total)
    if total >= 5 and account_share >= 0.65 and treatment_share >= 0.65:
        return "high"
    if total >= 2 and account_share >= 0.5 and treatment_share >= 0.5:
        return "medium"
    return "low"


def _supplier_names() -> list[str]:
    if not SUPPLIERS_FILE.exists():
        return []
    df = pd.read_excel(SUPPLIERS_FILE, dtype=str, keep_default_na=False)
    return sorted({_clean(v) for v in df.get("Supplier", []) if _clean(v)})


def _num(value) -> float:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def _date_key(value: str):
    text = _clean(value)
    if not text:
        return None
    for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _report_account_labels(path: Path) -> list[str]:
    if not path.exists():
        return []
    df = pd.read_excel(path, dtype=str, keep_default_na=False, header=None)
    labels = []
    for value in df.iloc[:, 0].tolist():
        text = _clean(value)
        if not text:
            continue
        lower = text.lower()
        if lower in {"balance sheet", "profit and loss", "total"}:
            continue
        if lower.startswith(("as of ", "january ")):
            continue
        labels.append(text)
    return labels


def _load_unpaid_bills() -> dict[str, dict]:
    if not UNPAID_BILLS_FILE.exists():
        return {}
    df = pd.read_excel(UNPAID_BILLS_FILE, dtype=str, keep_default_na=False)
    required = {"Supplier", "Due Date", "amount", "balance", "Status"}
    if not required.issubset(set(df.columns)):
        return {}
    grouped = {}
    for supplier, rows in df.groupby("Supplier", dropna=False):
        supplier = _clean(supplier)
        if not supplier:
            continue
        balances = [_num(v) for v in rows["balance"]]
        amounts = [_num(v) for v in rows["amount"]]
        statuses = Counter(_clean(v) for v in rows["Status"] if _clean(v))
        due_dates = [
            (_date_key(v), _clean(v))
            for v in rows["Due Date"]
            if _clean(v)
        ]
        due_dates = sorted((d, raw) for d, raw in due_dates if d)
        grouped[supplier] = {
            "open_bill_count": int(len(rows)),
            "open_amount_total": round(sum(amounts), 2),
            "open_balance_total": round(sum(balances), 2),
            "status_frequency": dict(statuses.most_common()),
            "earliest_due_date": due_dates[0][1] if due_dates else None,
            "latest_due_date": due_dates[-1][1] if due_dates else None,
        }
    return grouped


def _load_report_table(path: Path, required_columns: set[str]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    raw = pd.read_excel(path, dtype=str, keep_default_na=False, header=None)
    header_index = None
    for idx, row in raw.iterrows():
        values = {_clean(v) for v in row.tolist() if _clean(v)}
        if required_columns.issubset(values):
            header_index = idx
            break
    if header_index is None:
        return pd.DataFrame()
    headers = [_clean(v) or f"column_{i}" for i, v in enumerate(raw.iloc[header_index].tolist())]
    df = raw.iloc[header_index + 1:].copy()
    df.columns = headers
    df = df.dropna(how="all")
    return df


def _reference_export_history() -> dict:
    transaction_list = _load_report_table(
        TRANSACTION_LIST_FILE,
        {"Date", "Transaction type", "Account name", "Amount"},
    )
    general_ledger = _load_report_table(
        GENERAL_LEDGER_FILE,
        {"Distribution account", "Transaction date", "Transaction type", "Name", "Amount"},
    )
    journal = _load_report_table(
        JOURNAL_FILE,
        {"Transaction date", "Transaction type", "Name", "Full name", "Debit", "Credit"},
    )

    account_usage = Counter()
    transaction_types = Counter()
    journal_accounts = Counter()
    journal_types = Counter()

    if not transaction_list.empty:
        transaction_types.update(_clean(v) for v in transaction_list.get("Transaction type", []) if _clean(v))
        account_usage.update(_clean(v) for v in transaction_list.get("Account name", []) if _clean(v))

    if not general_ledger.empty:
        transaction_types.update(_clean(v) for v in general_ledger.get("Transaction type", []) if _clean(v))
        account_usage.update(_clean(v) for v in general_ledger.get("Distribution account", []) if _clean(v))

    if not journal.empty:
        journal_types.update(_clean(v) for v in journal.get("Transaction type", []) if _clean(v))
        journal_accounts.update(_clean(v) for v in journal.get("Full name", []) if _clean(v))

    return {
        "files_used": {
            "transaction_list": str(TRANSACTION_LIST_FILE) if not transaction_list.empty else None,
            "general_ledger": str(GENERAL_LEDGER_FILE) if not general_ledger.empty else None,
            "journal": str(JOURNAL_FILE) if not journal.empty else None,
        },
        "row_counts": {
            "transaction_list": int(len(transaction_list)),
            "general_ledger": int(len(general_ledger)),
            "journal": int(len(journal)),
        },
        "transaction_type_history": dict(transaction_types.most_common()),
        "account_usage_history": dict(account_usage.most_common(250)),
        "journal_history": {
            "transaction_types": dict(journal_types.most_common()),
            "accounts": dict(journal_accounts.most_common(250)),
        },
    }


def build() -> dict:
    coa_names, leaf_lookup = _load_coa_lookup()
    expenses = pd.read_excel(EXPENSES_FILE, dtype=str, keep_default_na=False)
    unpaid_bills = _load_unpaid_bills()
    reference_history = _reference_export_history()

    supplier_accounts = defaultdict(Counter)
    supplier_treatments = defaultdict(Counter)
    supplier_qbo_types = defaultdict(Counter)
    supplier_payment_methods = defaultdict(Counter)
    supplier_types = defaultdict(Counter)
    category_accounts = defaultdict(Counter)
    skipped = Counter()
    usable_rows = 0

    for _, row in expenses.iterrows():
        payee = _clean(row.get("Payee"))
        tx_type = _clean(row.get("Type"))
        category = _clean(row.get("Category"))
        if not payee:
            skipped["missing_payee"] += 1
            continue
        mapping = TYPE_TO_TREATMENT.get(tx_type)
        if not mapping:
            skipped["unsupported_type"] += 1
            continue
        treatment, qbo_type, payment_method = mapping
        supplier_treatments[payee][treatment] += 1
        supplier_qbo_types[payee][qbo_type] += 1
        supplier_payment_methods[payee][payment_method] += 1
        supplier_types[payee][tx_type] += 1
        account = _resolve_account(category, coa_names, leaf_lookup)
        if not account:
            skipped["missing_or_split_category"] += 1
            continue
        supplier_accounts[payee][account] += 1
        category_accounts[category][account] += 1
        usable_rows += 1

    suppliers = {}
    for payee in sorted(set(supplier_accounts) | set(supplier_treatments)):
        accounts = supplier_accounts.get(payee, Counter())
        treatments = supplier_treatments[payee]
        qbo_types = supplier_qbo_types[payee]
        payment_methods = supplier_payment_methods[payee]
        total_account_rows = sum(accounts.values())
        primary_account, primary_account_count = accounts.most_common(1)[0] if accounts else (None, 0)
        primary_treatment, primary_treatment_count = treatments.most_common(1)[0]
        primary_qbo_type = qbo_types.most_common(1)[0][0]
        primary_payment_method = payment_methods.most_common(1)[0][0]
        suppliers[payee] = {
            "primary": primary_account,
            "frequency": dict(accounts.most_common()),
            "primary_treatment": primary_treatment,
            "treatment_frequency": dict(treatments.most_common()),
            "qbo_posting_type": primary_qbo_type,
            "qbo_posting_frequency": dict(qbo_types.most_common()),
            "payment_method": primary_payment_method,
            "payment_method_frequency": dict(payment_methods.most_common()),
            "transaction_type_frequency": dict(supplier_types[payee].most_common()),
            "confidence": _confidence(
                total_account_rows,
                primary_account_count,
                sum(treatments.values()),
                primary_treatment_count,
            ),
            "source_rows": int(max(total_account_rows, sum(treatments.values()))),
        }
        if payee in unpaid_bills:
            suppliers[payee]["open_bills"] = unpaid_bills[payee]

    for payee, data in unpaid_bills.items():
        suppliers.setdefault(
            payee,
            {
                "primary": None,
                "frequency": {},
                "primary_treatment": "supplier_bill_unpaid",
                "treatment_frequency": {},
                "qbo_posting_type": "Bill",
                "qbo_posting_frequency": {},
                "payment_method": "unpaid",
                "payment_method_frequency": {},
                "transaction_type_frequency": {},
                "confidence": "low",
                "source_rows": 0,
                "open_bills": data,
            },
        )

    memory = {
        "_metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "expenses_file": str(EXPENSES_FILE),
            "coa_file": str(COA_FILE),
            "suppliers_file": str(SUPPLIERS_FILE),
            "unpaid_bills_file": str(UNPAID_BILLS_FILE) if UNPAID_BILLS_FILE.exists() else None,
            "profit_and_loss_file": str(PROFIT_AND_LOSS_FILE) if PROFIT_AND_LOSS_FILE.exists() else None,
            "balance_sheet_file": str(BALANCE_SHEET_FILE) if BALANCE_SHEET_FILE.exists() else None,
            "general_ledger_file": reference_history["files_used"].get("general_ledger"),
            "journal_file": reference_history["files_used"].get("journal"),
            "transaction_list_file": reference_history["files_used"].get("transaction_list"),
            "expense_rows": int(len(expenses)),
            "usable_account_rows": int(usable_rows),
            "supplier_export_count": len(_supplier_names()),
            "supplier_memory_count": len(suppliers),
            "unpaid_supplier_count": len(unpaid_bills),
            "unpaid_balance_total": round(
                sum(v["open_balance_total"] for v in unpaid_bills.values()), 2
            ),
            "profit_and_loss_account_labels": len(_report_account_labels(PROFIT_AND_LOSS_FILE)),
            "balance_sheet_account_labels": len(_report_account_labels(BALANCE_SHEET_FILE)),
            "reference_history_rows": reference_history["row_counts"],
            "skipped": dict(skipped),
        },
        "suppliers": suppliers,
        "category_account_map": {
            category: dict(counter.most_common())
            for category, counter in sorted(category_accounts.items())
        },
        "transaction_type_rules": {
            key: {
                "accounting_treatment": value[0],
                "qbo_posting_type": value[1],
                "payment_method": value[2],
            }
            for key, value in TYPE_TO_TREATMENT.items()
        },
        "reference_history": reference_history,
    }
    return memory


def main() -> None:
    memory = build()
    tmp = ACCOUNTING_MEMORY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(memory, indent=2, ensure_ascii=False))
    tmp.replace(ACCOUNTING_MEMORY_FILE)

    lean = {
        "_metadata": memory["_metadata"],
        "suppliers": memory["suppliers"],
    }
    tmp = SUPPLIER_ACCOUNT_MAP_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(lean, indent=2, ensure_ascii=False))
    tmp.replace(SUPPLIER_ACCOUNT_MAP_FILE)

    print(f"Built {len(memory['suppliers'])} supplier memory entries")
    print(f"Usable historical account rows: {memory['_metadata']['usable_account_rows']}")
    print(f"Wrote {SUPPLIER_ACCOUNT_MAP_FILE}")
    print(f"Wrote {ACCOUNTING_MEMORY_FILE}")


if __name__ == "__main__":
    main()
