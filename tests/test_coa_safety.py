import unittest

from extraction.ai_extractor import _filter_coa_by_category
from extraction.coa_loader import (
    filter_supplier_bill_candidate_accounts,
    validate_account_for_transaction,
)


class CoaSafetyTests(unittest.TestCase):
    def _coa(self):
        return {
            "repairs": {"name": "Repairs", "account_type": "Expense", "detail_type": "Repairs and Maintenance"},
            "cogs": {"name": "Salon Product COGS", "account_type": "Cost of Goods Sold", "detail_type": "Supplies"},
            "other expense": {"name": "Bank Charges", "account_type": "Other Expense", "detail_type": "Bank Charges"},
            "bank": {"name": "Example Bank", "account_type": "Bank", "detail_type": "Cash on hand"},
            "asset": {"name": "Uncategorized Asset", "account_type": "Other Current Asset", "detail_type": "Other Current Asset"},
            "income": {"name": "Uncategorized Income", "account_type": "Income", "detail_type": "Sales"},
            "ap": {"name": "Accounts Payable", "account_type": "Accounts Payable", "detail_type": "Accounts Payable"},
            "vat": {"name": "VAT Control", "account_type": "Other Current Liability", "detail_type": "Tax Payable"},
        }

    def test_supplier_bill_candidates_are_limited_to_posting_expense_types(self):
        filtered = filter_supplier_bill_candidate_accounts(self._coa())
        self.assertEqual(set(filtered), {"repairs", "cogs", "other expense"})

    def test_category_filter_never_reintroduces_tax_or_balance_sheet_accounts(self):
        filtered = _filter_coa_by_category(self._coa(), "other")
        names = {account["name"] for account in filtered.values()}
        self.assertNotIn("VAT Control", names)
        self.assertNotIn("Example Bank", names)
        self.assertNotIn("Uncategorized Income", names)

    def test_supplier_bill_rejects_incompatible_account_type(self):
        reason = validate_account_for_transaction(
            {"name": "Uncategorized Income", "account_type": "Income", "detail_type": "Sales"},
            {"qbo_posting_type": "Bill", "document_category": "Supplier Bill"},
            {"account_name": "Uncategorized Income"},
        )
        self.assertIn("Incompatible account", reason)

    def test_supplier_bill_accepts_expense_account_type(self):
        reason = validate_account_for_transaction(
            {"name": "Repairs", "account_type": "Expense", "detail_type": "Repairs and Maintenance"},
            {"qbo_posting_type": "Bill", "document_category": "Supplier Bill"},
            {"account_name": "Repairs"},
        )
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()

