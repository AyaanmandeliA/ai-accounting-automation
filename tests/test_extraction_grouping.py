import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from extraction.ai_extractor import _save_result


class ExtractionGroupingTests(unittest.TestCase):
    def test_invoice_plus_remittance_creates_one_transaction_with_both_attachments(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extracted_dir = root / "extracted"
            invoice = root / "invoice.pdf"
            remittance = root / "remittance.pdf"
            invoice.write_text("invoice")
            remittance.write_text("remittance")
            result = {
                "supplier_name": "Supplier LLC",
                "business_unit": "Sample Brand A",
                "brand": "Sample Brand A",
                "branch": "Branch North",
                "allocation_type": "branch",
                "folder_path_hint": "/Sample Brand A/Branch North/Bills",
                "invoice_number": "INV-100",
                "invoice_date": "2026-06-24",
                "due_date": None,
                "currency": "AED",
                "total_amount": 105,
                "tax_amount": 0,
                "line_items": [{"description": "Salon supplies", "amount": 105, "account_name": "Repairs"}],
                "notes": "Invoice with remittance support.",
                "primary_source_file": invoice.name,
                "document_type": "invoice",
                "supplier_trn": None,
                "service_period_start": None,
                "service_period_end": None,
                "accounting_treatment": "supplier_bill_paid",
                "payment_method": "bank_transfer",
                "qbo_posting_type": "Bill",
                "payment_confirmed": True,
                "payment_date": "2026-06-24",
                "payment_amount": 105,
                "payment_bank_account": "Example Bank",
                "extraction_confidence": 1,
                "vendor_confidence": 1,
                "amount_confidence": 1,
                "vat_confidence": 1,
                "branch_confidence": 1,
                "account_confidence": 1,
            }
            with patch("extraction.ai_extractor.EXTRACTED_DIR", extracted_dir), \
                 patch("allocation.review_router.REVIEW_ROOT", root / "review"):
                out = _save_result(result, invoice, [invoice, remittance], index=0, total=1)
            saved = json.loads(out.read_text())
        self.assertEqual(out.name, "invoice.pdf.extracted.json")
        self.assertEqual(saved["source_file"], str(invoice.resolve()))
        self.assertEqual(saved["all_source_files"], [str(invoice.resolve()), str(remittance.resolve())])
        self.assertEqual(saved["primary_source_file"], invoice.name)


if __name__ == "__main__":
    unittest.main()
