from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parents[1]
PERSISTENCE_CONFIG_FILE = BASE_DIR / "config/persistence.json"
DEFAULT_DB_FILE = BASE_DIR / "data/processed/accounting.sqlite3"


def _load_config() -> dict:
    config = {
        "backend": os.environ.get("ACCOUNTING_PERSISTENCE_BACKEND", "json").strip().lower(),
        "sqlite_path": os.environ.get("ACCOUNTING_SQLITE_PATH", str(DEFAULT_DB_FILE)),
    }
    if PERSISTENCE_CONFIG_FILE.exists():
        try:
            file_config = json.loads(PERSISTENCE_CONFIG_FILE.read_text())
        except (OSError, json.JSONDecodeError):
            file_config = {}
        if isinstance(file_config, dict):
            config.update({k: v for k, v in file_config.items() if v not in (None, "")})
    config["backend"] = str(config.get("backend") or "json").strip().lower()
    config["sqlite_path"] = str(config.get("sqlite_path") or DEFAULT_DB_FILE)
    return config


def sqlite_enabled(config: dict | None = None) -> bool:
    backend = (config or _load_config()).get("backend", "json")
    return backend in {"sqlite", "both", "json+sqlite"}


def _connect(config: dict | None = None) -> sqlite3.Connection | None:
    config = config or _load_config()
    if not sqlite_enabled(config):
        return None
    db_path = Path(config.get("sqlite_path") or DEFAULT_DB_FILE)
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    initialise_sqlite(conn)
    return conn


def initialise_sqlite(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            transaction_key TEXT PRIMARY KEY,
            supplier_name TEXT,
            invoice_number TEXT,
            invoice_date TEXT,
            total_amount REAL,
            qbo_posting_type TEXT,
            status TEXT,
            payload_json TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS review_decisions (
            decision_id TEXT PRIMARY KEY,
            transaction_key TEXT,
            decision TEXT,
            reviewer_name TEXT,
            payload_json TEXT NOT NULL,
            reviewed_at TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS audit_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT,
            payload_json TEXT NOT NULL,
            logged_at TEXT
        );
        CREATE TABLE IF NOT EXISTS metrics (
            metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
            metric_type TEXT,
            payload_json TEXT NOT NULL,
            recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS duplicate_fingerprints (
            fingerprint TEXT PRIMARY KEY,
            transaction_key TEXT,
            qbo_id TEXT,
            qbo_posting_type TEXT,
            payload_json TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS vendor_history (
            supplier_name TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    conn.commit()


def record_transaction(transaction_key: str, payload: dict[str, Any], status: str | None = None) -> None:
    conn = _connect()
    if conn is None:
        return
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO transactions (
                    transaction_key, supplier_name, invoice_number, invoice_date,
                    total_amount, qbo_posting_type, status, payload_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(transaction_key) DO UPDATE SET
                    supplier_name=excluded.supplier_name,
                    invoice_number=excluded.invoice_number,
                    invoice_date=excluded.invoice_date,
                    total_amount=excluded.total_amount,
                    qbo_posting_type=excluded.qbo_posting_type,
                    status=excluded.status,
                    payload_json=excluded.payload_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    transaction_key,
                    payload.get("supplier_name"),
                    payload.get("invoice_number"),
                    payload.get("invoice_date"),
                    payload.get("total_amount"),
                    payload.get("qbo_posting_type"),
                    status,
                    json.dumps(payload, default=str),
                ),
            )
    finally:
        conn.close()


def record_review_decision(event: dict[str, Any]) -> None:
    conn = _connect()
    if conn is None:
        return
    try:
        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO review_decisions (
                    decision_id, transaction_key, decision, reviewer_name,
                    payload_json, reviewed_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    event.get("decision_id") or event.get("event_id"),
                    event.get("extracted_transaction_id"),
                    event.get("decision"),
                    event.get("reviewer_name"),
                    json.dumps(event, default=str),
                    event.get("reviewed_at"),
                ),
            )
    finally:
        conn.close()


def record_audit_event(event: dict[str, Any]) -> None:
    conn = _connect()
    if conn is None:
        return
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO audit_events (event_id, event_type, payload_json, logged_at) VALUES (?, ?, ?, ?)",
                (
                    event.get("event_id"),
                    event.get("event_type"),
                    json.dumps(event, default=str),
                    event.get("logged_at") or event.get("attempted_at") or event.get("reviewed_at"),
                ),
            )
    finally:
        conn.close()


def record_metrics(metric_type: str, payload: dict[str, Any]) -> None:
    conn = _connect()
    if conn is None:
        return
    try:
        with conn:
            conn.execute(
                "INSERT INTO metrics (metric_type, payload_json) VALUES (?, ?)",
                (metric_type, json.dumps(payload, default=str)),
            )
    finally:
        conn.close()


def record_duplicate_fingerprint(fingerprint: str, payload: dict[str, Any]) -> None:
    conn = _connect()
    if conn is None:
        return
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO duplicate_fingerprints (
                    fingerprint, transaction_key, qbo_id, qbo_posting_type, payload_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    transaction_key=excluded.transaction_key,
                    qbo_id=excluded.qbo_id,
                    qbo_posting_type=excluded.qbo_posting_type,
                    payload_json=excluded.payload_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    fingerprint,
                    payload.get("file") or payload.get("transaction_key"),
                    payload.get("qbo_id") or payload.get("bill_id"),
                    payload.get("qbo_posting_type"),
                    json.dumps(payload, default=str),
                ),
            )
    finally:
        conn.close()


def record_vendor_history(supplier_name: str | None, payload: dict[str, Any]) -> None:
    if not supplier_name:
        return
    conn = _connect()
    if conn is None:
        return
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO vendor_history (supplier_name, payload_json, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(supplier_name) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (supplier_name, json.dumps(payload, default=str)),
            )
    finally:
        conn.close()
