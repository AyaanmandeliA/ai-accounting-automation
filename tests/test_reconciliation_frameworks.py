import json
import tempfile
import unittest
from pathlib import Path

from accounting.bank_reconciliation import normalize_bank_transaction, suggest_bank_matches
from accounting.supplier_reconciliation import compare_supplier_statement_to_books, extract_supplier_statement, store_supplier_statement


class ReconciliationFrameworkTests(unittest.TestCase):
    def test_supplier_statement_extracts_and_stores_summary(self):
        statement = extract_supplier_statement({
            "supplier_name": "Vendor A",
            "document_category": "Supplier Statement",
            "statement_date": "2026-06-30",
            "opening_balance": 100,
            "closing_balance": 50,
            "line_items": [
                {"date": "2026-06-01", "description": "Invoice", "reference": "INV-1", "debit": 100},
                {"date": "2026-06-10", "description": "Payment", "reference": "PAY-1", "credit": 50},
                {"date": "2026-06-15", "description": "Credit note", "reference": "CN-1", "credit": 0},
            ],
        })
        self.assertTrue(statement["not_postable"])
        self.assertTrue(statement["requires_reconciliation_review"])
        self.assertIn("INV-1", statement["invoice_numbers"])
        self.assertEqual(len(statement["payments"]), 1)
        with tempfile.TemporaryDirectory() as tmp:
            out = store_supplier_statement(statement, output_dir=Path(tmp))
            saved = json.loads(out.read_text())
        self.assertEqual(saved["supplier"], "Vendor A")

    def test_bank_transaction_normalization(self):
        txn = normalize_bank_transaction({
            "date": "24/06/2026",
            "description": "Payment Vendor A INV-1",
            "debit": "105.00",
            "reference": "INV-1",
            "bank_account": "EXB",
        })
        self.assertEqual(txn["date"], "2026-06-24")
        self.assertEqual(txn["amount"], -105.0)

    def test_bank_match_suggestion(self):
        suggestions = suggest_bank_matches(
            [{
                "date": "2026-06-24",
                "description": "Payment Vendor A INV-1",
                "debit": "105.00",
                "reference": "INV-1",
                "bank_account": "EXB",
            }],
            [{
                "supplier_name": "Vendor A",
                "invoice_number": "INV-1",
                "invoice_date": "2026-06-23",
                "total_amount": 105,
            }],
        )
        self.assertEqual(suggestions[0]["status"], "matched_candidate")
        self.assertFalse(suggestions[0]["requires_review"])

    def test_month_end_statement_comparison_flags_differences(self):
        statement = extract_supplier_statement({
            "supplier_name": "Vendor A",
            "document_category": "Supplier Statement",
            "closing_balance": 150,
            "line_items": [
                {"description": "Invoice", "reference": "INV-1", "debit": 100},
                {"description": "Invoice", "reference": "INV-2", "debit": 50},
                {"description": "Credit note", "reference": "CN-1", "credit": 10},
            ],
        })
        comparison = compare_supplier_statement_to_books(statement, [
            {"invoice_number": "INV-1", "open_amount": 100},
            {"invoice_number": "INV-3", "open_amount": 25},
        ])
        self.assertEqual(comparison["status"], "needs_review")
        self.assertIn("INV-2", comparison["missing_from_books"])
        self.assertIn("INV-3", comparison["book_items_missing_from_statement"])
        self.assertIn("CN-1", comparison["credit_note_references"])
        self.assertEqual(comparison["difference_amount"], 25.0)


if __name__ == "__main__":
    unittest.main()
