import json
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.build_accounting_memory import _reference_export_history


class MemoryAndConfigTests(unittest.TestCase):
    def test_equipment_category_does_not_include_broad_asset_keyword(self):
        data = json.loads(Path("config/coa_categories.json").read_text())
        self.assertNotIn("asset", data["equipment"])

    def test_professional_fees_pro_term_has_no_trailing_space(self):
        data = json.loads(Path("config/coa_categories.json").read_text())
        self.assertIn("pro", data["professional_fees"])
        self.assertNotIn("pro ", data["professional_fees"])

    def test_new_reference_exports_are_optional(self):
        missing = Path("does-not-exist.xlsx")
        with patch("tools.build_accounting_memory.TRANSACTION_LIST_FILE", missing), \
             patch("tools.build_accounting_memory.GENERAL_LEDGER_FILE", missing), \
             patch("tools.build_accounting_memory.JOURNAL_FILE", missing):
            history = _reference_export_history()
        self.assertEqual(history["row_counts"]["transaction_list"], 0)
        self.assertEqual(history["row_counts"]["general_ledger"], 0)
        self.assertEqual(history["row_counts"]["journal"], 0)


if __name__ == "__main__":
    unittest.main()

