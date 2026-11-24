import unittest

from accounting.payment_matching import suggest_payment_matches


class PaymentMatchingTests(unittest.TestCase):
    def _bill(self, **overrides):
        data = {
            "supplier_name": "Fabrikam Trading",
            "invoice_number": "INV-100",
            "invoice_date": "2026-06-01",
            "total_amount": 500,
            "open_amount": 500,
            "email_subject": "Invoice INV-100",
            "file_text": "Fabrikam Trading invoice INV-100",
        }
        data.update(overrides)
        return data

    def test_payment_proof_same_email_as_invoice(self):
        proof = {
            "supplier_name": "Fabrikam Trading",
            "payment_amount": 500,
            "payment_date": "2026-06-03",
            "file_text": "Paid invoice INV-100",
        }
        suggestions = suggest_payment_matches(proof, [self._bill()])
        self.assertGreaterEqual(suggestions[0]["match_confidence"], 0.80)
        self.assertFalse(suggestions[0]["requires_review"])

    def test_payment_proof_arrives_later(self):
        proof = {
            "supplier_name": "Fabrikam Trading",
            "payment_amount": 500,
            "payment_date": "2026-06-25",
            "file_text": "Settlement for invoice INV-100",
        }
        suggestions = suggest_payment_matches(proof, [self._bill()])
        self.assertGreater(suggestions[0]["match_confidence"], 0.70)

    def test_partial_payment_requires_review(self):
        proof = {
            "supplier_name": "Fabrikam Trading",
            "payment_amount": 250,
            "payment_date": "2026-06-03",
            "file_text": "Partial payment INV-100",
        }
        suggestions = suggest_payment_matches(proof, [self._bill()])
        self.assertTrue(suggestions[0]["requires_review"])
        self.assertIn("partial", suggestions[0]["match_reason"])

    def test_overpayment_requires_review(self):
        proof = {
            "supplier_name": "Fabrikam Trading",
            "payment_amount": 550,
            "payment_date": "2026-06-03",
            "file_text": "Payment INV-100",
        }
        suggestions = suggest_payment_matches(proof, [self._bill()])
        self.assertTrue(suggestions[0]["requires_review"])
        self.assertIn("overpayment", suggestions[0]["match_reason"])

    def test_wrong_supplier_same_amount_is_lower_confidence(self):
        proof = {
            "supplier_name": "Wrong Supplier",
            "payment_amount": 500,
            "payment_date": "2026-06-03",
            "file_text": "Payment transfer",
        }
        suggestions = suggest_payment_matches(proof, [self._bill()])
        self.assertTrue(suggestions[0]["requires_review"])

    def test_same_supplier_same_amount_multiple_invoices_is_ambiguous(self):
        proof = {
            "supplier_name": "Fabrikam Trading",
            "payment_amount": 500,
            "payment_date": "2026-06-03",
            "file_text": "Payment transfer",
        }
        bills = [self._bill(invoice_number="INV-100"), self._bill(invoice_number="INV-101")]
        suggestions = suggest_payment_matches(proof, bills)
        self.assertTrue(suggestions[0]["requires_review"])
        self.assertIn("ambiguous", suggestions[0]["match_reason"])
        self.assertEqual(suggestions[0]["match_status"], "Needs Review")

    def test_email_thread_and_attachment_group_raise_confidence(self):
        proof = {
            "supplier_name": "Fabrikam Trading",
            "payment_amount": 500,
            "payment_date": "2026-06-03",
            "payment_reference": "Paid INV-100",
            "email_thread_id": "thread-1",
            "attachment_group_id": "group-1",
        }
        bill = self._bill(email_thread_id="thread-1", attachment_group_id="group-1")
        suggestions = suggest_payment_matches(proof, [bill])
        self.assertFalse(suggestions[0]["requires_review"])
        self.assertEqual(suggestions[0]["match_status"], "Matched")


if __name__ == "__main__":
    unittest.main()
