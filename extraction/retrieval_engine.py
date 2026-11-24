from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

SUPPLIER_LIMIT = 3
ACCOUNT_LIMIT = 5
HISTORY_LIMIT = 3
MIN_SUPPLIER_SCORE = 0.45
MIN_HISTORY_SCORE = 0.45
MIN_ACCOUNT_SCORE = 0.45

STOPWORDS = {
    "llc",
    "l.l.c",
    "co",
    "company",
    "trading",
    "general",
    "fze",
    "fzco",
    "dmcc",
    "uae",
    "dubai",
    "invoice",
    "receipt",
    "payment",
    "slip",
    "tax",
}

ACCOUNT_STOPWORDS = {
    "account",
    "accounts",
    "and",
    "cost",
    "costs",
    "expense",
    "expenses",
    "general",
    "other",
    "purchase",
    "purchases",
    "service",
    "services",
    "supplies",
}


def normalise_text(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def supplier_tokens(value: Any) -> list[str]:
    return [
        token
        for token in normalise_text(value).split()
        if len(token) >= 3 and token not in STOPWORDS
    ]


def account_tokens(value: Any) -> list[str]:
    return [
        token
        for token in normalise_text(value).split()
        if len(token) >= 3 and token not in ACCOUNT_STOPWORDS
    ]


def _supplier_name(key: str, supplier: Any) -> str:
    if isinstance(supplier, dict):
        return str(supplier.get("name") or supplier.get("company_name") or key)
    return str(key)


def _supplier_score(query: str, name: str) -> float:
    query_norm = normalise_text(query)
    name_norm = normalise_text(name)
    if not query_norm or not name_norm:
        return 0.0
    if query_norm == name_norm:
        return 1.0
    if f" {name_norm} " in f" {query_norm} " or f" {query_norm} " in f" {name_norm} ":
        return 0.95
    query_tokens = set(supplier_tokens(query_norm))
    name_tokens = set(supplier_tokens(name_norm))
    overlap = len(query_tokens & name_tokens)
    token_score = overlap / max(1, len(name_tokens))
    fuzzy_score = SequenceMatcher(None, query_norm, name_norm).ratio()
    if token_score == 0 and fuzzy_score < 0.75:
        return 0.0
    return max(token_score, fuzzy_score)


def _candidate_queries(
    *,
    supplier_hint: str | None = None,
    supplier_confidence: float | None = None,
    text: str = "",
) -> list[tuple[str, float]]:
    queries: list[tuple[str, float]] = []
    if supplier_hint:
        confidence = 1.0 if supplier_confidence is None else max(0.0, min(1.0, float(supplier_confidence)))
        queries.append((supplier_hint, confidence))
    if not supplier_hint or (supplier_confidence or 0.0) <= 0.90:
        queries.append((text, 0.75))
    return queries


def retrieve_suppliers(
    suppliers: dict,
    *,
    supplier_hint: str | None = None,
    supplier_confidence: float | None = None,
    text: str = "",
    limit: int = SUPPLIER_LIMIT,
    min_score: float = MIN_SUPPLIER_SCORE,
) -> dict:
    if not suppliers:
        return {}
    scored = []
    for key, supplier in suppliers.items():
        name = _supplier_name(str(key), supplier)
        score = 0.0
        for query, weight in _candidate_queries(
            supplier_hint=supplier_hint,
            supplier_confidence=supplier_confidence,
            text=text,
        ):
            score = max(score, _supplier_score(query, name) * weight)
        if score >= min_score:
            scored.append((score, str(key), supplier))
    scored.sort(key=lambda item: (-item[0], _supplier_name(item[1], item[2]).lower()))
    return {key: supplier for _, key, supplier in scored[:limit]}


def _history_score(name: str, data: dict, suppliers: dict, text: str) -> float:
    supplier_names = [_supplier_name(key, supplier) for key, supplier in suppliers.items()]
    score = max((_supplier_score(name, supplier) for supplier in supplier_names), default=0.0)
    score = max(score, _supplier_score(text, name) * 0.75)
    confidence = str((data or {}).get("confidence") or "").lower()
    if confidence == "high":
        score += 0.05
    return score


def retrieve_supplier_history(
    supplier_account_map: dict,
    suppliers: dict,
    *,
    text: str = "",
    limit: int = HISTORY_LIMIT,
    min_score: float = MIN_HISTORY_SCORE,
) -> dict:
    if not supplier_account_map or not suppliers:
        return {}
    scored = []
    for name, data in supplier_account_map.items():
        if str(name).startswith("_"):
            continue
        score = _history_score(str(name), data if isinstance(data, dict) else {}, suppliers, text)
        if score >= min_score:
            scored.append((score, str(name), data))
    scored.sort(key=lambda item: (-item[0], item[1].lower()))
    return {name: data for _, name, data in scored[:limit]}


def _account_name(key: str, account: Any) -> str:
    if isinstance(account, dict):
        return str(account.get("name") or account.get("fully_qualified_name") or key)
    return str(key)


def _history_accounts(history: dict) -> list[str]:
    accounts = []
    for data in history.values():
        if not isinstance(data, dict):
            continue
        primary = data.get("primary") or data.get("primary_account")
        if primary:
            accounts.append(str(primary))
        frequency = data.get("frequency") or data.get("account_frequency") or {}
        if isinstance(frequency, dict):
            accounts.extend(str(name) for name in frequency)
    return accounts


def _account_score(account_name: str, history_accounts: list[str], document_type: str = "") -> float:
    score = 0.0
    for historical in history_accounts:
        account_norm = normalise_text(account_name)
        historical_norm = normalise_text(historical)
        if account_norm in historical_norm or historical_norm in account_norm:
            score = max(score, 0.95)
            continue
        account_terms = set(account_tokens(account_name))
        historical_terms = set(account_tokens(historical))
        token_overlap = len(account_terms & historical_terms) / max(1, len(account_terms))
        fuzzy_score = SequenceMatcher(None, account_norm, historical_norm).ratio()
        if token_overlap:
            score = max(score, token_overlap, fuzzy_score)
        elif fuzzy_score >= 0.75:
            score = max(score, fuzzy_score)
    doc = normalise_text(document_type)
    account = normalise_text(account_name)
    if doc and any(token in account for token in doc.split()):
        score = max(score, 0.5)
    return score


def retrieve_accounts(
    coa: dict,
    supplier_history: dict,
    *,
    document_type: str = "",
    limit: int = ACCOUNT_LIMIT,
    min_score: float = MIN_ACCOUNT_SCORE,
) -> dict:
    if not coa:
        return {}
    history_accounts = _history_accounts(supplier_history)
    scored = []
    for key, account in coa.items():
        name = _account_name(str(key), account)
        score = _account_score(name, history_accounts, document_type=document_type)
        if score >= min_score:
            scored.append((score, str(key), account))
    scored.sort(key=lambda item: (-item[0], _account_name(item[1], item[2]).lower()))
    selected = {key: account for _, key, account in scored[:limit]}
    if selected:
        return selected
    return {}


def retrieve_context(
    *,
    coa: dict,
    suppliers: dict,
    supplier_account_map: dict,
    text: str = "",
    supplier_hint: str | None = None,
    supplier_confidence: float | None = None,
    document_type: str = "",
    supplier_limit: int = SUPPLIER_LIMIT,
    account_limit: int = ACCOUNT_LIMIT,
    history_limit: int = HISTORY_LIMIT,
) -> dict:
    matched_suppliers = retrieve_suppliers(
        suppliers,
        supplier_hint=supplier_hint,
        supplier_confidence=supplier_confidence,
        text=text,
        limit=supplier_limit,
    )
    matched_history = retrieve_supplier_history(
        supplier_account_map,
        matched_suppliers,
        text=text,
        limit=history_limit,
    )
    matched_accounts = retrieve_accounts(
        coa,
        matched_history,
        document_type=document_type,
        limit=account_limit,
    )
    return {
        "suppliers": matched_suppliers,
        "supplier_history": matched_history,
        "accounts": matched_accounts,
    }
