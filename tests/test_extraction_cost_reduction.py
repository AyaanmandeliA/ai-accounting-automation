import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from document_processing.media_optimizer import optimize_media_for_ai
from extraction import ai_extractor


class ExtractionCostReductionTests(unittest.TestCase):
    def _config(self, **overrides):
        config = dict(ai_extractor.DEFAULT_EXTRACTION_COST_CONTROL)
        config.update(overrides)
        return config

    def test_text_pdf_uses_text_only_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invoice.pdf"
            path.write_text("placeholder")
            preview = {
                "text": "Tax Invoice Supplier TRN 123456789012345 Date 2026-06-01 Total AED 1,000 " * 10,
                "quality_score": 0.95,
                "page_count": 1,
                "method": "text_extract_ok",
                "warnings": [],
            }
            with patch("extraction.ai_extractor.extract_text_preview", return_value=preview):
                route = ai_extractor._plan_group_route([path], self._config())
        self.assertEqual(route["route"], "text_only")
        self.assertEqual(route["files"][0]["kind"], "text")
        self.assertEqual(route["files"][0]["original_path"], path)

    def test_poor_text_pdf_uses_media_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan.pdf"
            path.write_text("placeholder")
            preview = {
                "text": "tiny",
                "quality_score": 0.1,
                "page_count": 1,
                "method": "text_extract_low_quality",
                "warnings": ["short"],
            }
            with patch("extraction.ai_extractor.extract_text_preview", return_value=preview):
                route = ai_extractor._plan_group_route([path], self._config())
        self.assertEqual(route["route"], "optimized_media")
        self.assertEqual(route["files"][0]["kind"], "media")

    def test_oversized_media_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "huge.pdf"
            with path.open("wb") as handle:
                handle.truncate(8 * 1024 * 1024)
            preview = {
                "text": "",
                "quality_score": 0,
                "page_count": 1,
                "method": "text_extract_low_quality",
                "warnings": [],
            }
            with patch("extraction.ai_extractor.extract_text_preview", return_value=preview):
                route = ai_extractor._plan_group_route([path], self._config(max_media_file_mb_without_confirmation=2.0))
        self.assertEqual(route["route"], "full_media_requires_confirmation")
        self.assertTrue(route["requires_confirmation"])

    def test_optimized_copy_is_created_without_touching_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "invoice.png"
            output_dir = Path(tmp) / "optimized"
            Image.new("RGB", (2400, 1600), "white").save(source)
            original_bytes = source.read_bytes()

            result = optimize_media_for_ai(source, output_dir=output_dir)

            self.assertEqual(source.read_bytes(), original_bytes)
            self.assertTrue(Path(result["optimized_path"]).exists())
            self.assertNotEqual(Path(result["optimized_path"]).resolve(), source.resolve())
            self.assertLessEqual(result["optimized_file_size_mb"], result["original_file_size_mb"])

    def test_classifier_uses_text_preview_before_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "invoice.pdf"
            path.write_text("placeholder")
            route = {
                "route": "text_only",
                "files": [{
                    "original_path": path,
                    "ai_path": None,
                    "kind": "text",
                    "route": "text_only",
                    "text": "ambiguous local preview without category",
                    "text_preview": {"quality_score": 0.9},
                    "original_file_size_mb": 0.1,
                    "optimized_file_size_mb": 0,
                    "reason": "test",
                    "requires_confirmation": False,
                }],
            }
            response = SimpleNamespace(content=[SimpleNamespace(type="text", text="other")])
            client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: response))

            with patch("extraction.ai_extractor._category_from_supplier_memory", return_value=None), \
                 patch("extraction.ai_extractor._category_from_local_text", return_value=None), \
                 patch("extraction.ai_extractor._file_block") as file_block:
                category = ai_extractor._classify_bill(client, [path], route)

            self.assertEqual(category, "other")
            file_block.assert_not_called()

    def test_max_cost_blocks_extraction_without_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw = base / "data/raw"
            raw.mkdir(parents=True)
            (raw / "invoice.pdf").write_text("fake")

            with patch.multiple(
                ai_extractor,
                RAW_DIR=raw,
                PARSED_DIR=base / "data/parsed",
                EXTRACTED_DIR=base / "data/extracted",
                PROCESSED_DIR=base / "data/processed",
                PROCESSED_FILE=base / "data/processed/ai_extracted.json",
            ), patch("extraction.ai_extractor.load_coa", return_value={}), \
                 patch("extraction.ai_extractor.load_suppliers", return_value={}), \
                 patch("extraction.ai_extractor.load_supplier_account_map", return_value={}), \
                 patch("extraction.ai_extractor._load_api_key") as load_api_key, \
                 patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client:
                result = ai_extractor.run(dry_run=False, assume_yes=True, max_cost_aed=0.001)

            self.assertEqual(result, [])
            load_api_key.assert_not_called()
            anthropic_client.assert_not_called()

    def test_no_claude_local_text_routes_to_review_without_media_or_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw = base / "data/raw"
            raw.mkdir(parents=True)
            invoice = raw / "invoice.pdf"
            invoice.write_text("fake")
            preview = {
                "text": "Unmatched Supplier Tax Invoice INV-100 Date 2026-06-01 Total AED 105.00 " * 12,
                "quality_score": 0.95,
                "page_count": 1,
                "method": "text_extract_ok",
                "warnings": [],
            }

            with patch.multiple(
                ai_extractor,
                RAW_DIR=raw,
                PARSED_DIR=base / "data/parsed",
                EXTRACTED_DIR=base / "data/extracted",
                PROCESSED_DIR=base / "data/processed",
                PROCESSED_FILE=base / "data/processed/ai_extracted.json",
            ), patch("extraction.ai_extractor.load_coa", return_value={}), \
                 patch("extraction.ai_extractor.load_suppliers", return_value={}), \
                 patch("extraction.ai_extractor.load_supplier_account_map", return_value={}), \
                 patch("extraction.ai_extractor.load_supplier_templates", return_value=[]), \
                 patch("extraction.ai_extractor.extract_text_preview", return_value=preview), \
                 patch("allocation.review_router.REVIEW_ROOT", base / "data/review"), \
                 patch("extraction.ai_extractor._file_block") as file_block, \
                 patch("extraction.ai_extractor._load_api_key") as load_api_key, \
                 patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client:
                result = ai_extractor.run(dry_run=False, assume_yes=True, no_claude=True)

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["extraction_method"], "local_text_extraction")
            self.assertTrue(result[0]["needs_review"])
            self.assertEqual(result[0]["qbo_posting_type"], "None")
            file_block.assert_not_called()
            load_api_key.assert_not_called()
            anthropic_client.assert_not_called()

    def test_failed_local_extraction_routes_to_provider_required_queue_without_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw = base / "data/raw"
            raw.mkdir(parents=True)
            invoice = raw / "scan.pdf"
            invoice.write_text("fake")
            preview = {
                "text": "",
                "quality_score": 0.0,
                "page_count": 1,
                "method": "text_extract_empty",
                "warnings": [],
            }

            with patch.multiple(
                ai_extractor,
                RAW_DIR=raw,
                PARSED_DIR=base / "data/parsed",
                EXTRACTED_DIR=base / "data/extracted",
                PROCESSED_DIR=base / "data/processed",
                PROCESSED_FILE=base / "data/processed/ai_extracted.json",
            ), patch("extraction.ai_extractor.load_coa", return_value={}), \
                 patch("extraction.ai_extractor.load_suppliers", return_value={}), \
                 patch("extraction.ai_extractor.load_supplier_account_map", return_value={}), \
                 patch("extraction.ai_extractor.load_supplier_templates", return_value=[]), \
                 patch("extraction.ai_extractor.extract_text_preview", return_value=preview), \
                 patch("extraction.ai_extractor._load_api_key") as load_api_key, \
                 patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client:
                result = ai_extractor.run(dry_run=False, assume_yes=True)

            report = json.loads((base / "data/processed/provider_required.json").read_text())
            self.assertEqual(result, [])
            self.assertEqual(report["count"], 1)
            self.assertEqual(report["items"][0]["files"], ["scan.pdf"])
            self.assertIn("No primary low-cost provider configured", report["items"][0]["reason"])
            self.assertIn("no local text", report["items"][0]["reason"])
            load_api_key.assert_not_called()
            anthropic_client.assert_not_called()

    def test_dry_run_no_claude_shows_near_zero_current_cost_and_claude_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw = base / "data/raw"
            raw.mkdir(parents=True)
            invoice = raw / "scan.pdf"
            invoice.write_text("fake")
            preview = {
                "text": "",
                "quality_score": 0.0,
                "page_count": 1,
                "method": "text_extract_empty",
                "warnings": [],
            }
            stdout = StringIO()

            with patch.multiple(
                ai_extractor,
                RAW_DIR=raw,
                PARSED_DIR=base / "data/parsed",
                EXTRACTED_DIR=base / "data/extracted",
                PROCESSED_DIR=base / "data/processed",
                PROCESSED_FILE=base / "data/processed/ai_extracted.json",
            ), patch("extraction.ai_extractor.load_coa", return_value={}), \
                 patch("extraction.ai_extractor.load_suppliers", return_value={}), \
                 patch("extraction.ai_extractor.load_supplier_account_map", return_value={}), \
                 patch("extraction.ai_extractor.load_supplier_templates", return_value=[]), \
                 patch("extraction.ai_extractor.extract_text_preview", return_value=preview), \
                 patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client, \
                 redirect_stdout(stdout):
                result = ai_extractor.run(dry_run=True, no_claude=True)

            output = stdout.getvalue()
            self.assertEqual(result, [])
            self.assertIn("Claude-required document count: 1", output)
            self.assertIn("Claude blocked document count: 1", output)
            self.assertIn("estimated cost AED: AED 0.0000", output)
            self.assertIn("estimated cost if Claude disabled: AED 0.0000", output)
            self.assertIn("estimated cost if Claude enabled:", output)
            anthropic_client.assert_not_called()

    def test_low_cost_mode_requires_provider_and_estimates_blended_cost(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw = base / "data/raw"
            raw.mkdir(parents=True)
            invoice = raw / "scan.pdf"
            invoice.write_text("fake")
            preview = {
                "text": "",
                "quality_score": 0.0,
                "page_count": 1,
                "method": "text_extract_empty",
                "warnings": [],
            }
            stdout = StringIO()

            with patch.multiple(
                ai_extractor,
                RAW_DIR=raw,
                PARSED_DIR=base / "data/parsed",
                EXTRACTED_DIR=base / "data/extracted",
                PROCESSED_DIR=base / "data/processed",
                PROCESSED_FILE=base / "data/processed/ai_extracted.json",
            ), patch("extraction.ai_extractor.load_coa", return_value={}), \
                 patch("extraction.ai_extractor.load_suppliers", return_value={}), \
                 patch("extraction.ai_extractor.load_supplier_account_map", return_value={}), \
                 patch("extraction.ai_extractor.load_supplier_templates", return_value=[]), \
                 patch("extraction.ai_extractor.extract_text_preview", return_value=preview), \
                 patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client, \
                 redirect_stdout(stdout):
                result = ai_extractor.run(dry_run=True, low_cost_extract=True)

            output = stdout.getvalue()
            self.assertEqual(result, [])
            self.assertIn("extraction mode: low-cost provider-first mode", output)
            self.assertIn("provider warning: No primary low-cost provider configured", output)
            self.assertIn("low-cost provider required count: 1", output)
            self.assertIn("Claude-required document count: 0", output)
            self.assertIn("estimated cheap provider cost: AED 0.2500", output)
            self.assertIn("estimated blended cost:", output)
            anthropic_client.assert_not_called()

    def test_no_claude_mode_is_reported_as_diagnostic_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            scan = Path(tmp) / "scan.jpeg"
            scan.write_text("fake")
            plan = ai_extractor._build_extraction_plan(
                [[scan]],
                [],
                {"stale": False, "files": []},
                no_claude=True,
                coa={},
                suppliers={},
                supplier_account_map={},
                supplier_templates=[],
            )
        self.assertTrue(plan["no_claude"])
        self.assertEqual(plan["estimated_cost_aed"], 0.0)

    def test_text_only_claude_cost_is_blocked_by_budget_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw = base / "data/raw"
            raw.mkdir(parents=True)
            invoice = raw / "text_invoice.pdf"
            invoice.write_text("fake")
            preview = {
                "text": "Unmatched Supplier Tax Invoice INV-100 Date 2026-06-01 Total AED 105.00 " * 5000,
                "quality_score": 0.95,
                "page_count": 1,
                "method": "text_extract_ok",
                "warnings": [],
            }

            with patch.multiple(
                ai_extractor,
                RAW_DIR=raw,
                PARSED_DIR=base / "data/parsed",
                EXTRACTED_DIR=base / "data/extracted",
                PROCESSED_DIR=base / "data/processed",
                PROCESSED_FILE=base / "data/processed/ai_extracted.json",
            ), patch("extraction.ai_extractor.load_coa", return_value={}), \
                 patch("extraction.ai_extractor.load_suppliers", return_value={}), \
                 patch("extraction.ai_extractor.load_supplier_account_map", return_value={}), \
                 patch("extraction.ai_extractor.load_supplier_templates", return_value=[]), \
                 patch("extraction.ai_extractor.extract_text_preview", return_value=preview), \
                 patch("extraction.ai_extractor._load_api_key") as load_api_key, \
                 patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client:
                result = ai_extractor.run(dry_run=False, assume_yes=True, max_cost_aed=0.001)

            self.assertEqual(result, [])
            load_api_key.assert_not_called()
            anthropic_client.assert_not_called()

    def test_optimized_path_used_only_for_ai_original_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "invoice.png"
            optimized = Path(tmp) / "optimized.jpg"
            Image.new("RGB", (10, 10), "white").save(original)
            Image.new("RGB", (5, 5), "white").save(optimized)
            route = {
                "route": "optimized_media",
                "files": [{
                    "original_path": original,
                    "ai_path": optimized,
                    "kind": "media",
                    "route": "optimized_media",
                    "text": "",
                    "text_preview": {"quality_score": 0},
                    "original_file_size_mb": 1,
                    "optimized_file_size_mb": 0.1,
                    "reason": "test",
                    "requires_confirmation": False,
                }],
            }
            blocks = ai_extractor._content_blocks_for_route(route)
        self.assertEqual(blocks[0]["source"]["media_type"], "image/jpeg")
        self.assertEqual(route["files"][0]["original_path"], original)
        self.assertEqual(route["files"][0]["ai_path"], optimized)


if __name__ == "__main__":
    unittest.main()
