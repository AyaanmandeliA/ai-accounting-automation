import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from accounting.audit_log import (
    append_qbo_posting_attempt,
    append_review_decision,
    resolve_reviewer_name,
)


class AuditLogTests(unittest.TestCase):
    def test_review_decision_appends_jsonl_with_changes(self):
        before = {
            "supplier_name": "Old",
            "total_amount": 100,
            "source_file": "/tmp/invoice.pdf",
            "extracted_file": "/tmp/invoice.extracted.json",
            "accounting_decision": {"posting_type": "Bill", "decision_explanation": "Reviewed."},
        }
        after = {
            "supplier_name": "New",
            "total_amount": 100,
            "source_file": "/tmp/invoice.pdf",
            "extracted_file": "/tmp/invoice.extracted.json",
            "accounting_decision": {"posting_type": "Bill", "decision_explanation": "Reviewed."},
        }
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "audit_log.jsonl"
            with patch("accounting.audit_log.AUDIT_LOG_FILE", audit_path):
                event = append_review_decision(
                    reviewer_name="Reviewer",
                    raw_decision="approved",
                    before=before,
                    after=after,
                    filename="invoice.extracted.json",
                )
            line = json.loads(audit_path.read_text().strip())
        self.assertEqual(event["decision"], "edited")
        self.assertEqual(line["reviewer_name"], "Reviewer")
        self.assertEqual(line["fields_changed"], ["supplier_name"])
        self.assertIn("_review_decision_id", after)

    def test_reviewer_identity_prefers_explicit_then_env(self):
        self.assertEqual(resolve_reviewer_name("Reviewer")[0], "Reviewer")
        with patch.dict("os.environ", {"REVIEWER_NAME": "Env Reviewer"}):
            self.assertEqual(resolve_reviewer_name()[0], "Env Reviewer")

    def test_qbo_posting_attempt_appends_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "audit_log.jsonl"
            with patch("accounting.audit_log.AUDIT_LOG_FILE", audit_path):
                append_qbo_posting_attempt(
                    qbo_action="preflight_block",
                    qbo_posting_type="Expense",
                    extracted={"supplier_name": "Vendor", "invoice_number": "INV-1"},
                    success=False,
                    error_message="Unsupported",
                    duplicate_fingerprint="abc",
                )
            line = json.loads(audit_path.read_text().strip())
        self.assertEqual(line["event_type"], "qbo_posting_attempt")
        self.assertFalse(line["success"])
        self.assertEqual(line["duplicate_fingerprint"], "abc")


if __name__ == "__main__":
    unittest.main()
