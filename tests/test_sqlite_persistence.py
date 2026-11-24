import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from accounting.persistence import (
    record_audit_event,
    record_duplicate_fingerprint,
    record_metrics,
    record_review_decision,
    record_transaction,
    record_vendor_history,
)


class SQLitePersistenceTests(unittest.TestCase):
    def test_optional_sqlite_sidecar_records_core_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "accounting.sqlite3"
            env = {
                "ACCOUNTING_PERSISTENCE_BACKEND": "sqlite",
                "ACCOUNTING_SQLITE_PATH": str(db_path),
            }
            with patch.dict("os.environ", env):
                record_transaction("invoice.json", {"supplier_name": "Vendor A", "invoice_number": "INV-1", "total_amount": 105}, "extracted")
                record_review_decision({"decision_id": "review-1", "extracted_transaction_id": "invoice.json", "decision": "approved", "reviewer_name": "Reviewer"})
                record_audit_event({"event_id": "audit-1", "event_type": "qbo_posting_attempt"})
                record_metrics("review_run", {"reviewed": 1})
                record_duplicate_fingerprint("fp-1", {"file": "invoice.json", "qbo_id": "123", "qbo_posting_type": "Bill"})
                record_vendor_history("Vendor A", {"qbo_posting_type": "Bill"})
            conn = sqlite3.connect(db_path)
            counts = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "transactions",
                    "review_decisions",
                    "audit_events",
                    "metrics",
                    "duplicate_fingerprints",
                    "vendor_history",
                )
            }
            conn.close()
        self.assertEqual(counts["transactions"], 1)
        self.assertEqual(counts["review_decisions"], 1)
        self.assertEqual(counts["audit_events"], 1)
        self.assertEqual(counts["metrics"], 1)
        self.assertEqual(counts["duplicate_fingerprints"], 1)
        self.assertEqual(counts["vendor_history"], 1)


if __name__ == "__main__":
    unittest.main()
