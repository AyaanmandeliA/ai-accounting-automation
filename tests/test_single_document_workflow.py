import json
import tempfile
import unittest
from pathlib import Path

from workflows.single_document import build_single_document_preview


class SingleDocumentWorkflowTests(unittest.TestCase):
    def test_single_extracted_json_builds_dry_run_preview_without_ingestion(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "invoice.extracted.json"
            source.write_text(json.dumps({
                "supplier_name": "Vendor A",
                "invoice_number": "INV-1",
                "invoice_date": "2026-06-23",
                "total_amount": 105,
                "tax_amount": 5,
                "document_category": "Supplier Bill",
                "document_type": "invoice",
                "brand": "Sample Brand A",
                "business_unit": "Sample Brand A",
                "branch": "Branch North",
                "allocation_type": "branch",
                "qbo_class_name": "Brand A - Branch North",
                "qbo_location_name": "Branch North",
                "qbo_posting_type": "Bill",
                "payment_method": "unpaid",
                "overall_confidence": 0.97,
                "extraction_confidence": 1,
                "vendor_confidence": 1,
                "amount_confidence": 1,
                "vat_confidence": 1,
                "branch_confidence": 1,
                "account_confidence": 1,
                "notes": "Tax invoice TRN 100000000000003",
                "line_items": [{"description": "x", "amount": 105, "account_name": "Repairs", "account_type": "Expense"}],
            }))
            preview = build_single_document_preview(source)
        self.assertEqual(preview["workflow"], "single_document")
        self.assertEqual(preview["posting_preview"]["qbo_api_entity"], "Bill")
        self.assertIn("Dry-run Posting Preview", preview["stages"])


if __name__ == "__main__":
    unittest.main()
