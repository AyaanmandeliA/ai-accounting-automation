import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from extraction import ai_extractor
from extraction.model_routing import default_extraction_model, load_model_routing_config


class ExtractionCostControlTests(unittest.TestCase):
    def _patch_dirs(self, base: Path):
        return patch.multiple(
            ai_extractor,
            RAW_DIR=base / "data/raw",
            PARSED_DIR=base / "data/parsed",
            EXTRACTED_DIR=base / "data/extracted",
            PROCESSED_DIR=base / "data/processed",
            PROCESSED_FILE=base / "data/processed/ai_extracted.json",
        )

    def test_high_cost_extraction_is_blocked_without_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw = base / "data/raw"
            raw.mkdir(parents=True)
            big_invoice = raw / "big_invoice.pdf"
            with big_invoice.open("wb") as f:
                f.truncate(10 * 1024 * 1024)

            with self._patch_dirs(base), \
                 patch("extraction.ai_extractor.load_coa", return_value={}), \
                 patch("extraction.ai_extractor.load_suppliers", return_value={}), \
                 patch("extraction.ai_extractor.load_supplier_account_map", return_value={}), \
                 patch("extraction.ai_extractor._load_api_key") as load_api_key, \
                 patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client:
                result = ai_extractor.run(dry_run=False, assume_yes=True)

            self.assertEqual(result, [])
            load_api_key.assert_not_called()
            anthropic_client.assert_not_called()

    def test_max_docs_limits_planned_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            paths = []
            for i in range(3):
                path = base / f"invoice_{i}.pdf"
                path.write_text("fake")
                paths.append(path)
            groups = [[path] for path in paths]
            plan = ai_extractor._build_extraction_plan(
                groups,
                [],
                {"stale": False, "files": []},
                max_docs=2,
            )
            self.assertEqual(plan["document_groups_count"], 2)
            self.assertEqual(plan["file_count"], 2)
            self.assertTrue(plan["limited_by_max_docs"])

    def test_dry_run_never_calls_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw = base / "data/raw"
            raw.mkdir(parents=True)
            (raw / "invoice.pdf").write_text("fake")

            with self._patch_dirs(base), \
                 patch("extraction.ai_extractor.load_coa", return_value={}), \
                 patch("extraction.ai_extractor.load_suppliers", return_value={}), \
                 patch("extraction.ai_extractor.load_supplier_account_map", return_value={}), \
                 patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client:
                result = ai_extractor.run(dry_run=True)

            self.assertEqual(result, [])
            anthropic_client.assert_not_called()

    def test_dry_run_estimates_classifier_pass_without_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw = base / "data/raw"
            raw.mkdir(parents=True)
            (raw / "invoice.pdf").write_text("fake")
            stdout = StringIO()

            with self._patch_dirs(base), \
                 patch("extraction.ai_extractor.load_coa", return_value={}), \
                 patch("extraction.ai_extractor.load_suppliers", return_value={}), \
                 patch("extraction.ai_extractor.load_supplier_account_map", return_value={}), \
                 patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client, \
                 redirect_stdout(stdout):
                result = ai_extractor.run(dry_run=True)

            output = stdout.getvalue()
            self.assertEqual(result, [])
            anthropic_client.assert_not_called()
            self.assertIn("classifier requests estimated: 0", output)
            self.assertIn("low-cost provider required count: 1", output)
            self.assertIn("Claude-required document count: 0", output)
            self.assertIn("provider warning: No primary low-cost provider configured", output)
            self.assertIn("classifier pass: no (low_cost_provider)", output)

    def test_dry_run_shows_stale_ledger_as_reprocessing_driver(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw = base / "data/raw"
            processed = base / "data/processed"
            raw.mkdir(parents=True)
            processed.mkdir(parents=True)
            (raw / "invoice.pdf").write_text("fake")
            (processed / "ai_extracted.json").write_text('{"version": 1, "files": ["invoice.pdf"]}')
            stdout = StringIO()

            with self._patch_dirs(base), \
                 patch("extraction.ai_extractor.load_coa", return_value={}), \
                 patch("extraction.ai_extractor.load_suppliers", return_value={}), \
                 patch("extraction.ai_extractor.load_supplier_account_map", return_value={}), \
                 patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client, \
                 redirect_stdout(stdout):
                result = ai_extractor.run(dry_run=True)

            output = stdout.getvalue()
            self.assertEqual(result, [])
            anthropic_client.assert_not_called()
            self.assertIn("reprocessing old examples: yes", output)
            self.assertIn("stale reprocessing driver: extraction ledger version 1 != current schema 15", output)

    def test_model_routing_config_loads_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            self.assertEqual(default_extraction_model(missing), "claude-sonnet-4-6")

            invalid = Path(tmp) / "invalid.json"
            invalid.write_text("{")
            self.assertEqual(load_model_routing_config(invalid)["default_model"], "claude-sonnet-4-6")

            custom = Path(tmp) / "model_routing.json"
            custom.write_text('{"default_model": "claude-sonnet-4-6", "routes": {"simple_invoice": {"enabled": false}}}')
            config = load_model_routing_config(custom)
            self.assertIn("simple_invoice", config["routes"])
            self.assertIn("low_confidence_retry", config["routes"])


if __name__ == "__main__":
    unittest.main()
