"""
Loads CE Suppliers from QBO export (XLS) into a lookup dict.
Key: supplier name (lowercase, stripped)
Value: {name, company_name, email, currency, open_balance}
"""

import pandas as pd
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
REFERENCE_DIR = BASE_DIR / "reference_accounts"
REFERENCE_SUPPLIERS_FILE = REFERENCE_DIR / "Suppliers (1).xls"


def _clean_value(value, default=""):
    if pd.isna(value):
        return default
    return value


def load_suppliers(xls_path: str = "config/suppliers.xls") -> dict:
    if xls_path == "config/suppliers.xls" and REFERENCE_SUPPLIERS_FILE.exists():
        path = REFERENCE_SUPPLIERS_FILE
    else:
        path = Path(xls_path)
    if not path.is_absolute():
        path = BASE_DIR / path

    df = pd.read_excel(path)
    required_columns = {"Supplier", "Company name", "Email", "Currency", "Open Balance"}
    missing = required_columns - set(df.columns)
    if missing:
        raise ValueError(f"Supplier export is missing columns: {sorted(missing)}")

    suppliers = {}
    for _, row in df.iterrows():
        name = str(_clean_value(row["Supplier"])).strip()
        if not name:
            continue
        suppliers[name.lower()] = {
            "name": name,
            "company_name": _clean_value(row.get("Company name", "")),
            "email": _clean_value(row.get("Email", "")),
            "currency": _clean_value(row.get("Currency", "UAE Dirham"), "UAE Dirham"),
            "open_balance": _clean_value(row.get("Open Balance", 0.0), 0.0),
        }
    return suppliers


def find_supplier(vendor_name: str, suppliers: dict) -> dict | None:
    """Simple lowercase match — Phase 2 uses Claude for fuzzy matching."""
    key = (vendor_name or "").lower().strip()
    if not key:
        return None
    if key in suppliers:
        return suppliers[key]
    # partial match fallback
    for k, v in suppliers.items():
        if key in k or k in key:
            return v
    return None


if __name__ == "__main__":
    suppliers = load_suppliers()
    print(f"Loaded {len(suppliers)} suppliers")
    test = find_supplier("Example Supplies", suppliers)
    print(f"Test match for 'Example Supplies': {test}")
