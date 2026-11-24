import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from review import _edit_accounting_corrections, _print_transaction, _save_google_feedback


class ReviewUiTests(unittest.TestCase):
    def _item(self, **overrides):
        item = {
            "business_unit": "Sample Brand A",
            "supplier_name": "Vendor",
            "invoice_number": "INV-1",
            "invoice_date": "2026-06-23",
            "due_date": "2026-07-23",
            "total_amount": 100,
            "tax_amount": 5,
            "currency": "AED",
            "brand": "Sample Brand A",
            "branch": "Branch North",
            "allocation_type": "branch",
            "folder_path_hint": "Sample Brand A/Branch North",
            "document_type": "invoice",
            "document_category": "Supplier Bill",
            "accounting_treatment": "supplier_bill_unpaid",
            "posting_type": "Bill",
            "payment_status": "Unpaid",
            "payment_method": "unpaid",
            "qbo_posting_type": "Bill",
            "vat_treatment": "Claimable VAT",
            "overall_confidence": 1,
            "qbo_class_name": "Brand A - Branch North",
            "qbo_location_name": "Branch North",
            "line_items": [{"description": "Supplies", "amount": 100, "account_name": "Repairs"}],
            "source_file": __file__,
            "all_source_files": [__file__],
            "provider_name": "google",
        }
        item.update(overrides)
        return item

    def test_line_item_prints_once(self):
        item = self._item()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _print_transaction(item, "invoice.extracted.json", 1, 1)
        output = buffer.getvalue()
        self.assertEqual(output.count("  1."), 1)
        self.assertEqual(output.count("Supplies"), 1)

    def test_accounting_treatment_panel_is_shown(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _print_transaction(self._item(), "invoice.extracted.json", 1, 1)
        output = buffer.getvalue()
        self.assertIn("PROPOSED ACCOUNTING", output)
        self.assertIn("posting treatment: Create Bill", output)
        self.assertIn("payment status: Unpaid", output)
        self.assertIn("payment method: Unknown", output)
        self.assertIn("QBO posting type: Bill", output)
        self.assertIn("supplier/vendor: Vendor", output)
        self.assertIn("expense account: Repairs", output)
        self.assertIn("VAT treatment: Claimable VAT", output)
        self.assertIn("class/location: Brand A - Branch North / Branch North", output)
        self.assertIn("due date: 2026-07-23", output)
        self.assertIn("reasoning:", output)

    def test_accounting_correction_prompt_updates_fields(self):
        item = self._item()
        answers = iter([
            "Create Expense",
            "Paid",
            "Bank Transfer",
            "Expense",
            "Corrected Vendor",
            "Salon Consumables",
            "Non-claimable VAT",
            "Branch South",
            "Brand A - Branch South",
            "Branch South",
            "Corrected accounting treatment",
        ])
        with patch("review._read", side_effect=lambda prompt: next(answers)), redirect_stdout(io.StringIO()):
            _edit_accounting_corrections(item)
        self.assertTrue(item["human_override_decision"])
        self.assertEqual(item["posting_type"], "Create Expense")
        self.assertEqual(item["posting_action"], "Create Expense")
        self.assertEqual(item["payment_status"], "Paid")
        self.assertEqual(item["payment_method"], "bank_transfer")
        self.assertEqual(item["qbo_posting_type"], "Expense")
        self.assertEqual(item["supplier_name"], "Corrected Vendor")
        self.assertEqual(item["line_items"][0]["account_name"], "Salon Consumables")
        self.assertEqual(item["vat_treatment"], "Non-claimable VAT")
        self.assertEqual(item["branch"], "Branch South")
        self.assertEqual(item["qbo_class_name"], "Brand A - Branch South")
        self.assertEqual(item["qbo_location_name"], "Branch South")
        self.assertEqual(item["notes"], "Corrected accounting treatment")

    def test_google_feedback_records_extraction_and_accounting_mistakes(self):
        before = self._item()
        after = self._item(
            supplier_name="Corrected Vendor",
            posting_type="Expense",
            payment_status="Paid",
            qbo_posting_type="Expense",
            vat_treatment="Non-claimable VAT",
            line_items=[{"description": "Supplies", "amount": 100, "account_name": "Salon Consumables"}],
        )
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "google_extraction_feedback.jsonl"
            md = Path(tmp) / "google_extraction_feedback.md"
            with patch("review.GOOGLE_FEEDBACK_JSONL", jsonl), patch("review.GOOGLE_FEEDBACK_MD", md):
                record = _save_google_feedback(before, after, filename="invoice.json", decision="approved", reviewer="Reviewer")
            saved = json.loads(jsonl.read_text().splitlines()[0])
            markdown = md.read_text()
        self.assertFalse(record["extraction_correct"])
        self.assertFalse(record["accounting_treatment_correct"])
        self.assertEqual(saved["extraction_mistakes"][0]["field"], "supplier_name")
        self.assertIn("posting treatment", [m["label"] for m in saved["accounting_mistakes"]])
        self.assertIn("expense account", [m["label"] for m in saved["accounting_mistakes"]])
        self.assertIn("Extraction accuracy", markdown)
        self.assertIn("Accounting treatment accuracy", markdown)
        self.assertIn("supplier/vendor: 1", markdown)
        self.assertIn("posting treatment: 1", markdown)


if __name__ == "__main__":
    unittest.main()
