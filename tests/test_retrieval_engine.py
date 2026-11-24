import tempfile
import unittest
from pathlib import Path

from extraction import ai_extractor
from extraction.retrieval_engine import retrieve_context


class RetrievalEngineTests(unittest.TestCase):
    def setUp(self):
        self.suppliers = {
            "tailspin": {"name": "Tailspin Trading LLC"},
            "northwind": {"name": "Northwind Energy Services LLC"},
            "contoso": {"name": "Contoso Hypermarket"},
            "fabrikam": {"name": "Fabrikam Trading Company"},
            "office": {"name": "Office Supplies LLC"},
        }
        self.supplier_account_map = {
            "Tailspin Trading LLC": {
                "primary": "Food Purchases",
                "frequency": {
                    "Food Purchases": 12,
                    "Packaging Supplies": 2,
                },
                "confidence": "high",
            },
            "Northwind Energy Services LLC": {
                "primary": "Utilities: Chiller",
                "frequency": {
                    "Utilities: Chiller": 8,
                    "Repairs and Maintenance": 1,
                },
                "confidence": "high",
            },
            "Contoso Hypermarket": {
                "primary": "Staff Welfare",
                "frequency": {"Staff Welfare": 4},
                "confidence": "high",
            },
            "Fabrikam Trading Company": {
                "primary": "Salon Supplies",
                "frequency": {"Salon Supplies": 6},
                "confidence": "high",
            },
        }
        self.coa = {
            "food": {"name": "Food Purchases", "account_type": "Expense"},
            "packaging": {"name": "Packaging Supplies", "account_type": "Expense"},
            "chiller": {"name": "Utilities: Chiller", "account_type": "Expense"},
            "maintenance": {"name": "Repairs and Maintenance", "account_type": "Expense"},
            "staff": {"name": "Staff Welfare", "account_type": "Expense"},
            "salon": {"name": "Salon Supplies", "account_type": "Expense"},
            "rent": {"name": "Rent Expense", "account_type": "Expense"},
        }

    def test_tailspin_only_loads_tailspin_history(self):
        context = retrieve_context(
            coa=self.coa,
            suppliers=self.suppliers,
            supplier_account_map=self.supplier_account_map,
            supplier_hint="Tailspin Trading LLC",
            supplier_confidence=0.96,
            document_type="invoice",
        )

        self.assertEqual(list(context["supplier_history"]), ["Tailspin Trading LLC"])
        self.assertIn("tailspin", context["suppliers"])
        self.assertNotIn("northwind", context["suppliers"])
        self.assertNotIn("Northwind Energy Services LLC", context["supplier_history"])

    def test_northwind_only_loads_northwind_history(self):
        context = retrieve_context(
            coa=self.coa,
            suppliers=self.suppliers,
            supplier_account_map=self.supplier_account_map,
            supplier_hint="Northwind",
            supplier_confidence=0.95,
            document_type="invoice",
        )

        self.assertEqual(list(context["supplier_history"]), ["Northwind Energy Services LLC"])
        self.assertIn("northwind", context["suppliers"])
        self.assertNotIn("tailspin", context["suppliers"])
        self.assertNotIn("Tailspin Trading LLC", context["supplier_history"])

    def test_no_unrelated_accounts_are_returned(self):
        context = retrieve_context(
            coa=self.coa,
            suppliers=self.suppliers,
            supplier_account_map=self.supplier_account_map,
            supplier_hint="Tailspin Trading LLC",
            supplier_confidence=0.97,
            document_type="invoice",
        )

        account_names = {account["name"] for account in context["accounts"].values()}
        self.assertLessEqual(len(account_names), 5)
        self.assertIn("Food Purchases", account_names)
        self.assertIn("Packaging Supplies", account_names)
        self.assertNotIn("Utilities: Chiller", account_names)
        self.assertNotIn("Salon Supplies", account_names)
        self.assertNotIn("Rent Expense", account_names)

    def test_high_confidence_supplier_does_not_load_unrelated_suppliers(self):
        context = retrieve_context(
            coa=self.coa,
            suppliers=self.suppliers,
            supplier_account_map=self.supplier_account_map,
            text="Northwind appears in unrelated email footer text",
            supplier_hint="Tailspin Trading LLC",
            supplier_confidence=0.99,
            document_type="invoice",
        )

        self.assertEqual(set(context["suppliers"]), {"tailspin"})
        self.assertEqual(set(context["supplier_history"]), {"Tailspin Trading LLC"})

    def test_dry_run_plan_reports_retrieval_token_savings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Tailspin invoice.pdf"
            path.write_text("Tailspin Trading LLC invoice for food purchases")

            plan = ai_extractor._build_extraction_plan(
                [[path]],
                [],
                {"stale": False, "files": []},
                coa=self.coa,
                suppliers=self.suppliers,
                supplier_account_map=self.supplier_account_map,
            )

        self.assertIn("old_supplier_context_tokens", plan)
        self.assertIn("new_supplier_context_tokens", plan)
        self.assertIn("old_coa_context_tokens", plan)
        self.assertIn("new_coa_context_tokens", plan)
        self.assertIn("retrieval_context_tokens_saved", plan)
        self.assertLessEqual(plan["new_supplier_context_tokens"], plan["old_supplier_context_tokens"])
        self.assertLessEqual(plan["new_coa_context_tokens"], plan["old_coa_context_tokens"])


if __name__ == "__main__":
    unittest.main()
