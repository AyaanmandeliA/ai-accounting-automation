import unittest

from accounting.rules import (
    DOCUMENT_ASSET_PURCHASE,
    DOCUMENT_CREDIT_NOTE,
    DOCUMENT_DEBIT_NOTE,
    DOCUMENT_DELIVERY_NOTE,
    DOCUMENT_EXPENSE_RECEIPT,
    DOCUMENT_HEAD_OFFICE_COST,
    DOCUMENT_PAYMENT_PROOF,
    DOCUMENT_QUOTATION,
    DOCUMENT_REMITTANCE,
    DOCUMENT_SHARED_COST,
    DOCUMENT_STATEMENT,
    DOCUMENT_SUPPLIER_BILL,
    apply_accounting_rules,
)


class AccountingRulesTests(unittest.TestCase):
    def _bill(self, **overrides):
        data = {
            "supplier_name": "Supplier LLC",
            "document_type": "invoice",
            "accounting_treatment": "supplier_bill_unpaid",
            "qbo_posting_type": "Bill",
            "invoice_date": "2026-06-01",
            "total_amount": 105,
            "tax_amount": 5,
            "line_items": [{"description": "Salon consumables", "amount": 100, "account_name": "Supplies"}],
            "allocation_type": "branch",
            "brand": "Sample Brand A",
            "branch": "Branch North",
            "notes": "Tax invoice TRN 100000000000003",
        }
        data.update(overrides)
        return data

    def test_valid_vat_detects_trn_and_rate(self):
        enriched = apply_accounting_rules(self._bill())
        self.assertEqual(enriched["vat_validation_status"], "valid")
        self.assertEqual(enriched["supplier_trn"], "100000000000003")
        self.assertAlmostEqual(enriched["vat_rate"], 0.05)

    def test_invalid_vat_requires_review(self):
        enriched = apply_accounting_rules(self._bill(total_amount=110, tax_amount=5))
        self.assertTrue(enriched["needs_review"])
        self.assertEqual(enriched["vat_validation_status"], "invalid_vat_rate")
        self.assertIn("Invalid VAT rate", enriched["review_reason"])

    def test_cutoff_prior_period_requires_review(self):
        enriched = apply_accounting_rules(self._bill(source_upload_date="2026-07-03"))
        self.assertTrue(enriched["cutoff"]["prior_period_risk"])
        self.assertTrue(enriched["needs_review"])
        self.assertIn("cutoff", enriched["review_reason"].lower())

    def test_capital_asset_is_flagged(self):
        enriched = apply_accounting_rules(
            self._bill(line_items=[{"description": "New salon chair", "amount": 100, "account_name": "Equipment"}])
        )
        self.assertTrue(enriched["possible_capital_asset"])
        self.assertEqual(enriched["capital_asset_category"], "furniture")
        self.assertTrue(enriched["needs_review"])

    def test_shared_cost_never_auto_allocates(self):
        enriched = apply_accounting_rules(self._bill(allocation_type="shared"))
        self.assertEqual(enriched["shared_expense_type"], "Shared Cost")
        self.assertTrue(enriched["needs_review"])
        self.assertIn("Shared costs", enriched["review_reason"])

    def test_corporate_tax_tags_are_added(self):
        enriched = apply_accounting_rules(self._bill(notes="Director restaurant hospitality expense"))
        self.assertIn("owner_director_expense", enriched["corporate_tax_tags"])
        self.assertIn("potential_entertainment_expense", enriched["corporate_tax_tags"])

    def test_reconciliation_frameworks_exist(self):
        enriched = apply_accounting_rules(self._bill())
        self.assertEqual(enriched["supplier_statement_reconciliation"]["status"], "not_started")
        self.assertEqual(enriched["bank_reconciliation"]["status"], "not_started")
        self.assertIn("processing_history", enriched["audit_trail"])

    def test_supplier_bill_classification(self):
        enriched = apply_accounting_rules(self._bill(document_type="invoice"))
        self.assertEqual(enriched["document_category"], DOCUMENT_SUPPLIER_BILL)

    def test_expense_receipt_classification(self):
        enriched = apply_accounting_rules(
            self._bill(document_type="receipt", accounting_treatment="cash_expense_paid", qbo_posting_type="Expense")
        )
        self.assertEqual(enriched["document_category"], DOCUMENT_EXPENSE_RECEIPT)

    def test_payment_proof_classification(self):
        enriched = apply_accounting_rules(self._bill(document_type="payment proof", notes="bank transfer receipt"))
        self.assertEqual(enriched["document_category"], DOCUMENT_PAYMENT_PROOF)

    def test_remittance_advice_classification(self):
        enriched = apply_accounting_rules(self._bill(document_type="remittance advice"))
        self.assertEqual(enriched["document_category"], DOCUMENT_REMITTANCE)

    def test_supplier_statement_classification(self):
        enriched = apply_accounting_rules(self._bill(document_type="supplier statement"))
        self.assertEqual(enriched["document_category"], DOCUMENT_STATEMENT)

    def test_quotation_classification(self):
        enriched = apply_accounting_rules(self._bill(document_type="quotation"))
        self.assertEqual(enriched["document_category"], DOCUMENT_QUOTATION)

    def test_delivery_note_classification(self):
        enriched = apply_accounting_rules(self._bill(document_type="delivery note"))
        self.assertEqual(enriched["document_category"], DOCUMENT_DELIVERY_NOTE)

    def test_credit_note_classification(self):
        enriched = apply_accounting_rules(self._bill(document_type="credit note"))
        self.assertEqual(enriched["document_category"], DOCUMENT_CREDIT_NOTE)

    def test_debit_note_classification(self):
        enriched = apply_accounting_rules(self._bill(document_type="debit note"))
        self.assertEqual(enriched["document_category"], DOCUMENT_DEBIT_NOTE)

    def test_asset_purchase_classification(self):
        enriched = apply_accounting_rules(
            self._bill(line_items=[{"description": "New computer", "amount": 100, "account_name": "Equipment"}])
        )
        self.assertEqual(enriched["document_category"], DOCUMENT_ASSET_PURCHASE)

    def test_shared_cost_classification(self):
        enriched = apply_accounting_rules(self._bill(allocation_type="shared"))
        self.assertEqual(enriched["document_category"], DOCUMENT_SHARED_COST)

    def test_head_office_cost_classification(self):
        enriched = apply_accounting_rules(self._bill(allocation_type="head_office"))
        self.assertEqual(enriched["document_category"], DOCUMENT_HEAD_OFFICE_COST)


if __name__ == "__main__":
    unittest.main()
