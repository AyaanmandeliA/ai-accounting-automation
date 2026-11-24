import tempfile
import unittest
from pathlib import Path

from accounting.cost_controls import cost_guard_status, estimate_extraction_cost, extraction_run_cost_guard, record_ai_usage


class CostControlsTests(unittest.TestCase):
    def test_cost_estimate_contains_required_fields(self):
        path = Path(__file__)
        estimate = estimate_extraction_cost([[path]], [], model="claude-sonnet-4-6")
        self.assertEqual(estimate["documents_processed"], 1)
        self.assertEqual(estimate["model"], "claude-sonnet-4-6")
        self.assertGreater(estimate["estimated_input_tokens"], 0)
        self.assertGreater(estimate["estimated_output_tokens"], 0)
        self.assertGreater(estimate["estimated_cost_aed"], 0)

    def test_cost_guard_blocks_when_cap_exceeded(self):
        estimate = {
            "estimated_cost_aed": 150,
        }
        guard = cost_guard_status(
            estimate,
            controls={"monthly_ai_cost_limit_aed": 100, "warn_at_percent": 70, "block_at_percent": 100},
        )
        self.assertTrue(guard["warn"])
        self.assertTrue(guard["block"])

    def test_extraction_run_cost_guard_blocks_without_confirmation(self):
        guard = extraction_run_cost_guard(
            {"estimated_cost_aed": 72},
            controls={"extraction_run_cost_block_aed": 25},
        )
        self.assertTrue(guard["block"])
        self.assertEqual(guard["run_cost_block_aed"], 25)

    def test_usage_record_is_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "usage.json"
            record = record_ai_usage({"documents_processed": 1, "estimated_cost_aed": 1.5}, usage_path=path)
            text = path.read_text()
        self.assertIn("documents_processed", text)
        self.assertEqual(record["estimated_cost_aed"], 1.5)


if __name__ == "__main__":
    unittest.main()
