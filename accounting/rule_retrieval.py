from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
VENDOR_RULES_FILE = BASE_DIR / "config/vendor_rules.json"
VENDOR_BRANCH_RULES_FILE = BASE_DIR / "config/vendor_branch_rules.json"
ACCOUNT_RULES_FILE = BASE_DIR / "config/account_rules.json"
SUPPLIER_ACCOUNT_MAP_FILE = BASE_DIR / "config/supplier_account_map.json"


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _normalise(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _lookup_vendor(mapping: dict, supplier_name: str | None) -> dict:
    supplier = (supplier_name or "").strip()
    if not supplier:
        return {}
    vendors = mapping.get("vendors") or mapping.get("suppliers") or {}
    if supplier in vendors:
        return vendors[supplier] or {}
    normalised = _normalise(supplier)
    for name, rule in vendors.items():
        if _normalise(name) == normalised:
            return rule or {}
    return {}


def _append_applied(enriched: dict, rule_type: str, source: str, fields: list[str]) -> None:
    if not fields:
        return
    applied = enriched.setdefault("deterministic_rules_applied", [])
    applied.append({
        "rule_type": rule_type,
        "source": source,
        "fields": sorted(set(fields)),
    })


def _set_if_present(enriched: dict, target: str, value: Any, changed: list[str]) -> None:
    if value in (None, "", []):
        return
    if enriched.get(target) in (None, "", [], "unknown", "Unknown"):
        enriched[target] = value
        changed.append(target)


def _apply_vendor_rules(enriched: dict) -> None:
    rule = _lookup_vendor(_load_json(VENDOR_RULES_FILE), enriched.get("supplier_name"))
    changed: list[str] = []
    for target in (
        "brand",
        "allocation_type",
        "posting_action",
        "posting_type",
        "qbo_object",
        "qbo_posting_type",
        "vat_treatment",
        "payment_status",
        "payment_method",
        "capitalisation_required",
        "document_category",
    ):
        source = "supplier_classification" if target == "document_category" else target
        _set_if_present(enriched, target, rule.get(source), changed)
    _set_if_present(enriched, "decision_document_type", rule.get("document_type"), changed)
    _append_applied(enriched, "vendor_rules", str(VENDOR_RULES_FILE), changed)


def _apply_branch_rules(enriched: dict) -> None:
    rule = _lookup_vendor(_load_json(VENDOR_BRANCH_RULES_FILE), enriched.get("supplier_name"))
    changed: list[str] = []
    for target in ("brand", "branch", "allocation_type"):
        _set_if_present(enriched, target, rule.get(target), changed)
    _append_applied(enriched, "branch_rules", str(VENDOR_BRANCH_RULES_FILE), changed)


def _apply_account_rules(enriched: dict) -> None:
    rules = _load_json(ACCOUNT_RULES_FILE)
    supplier_rule = _lookup_vendor(rules, enriched.get("supplier_name"))
    supplier_memory = _lookup_vendor(_load_json(SUPPLIER_ACCOUNT_MAP_FILE), enriched.get("supplier_name"))
    account = supplier_rule.get("primary_account") or supplier_memory.get("primary")
    changed: list[str] = []
    if account:
        lines = enriched.setdefault("line_items", [])
        if not lines:
            lines.append({
                "description": "Deterministic vendor account rule",
                "amount": enriched.get("total_amount"),
                "account_name": account,
            })
            changed.append("line_items")
        else:
            first = lines[0]
            if isinstance(first, dict) and first.get("account_name") in (None, "", "Unknown"):
                first["account_name"] = account
                changed.append("line_items[0].account_name")
    _set_if_present(enriched, "accounting_treatment", supplier_memory.get("primary_treatment"), changed)
    _set_if_present(enriched, "qbo_posting_type", supplier_memory.get("qbo_posting_type"), changed)
    _set_if_present(enriched, "payment_method", supplier_memory.get("payment_method"), changed)
    source = f"{ACCOUNT_RULES_FILE}; {SUPPLIER_ACCOUNT_MAP_FILE}"
    _append_applied(enriched, "account_rules", source, changed)


def apply_learning_rules(extracted: dict) -> dict:
    enriched = dict(extracted)
    enriched.setdefault("deterministic_rules_applied", [])
    _apply_vendor_rules(enriched)
    _apply_branch_rules(enriched)
    _apply_account_rules(enriched)
    return enriched
