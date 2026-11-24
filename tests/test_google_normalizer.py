import json
import tempfile
import unittest
from pathlib import Path

from document_processing.google_normalizer import (
    normalize_date,
    normalize_google_provider_result,
    normalize_invoice_number,
    normalize_money,
    normalize_trn,
)
from extraction.ai_extractor import _validate_google_provider_result


class GoogleNormalizerTests(unittest.TestCase):
    def test_normalizes_money_dates_invoice_number_and_trn(self):
        self.assertEqual(normalize_money("AED1,234.50"), 1234.5)
        self.assertEqual(normalize_money("(AED 100.50)"), -100.5)
        self.assertEqual(normalize_date("18/06/2026"), "2026-06-18")
        self.assertEqual(normalize_invoice_number("Invoice No: INV 100/25"), "INV100/25")
        self.assertEqual(normalize_trn("TRN: 100123456700003"), "100123456700003")

    def test_fuzzy_supplier_matching_uses_supplier_memory(self):
        raw = {
            "provider": "google",
            "document_type": "invoice",
            "fields": {
                "supplier_name": "EXAMPLE HYPERMARKET",
                "invoice_date": "2026-05-30",
                "total_amount": "AED 52.50",
            },
            "confidence_by_field": {
                "supplier_name": 0.45,
                "invoice_date": 0.8,
                "total_amount": 0.8,
            },
        }
        normalized = normalize_google_provider_result(
            raw,
            supplier_memory={"suppliers": {"Example Hypermarket": {}}},
        )
        self.assertEqual(normalized["fields"]["supplier_name"], "Example Hypermarket")
        self.assertGreaterEqual(normalized["confidence_by_field"]["supplier_name"], 0.9)
        self.assertIn("supplier matched", " ".join(normalized["normalization"]["notes"]))

    def test_reviewer_feedback_teaches_supplier_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            feedback = Path(tmp) / "google_extraction_feedback.jsonl"
            feedback.write_text(json.dumps({
                "before": {"supplier_name": "Sample Salon"},
                "after": {"supplier_name": "Example Chillers LLC"},
                "extraction_mistakes": [
                    {
                        "field": "supplier_name",
                        "before": "Sample Salon",
                        "after": "Example Chillers LLC",
                    }
                ],
            }) + "\n")
            normalized = normalize_google_provider_result(
                {
                    "provider": "google",
                    "document_type": "invoice",
                    "fields": {
                        "supplier_name": "Sample Salon",
                        "invoice_date": "2026-06-18",
                        "total_amount": "AED 800.00",
                    },
                    "confidence_by_field": {
                        "supplier_name": 0.56,
                        "invoice_date": 0.78,
                        "total_amount": 0.56,
                    },
                },
                feedback_path=feedback,
            )
        self.assertEqual(normalized["fields"]["supplier_name"], "Example Chillers LLC")
        self.assertEqual(normalized["confidence_by_field"]["supplier_name"], 1.0)

    def test_normalizes_google_output_before_validation(self):
        normalized = normalize_google_provider_result(
            {
                "provider": "google",
                "document_type": "invoice",
                "fields": {
                    "vendor_name": "Contoso",
                    "date": "20/06/2026",
                    "invoice_total": "AED 420.00",
                    "supplier_tax_id": "TRN 100123456700003",
                },
                "confidence_by_field": {
                    "vendor_name": 0.52,
                    "date": 0.8,
                    "invoice_total": 0.71,
                    "supplier_tax_id": 0.8,
                },
            },
            supplier_memory={"suppliers": {"Contoso": {}}},
        )
        validation = _validate_google_provider_result(normalized)
        self.assertTrue(validation["valid"])
        self.assertEqual(normalized["fields"]["invoice_date"], "2026-06-20")
        self.assertEqual(normalized["fields"]["total_amount"], 420.0)
        self.assertEqual(normalized["fields"]["supplier_trn"], "100123456700003")

    def test_line_items_are_canonical_and_can_infer_total(self):
        normalized = normalize_google_provider_result({
            "provider": "google",
            "document_type": "invoice",
            "fields": {
                "supplier_name": "Vendor A",
                "invoice_date": "2026-06-01",
            },
            "confidence_by_field": {
                "supplier_name": 0.9,
                "invoice_date": 0.9,
            },
            "line_items": [
                {"line_item_description": "Shampoo", "quantity": "2", "unit_price": "AED 10.50", "amount": "AED 21.00"},
                {"description": "Conditioner", "amount": "AED 9.00"},
            ],
        })
        self.assertEqual(normalized["fields"]["total_amount"], 30.0)
        self.assertEqual(normalized["line_items"][0]["unit_price"], 10.5)
        self.assertEqual(normalized["confidence_by_field"]["total_amount"], 0.55)


if __name__ == "__main__":
    unittest.main()
