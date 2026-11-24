import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from accounting.decision_engine import (
    ACTION_ATTACH_TO_EXISTING_BILL,
    ACTION_CREATE_BILL,
    ACTION_CREATE_EXPENSE,
    ACTION_CREATE_VENDOR_CREDIT,
    ACTION_IGNORE,
    ACTION_NEEDS_HUMAN_REVIEW,
    ACTION_RECONCILIATION_ONLY,
    PAID,
    PARTIALLY_PAID,
    PAYMENT_BANK_TRANSFER,
    PAYMENT_CASH,
    PAYMENT_CREDIT_CARD,
    UNPAID,
    VAT_CLAIMABLE,
    VAT_UNKNOWN,
    build_accounting_decision,
)
from allocation.rules import remember_correction


class AccountingDecisionEngineTests(unittest.TestCase):
    def _item(self, **overrides):
        data = {
            "supplier_name": "Supplier LLC",
            "document_category": "Supplier Bill",
            "document_type": "invoice",
            "payment_confirmed": False,
            "payment_method": "unpaid",
            "qbo_posting_type": "Bill",
            "total_amount": 105,
            "tax_amount": 5,
            "notes": "Tax invoice TRN 100000000000003 amount due",
            "allocation_type": "branch",
            "brand": "Sample Brand A",
            "branch": "Branch North",
            "overall_confidence": 0.97,
        }
        data.update(overrides)
        return data

    def test_supplier_invoice_unpaid_becomes_bill(self):
        decision = build_accounting_decision(self._item())
        self.assertEqual(decision["document_type"], "Supplier Invoice")
        self.assertEqual(decision["payment_status"], UNPAID)
        self.assertEqual(decision["posting_action"], ACTION_CREATE_BILL)
        self.assertEqual(decision["qbo_object"], "Bill")
        self.assertFalse(decision["requires_review"])

    def test_supplier_invoice_paid_becomes_expense(self):
        decision = build_accounting_decision(
            self._item(
                payment_confirmed=True,
                payment_amount=105,
                payment_method="bank_transfer",
                notes="Tax invoice TRN 100000000000003 paid by bank transfer",
            )
        )
        self.assertEqual(decision["payment_status"], PAID)
        self.assertEqual(decision["payment_method"], PAYMENT_BANK_TRANSFER)
        self.assertEqual(decision["posting_action"], ACTION_CREATE_EXPENSE)
        self.assertEqual(decision["qbo_object"], "Expense")

    def test_partial_payment_status_detection(self):
        decision = build_accounting_decision(
            self._item(payment_confirmed=True, payment_amount=50, payment_method="bank_transfer")
        )
        self.assertEqual(decision["payment_status"], PARTIALLY_PAID)
        self.assertEqual(decision["posting_action"], ACTION_CREATE_BILL)

    def test_credit_note_becomes_vendor_credit(self):
        decision = build_accounting_decision(self._item(document_category="Credit Note", document_type="credit note"))
        self.assertEqual(decision["document_type"], "Supplier Credit Note")
        self.assertEqual(decision["posting_action"], ACTION_CREATE_VENDOR_CREDIT)
        self.assertEqual(decision["qbo_object"], "VendorCredit")

    def test_payment_proof_is_attach_only(self):
        decision = build_accounting_decision(self._item(document_category="Payment Proof", document_type="payment proof"))
        self.assertEqual(decision["posting_action"], ACTION_ATTACH_TO_EXISTING_BILL)
        self.assertEqual(decision["qbo_object"], "None")

    def test_remittance_advice_is_attach_only(self):
        decision = build_accounting_decision(
            self._item(document_category="Remittance Advice", document_type="remittance advice")
        )
        self.assertEqual(decision["posting_action"], ACTION_ATTACH_TO_EXISTING_BILL)

    def test_supplier_statement_is_reconciliation_only(self):
        decision = build_accounting_decision(
            self._item(document_category="Supplier Statement", document_type="supplier statement")
        )
        self.assertEqual(decision["posting_action"], ACTION_RECONCILIATION_ONLY)

    def test_quotation_is_ignored(self):
        decision = build_accounting_decision(self._item(document_category="Quotation", document_type="quotation"))
        self.assertEqual(decision["posting_action"], ACTION_IGNORE)

    def test_delivery_note_is_ignored(self):
        decision = build_accounting_decision(self._item(document_category="Delivery Note", document_type="delivery note"))
        self.assertEqual(decision["posting_action"], ACTION_IGNORE)

    def test_merchant_settlement_is_reconciliation_only(self):
        decision = build_accounting_decision(
            self._item(document_type="merchant settlement", accounting_treatment="merchant_settlement")
        )
        self.assertEqual(decision["document_type"], "Merchant Settlement")
        self.assertEqual(decision["posting_action"], ACTION_RECONCILIATION_ONLY)

    def test_asset_purchase_requires_review(self):
        decision = build_accounting_decision(
            self._item(document_category="Asset Purchase", possible_capital_asset=True)
        )
        self.assertEqual(decision["posting_action"], ACTION_NEEDS_HUMAN_REVIEW)
        self.assertTrue(decision["capitalisation_required"])
        self.assertTrue(decision["requires_review"])

    def test_capital_asset_keyword_detection_requires_review(self):
        decision = build_accounting_decision(self._item(notes="Tax invoice TRN 100000000000003 for salon chair"))
        self.assertEqual(decision["document_type"], "Asset Purchase")
        self.assertTrue(decision["capitalisation_required"])
        self.assertTrue(decision["requires_review"])

    def test_shared_expense_requires_review(self):
        decision = build_accounting_decision(
            self._item(document_category="Shared Cost", allocation_type="shared")
        )
        self.assertEqual(decision["posting_action"], ACTION_NEEDS_HUMAN_REVIEW)
        self.assertTrue(decision["requires_review"])

    def test_unknown_requires_review(self):
        decision = build_accounting_decision(
            self._item(document_category="Unknown", document_type="unknown", notes="unclear document")
        )
        self.assertEqual(decision["document_type"], "Unknown")
        self.assertEqual(decision["posting_action"], ACTION_NEEDS_HUMAN_REVIEW)
        self.assertTrue(decision["requires_review"])

    def test_cash_receipt(self):
        decision = build_accounting_decision(
            self._item(document_type="receipt", notes="Cash receipt TRN 100000000000003", payment_method="cash")
        )
        self.assertEqual(decision["payment_status"], PAID)
        self.assertEqual(decision["payment_method"], PAYMENT_CASH)
        self.assertEqual(decision["posting_action"], ACTION_CREATE_EXPENSE)

    def test_credit_card_receipt(self):
        decision = build_accounting_decision(
            self._item(document_type="receipt", notes="Card receipt TRN 100000000000003", payment_method="credit_card")
        )
        self.assertEqual(decision["payment_method"], PAYMENT_CREDIT_CARD)
        self.assertEqual(decision["posting_action"], ACTION_CREATE_EXPENSE)

    def test_bank_transfer_receipt(self):
        decision = build_accounting_decision(
            self._item(notes="Tax invoice TRN 100000000000003 paid by bank transfer")
        )
        self.assertEqual(decision["payment_status"], PAID)
        self.assertEqual(decision["payment_method"], PAYMENT_BANK_TRANSFER)

    def test_vat_claimable(self):
        decision = build_accounting_decision(self._item())
        self.assertEqual(decision["vat_treatment"], VAT_CLAIMABLE)

    def test_vat_unknown_requires_review(self):
        decision = build_accounting_decision(self._item(tax_amount=5, notes="Tax invoice without TRN"))
        self.assertEqual(decision["vat_treatment"], VAT_UNKNOWN)
        self.assertTrue(decision["requires_review"])

    def test_low_confidence_requires_review(self):
        decision = build_accounting_decision(self._item(overall_confidence=0.42))
        self.assertTrue(decision["requires_review"])
        self.assertIn("Confidence 0.42", decision["review_reason"])

    def test_folder_conflict_requires_review(self):
        decision = build_accounting_decision(self._item(folder_branch="Branch East", branch="Branch North"))
        self.assertTrue(decision["requires_review"])
        self.assertIn("Folder allocation conflicts", decision["review_reason"])

    def test_decision_explanation_is_present(self):
        decision = build_accounting_decision(self._item())
        self.assertIn("Supplier Invoice detected", decision["decision_explanation"])
        self.assertIn("Posting action: Create Bill", decision["decision_explanation"])
        self.assertIn("Confidence 97%", decision["decision_explanation"])

    def test_human_correction_updates_vendor_decision_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            rules_path = Path(tmp) / "vendor_rules.json"
            branch_rules_path = Path(tmp) / "vendor_branch_rules.json"
            item = self._item(
                supplier_name="Corrected Supplier",
                posting_action="Create Expense",
                posting_type="Create Expense",
                qbo_object="Expense",
                qbo_posting_type="Expense",
                vat_treatment="Non Claimable",
                payment_status="Paid",
                payment_method="Cash",
                document_category="Expense Receipt",
                accounting_decision={"document_type": "Expense Receipt"},
            )
            with patch("allocation.rules.VENDOR_RULES_FILE", rules_path), \
                 patch("allocation.rules.VENDOR_BRANCH_RULES_FILE", branch_rules_path):
                remember_correction(item)
            text = rules_path.read_text()
        self.assertIn("Corrected Supplier", text)
        self.assertIn("Non Claimable", text)
        self.assertIn("Create Expense", text)
        self.assertIn("Expense Receipt", text)


if __name__ == "__main__":
    unittest.main()
