import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from accounting.rule_retrieval import apply_learning_rules


class LearningRuleApplicationTests(unittest.TestCase):
    def test_vendor_branch_account_rules_apply_before_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            vendor_rules = tmp_path / "vendor_rules.json"
            branch_rules = tmp_path / "vendor_branch_rules.json"
            account_rules = tmp_path / "account_rules.json"
            supplier_map = tmp_path / "supplier_account_map.json"
            vendor_rules.write_text(json.dumps({
                "vendors": {
                    "Vendor A": {
                        "posting_type": "Expense",
                        "qbo_posting_type": "Expense",
                        "vat_treatment": "Claimable VAT",
                        "payment_status": "Paid",
                    }
                }
            }))
            branch_rules.write_text(json.dumps({
                "vendors": {"Vendor A": {"brand": "Sample Brand A", "branch": "Branch East", "allocation_type": "branch"}}
            }))
            account_rules.write_text(json.dumps({
                "vendors": {"Vendor A": {"primary_account": "Repairs and Maintenance"}}
            }))
            supplier_map.write_text(json.dumps({"suppliers": {}}))
            item = {"supplier_name": "Vendor A", "line_items": [{}]}
            with patch("accounting.rule_retrieval.VENDOR_RULES_FILE", vendor_rules), \
                 patch("accounting.rule_retrieval.VENDOR_BRANCH_RULES_FILE", branch_rules), \
                 patch("accounting.rule_retrieval.ACCOUNT_RULES_FILE", account_rules), \
                 patch("accounting.rule_retrieval.SUPPLIER_ACCOUNT_MAP_FILE", supplier_map):
                enriched = apply_learning_rules(item)
        self.assertEqual(enriched["qbo_posting_type"], "Expense")
        self.assertEqual(enriched["branch"], "Branch East")
        self.assertEqual(enriched["line_items"][0]["account_name"], "Repairs and Maintenance")
        self.assertGreaterEqual(len(enriched["deterministic_rules_applied"]), 3)


if __name__ == "__main__":
    unittest.main()
