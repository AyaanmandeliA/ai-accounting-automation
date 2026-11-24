import unittest
from unittest.mock import patch

from qbo.dry_run_builders import build_bill_dry_run, build_expense_dry_run, build_vendor_credit_dry_run
from qbo.qbo_poster import run


class QboDryRunBuilderTests(unittest.TestCase):
    def setUp(self):
        self.audit_patch = patch("qbo.qbo_poster.append_qbo_posting_attempt")
        self.audit_patch.start()

    def tearDown(self):
        self.audit_patch.stop()

    def _item(self, **overrides):
        data = {
            "supplier_name": "Vendor A",
            "invoice_number": "INV-1",
            "invoice_date": "2026-06-23",
            "total_amount": 105,
            "tax_amount": 5,
            "overall_confidence": 0.97,
            "brand": "Sample Brand A",
            "business_unit": "Sample Brand A",
            "branch": "Branch North",
            "allocation_type": "branch",
            "document_type": "receipt",
            "document_category": "Expense Receipt",
            "accounting_treatment": "direct_expense_paid",
            "qbo_posting_type": "Expense",
            "payment_method": "card",
            "qbo_class_name": "Brand A - Branch North",
            "qbo_location_name": "Branch North",
            "line_items": [{"description": "x", "amount": 105, "account_name": "Repairs", "account_type": "Expense"}],
            "source_file": __file__,
            "all_source_files": [__file__],
            "notes": "Tax invoice TRN 100000000000003",
        }
        data.update(overrides)
        return data

    def test_expense_dry_run_payload_is_built_but_live_blocked(self):
        result = build_expense_dry_run(self._item())
        self.assertEqual(result["qbo_posting_type"], "Expense")
        self.assertEqual(result["qbo_api_entity"], "Purchase")
        self.assertTrue(result["blocked_from_live_posting"])
        self.assertEqual(result["dry_run_payload"]["TotalAmt"], 105)
        self.assertEqual(result["dry_run_payload"]["PaymentType"], "CreditCard")

    def test_vendor_credit_dry_run_payload_is_built_but_live_blocked(self):
        item = self._item(
            document_type="credit note",
            document_category="Credit Note",
            accounting_treatment="supplier_credit",
            qbo_posting_type="SupplierCredit",
        )
        result = build_vendor_credit_dry_run(item)
        self.assertEqual(result["qbo_posting_type"], "VendorCredit")
        self.assertEqual(result["qbo_api_entity"], "VendorCredit")
        self.assertTrue(result["blocked_from_live_posting"])
        self.assertEqual(result["dry_run_payload"]["VendorRef"]["name"], "Vendor A")

    def test_bill_dry_run_payload_preserves_bill_model(self):
        item = self._item(qbo_posting_type="Bill", payment_method="unpaid")
        result = build_bill_dry_run(item)
        self.assertEqual(result["qbo_api_entity"], "Bill")
        self.assertEqual(result["dry_run_payload"]["VendorRef"]["name"], "Vendor A")
        self.assertTrue(result["blocked_from_live_posting"])

    def test_feature_flags_default_off_even_if_non_bill_reaches_poster(self):
        item = self._item()
        with patch("qbo.qbo_poster._load_qbo_config", return_value={"environment": "sandbox"}), \
             patch("qbo.qbo_poster._load_processed", return_value={}), \
             patch("qbo.qbo_poster._load_fingerprints", return_value={}), \
             patch("qbo.qbo_poster.copy_to_review_folder"), \
             patch("qbo.qbo_poster._get_qbo_client") as get_client:
            posted = run([item])
        self.assertEqual(posted, [])
        get_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
