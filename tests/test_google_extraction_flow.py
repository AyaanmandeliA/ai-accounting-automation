import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from document_processing.document_parser import normalized_provider_result, provider_result_to_transaction
from extraction import ai_extractor


def google_invoice_result(**overrides):
    fields = {
        "supplier_name": "Example Supplier LLC",
        "invoice_number": "INV-100",
        "invoice_date": "2026-06-01",
        "subtotal_amount": 100.0,
        "tax_amount": 5.0,
        "total_amount": 105.0,
        "supplier_trn": "100000000000001",
    }
    fields.update(overrides.pop("fields", {}))
    confidence = {
        "supplier_name": 0.96,
        "invoice_number": 0.95,
        "invoice_date": 0.95,
        "subtotal_amount": 0.95,
        "tax_amount": 0.95,
        "total_amount": 0.97,
        "supplier_trn": 0.91,
    }
    confidence.update(overrides.pop("confidence_by_field", {}))
    return normalized_provider_result(
        provider="google",
        document_type=overrides.pop("document_type", "invoice"),
        raw_text="Example Supplier LLC Tax Invoice INV-100 Total AED 105.00",
        fields=fields,
        line_items=overrides.pop("line_items", [{"description": "Salon supplies", "amount": 105.0}]),
        confidence_by_field=confidence,
        page_count=1,
        raw_provider_response=overrides.pop("raw_provider_response", {"document": {"text": "raw"}}),
        **overrides,
    )


class GoogleExtractionFlowTests(unittest.TestCase):
    def _run_with_raw_file(self, callback):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw = base / "data/raw"
            raw.mkdir(parents=True)
            invoice = raw / "invoice.pdf"
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
                 patch("allocation.review_router.REVIEW_ROOT", base / "data/review"):
                return callback(base, invoice)

    def test_google_provider_result_converts_into_transaction_schema(self):
        transaction = provider_result_to_transaction(
            google_invoice_result(),
            source_file="invoice.pdf",
            folder_path_hint="/Sample Brand A/Branch North/Bills",
        )
        self.assertEqual(transaction["provider_name"], "google")
        self.assertEqual(transaction["extraction_method"], "provider_parser")
        self.assertEqual(transaction["supplier_name"], "Example Supplier LLC")
        self.assertEqual(transaction["invoice_number"], "INV-100")
        self.assertEqual(transaction["total_amount"], 105.0)

    def test_google_validation_uses_field_specific_warning_thresholds(self):
        result = google_invoice_result(
            confidence_by_field={
                "supplier_name": 0.52,
                "total_amount": 0.71,
                "invoice_date": 0.95,
            }
        )
        validation = ai_extractor._validate_google_provider_result(result)
        self.assertTrue(validation["valid"])
        self.assertEqual(validation["blockers"], [])
        self.assertTrue(any("supplier" in warning for warning in validation["warnings"]))
        self.assertGreaterEqual(validation["weighted_confidence"], 0.65)

    def test_google_validation_blocks_required_field_below_blocker_threshold(self):
        result = google_invoice_result(confidence_by_field={"total_amount": 0.5})
        validation = ai_extractor._validate_google_provider_result(result)
        self.assertFalse(validation["valid"])
        self.assertTrue(any("total" in blocker and "blocker" in blocker for blocker in validation["blockers"]))

    def test_google_validation_warns_on_minor_amount_mismatch(self):
        result = google_invoice_result(fields={"subtotal_amount": 100.0, "tax_amount": 5.0, "total_amount": 105.15})
        validation = ai_extractor._validate_google_provider_result(result)
        self.assertTrue(validation["valid"])
        self.assertTrue(any("amount reconciliation" in warning for warning in validation["warnings"]))

    def test_google_validation_blocks_material_amount_mismatch(self):
        result = google_invoice_result(fields={"subtotal_amount": 180.0, "tax_amount": 8.57, "total_amount": 171.36})
        validation = ai_extractor._validate_google_provider_result(result)
        self.assertFalse(validation["valid"])
        self.assertTrue(any("amount reconciliation" in blocker for blocker in validation["blockers"]))

    def test_valid_google_invoice_routes_to_review_without_claude_or_qbo(self):
        def scenario(base, invoice):
            with patch("extraction.ai_extractor._parse_google_provider_group", return_value=google_invoice_result()), \
                 patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client, \
                 patch("qbo.qbo_poster.run") as qbo_run:
                result = ai_extractor.run(assume_yes=True, provider_name="google")
            anthropic_client.assert_not_called()
            qbo_run.assert_not_called()
            return result, Path(result[0]["provider_raw_response_path"]).exists()

        result, raw_response_exists = self._run_with_raw_file(scenario)
        self.assertEqual(len(result), 1)
        saved = result[0]
        self.assertEqual(saved["provider_name"], "google")
        self.assertEqual(saved["extraction_method"], "provider_parser")
        self.assertEqual(saved["source_file"], str(Path(saved["source_file"]).resolve()))
        self.assertEqual(saved["all_source_files"], [saved["source_file"]])
        self.assertTrue(raw_response_exists)
        self.assertTrue(saved["needs_review"])

    def test_google_no_claude_never_enters_claude_path(self):
        def scenario(base, invoice):
            with patch("extraction.ai_extractor._parse_google_provider_group", return_value=google_invoice_result()), \
                 patch("extraction.ai_extractor._load_api_key") as load_api_key, \
                 patch("extraction.ai_extractor._classify_bill") as classify_bill, \
                 patch("extraction.ai_extractor._extract_group") as extract_group, \
                 patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client:
                result = ai_extractor.run(assume_yes=True, provider_name="google", no_claude=True)
            load_api_key.assert_not_called()
            classify_bill.assert_not_called()
            extract_group.assert_not_called()
            anthropic_client.assert_not_called()
            return result

        result = self._run_with_raw_file(scenario)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["provider_name"], "google")

    def test_google_no_claude_ignores_stale_claude_prompt_and_regenerates_outputs(self):
        def scenario(base, invoice):
            processed = base / "data/processed"
            extracted = base / "data/extracted"
            processed.mkdir(parents=True, exist_ok=True)
            extracted.mkdir(parents=True, exist_ok=True)
            (processed / "ai_extracted.json").write_text(json.dumps({"version": 1, "files": [invoice.name]}))
            old = extracted / f"{invoice.name}.extracted.json"
            old.write_text('{"provider_name": "claude", "stale": true}')
            stdout = StringIO()
            with patch("extraction.ai_extractor._parse_google_provider_group", return_value=google_invoice_result()), \
                 patch("extraction.ai_extractor._load_api_key") as load_api_key, \
                 patch("extraction.ai_extractor._classify_bill") as classify_bill, \
                 patch("extraction.ai_extractor._extract_group") as extract_group, \
                 patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client, \
                 patch("sys.stdin.isatty", return_value=True), \
                 patch("builtins.input", side_effect=AssertionError("Google extraction must not prompt for Claude")), \
                 redirect_stdout(stdout):
                result = ai_extractor.run(provider_name="google", no_claude=True)
            load_api_key.assert_not_called()
            classify_bill.assert_not_called()
            extract_group.assert_not_called()
            anthropic_client.assert_not_called()
            self.assertNotIn("Claude", stdout.getvalue())
            archive_dir = extracted / "archive_version_1"
            archived = (archive_dir / "invoice.pdf.extracted.json").exists()
            replaced_with_google = old.exists() and json.loads(old.read_text()).get("provider_name") == "google"
            return result, archived, replaced_with_google

        result, archived, replaced_with_google = self._run_with_raw_file(scenario)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["provider_name"], "google")
        self.assertTrue(archived)
        self.assertTrue(replaced_with_google)

    def test_google_no_claude_failed_validation_routes_to_review_not_claude_required(self):
        def scenario(base, invoice):
            low = google_invoice_result(confidence_by_field={"total_amount": 0.5})
            with patch("extraction.ai_extractor._parse_google_provider_group", return_value=low), \
                 patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client:
                result = ai_extractor.run(assume_yes=True, provider_name="google", no_claude=True)
            anthropic_client.assert_not_called()
            return result, base / "data/processed/claude_required.json"

        result, claude_required = self._run_with_raw_file(scenario)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["provider_validation_status"], "rejected")
        self.assertTrue(result[0]["needs_review"])
        self.assertFalse(claude_required.exists())

    def test_claude_stale_confirmation_workflow_is_unchanged(self):
        stale = {
            "stale": True,
            "old_version": 1,
            "current_version": 15,
            "files": ["invoice.pdf"],
            "reason": "extraction ledger version 1 != current schema 15",
        }
        with patch("sys.stdin.isatty", return_value=True), \
             patch("builtins.input", return_value="y") as prompt:
            confirmed = ai_extractor._confirm_stale_reprocessing(
                stale,
                [[Path("invoice.pdf")]],
                [],
                False,
                provider_name="claude",
            )
        self.assertTrue(confirmed)
        self.assertIn("Proceed with live Claude re-extraction?", prompt.call_args.args[0])

    def test_low_confidence_google_result_routes_to_claude_required_without_claude_by_default(self):
        def scenario(base, invoice):
            low = google_invoice_result(confidence_by_field={"total_amount": 0.5})
            with patch("extraction.ai_extractor._parse_google_provider_group", return_value=low), \
                 patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client:
                result = ai_extractor.run(assume_yes=True, provider_name="google")
            report = json.loads((base / "data/processed/claude_required.json").read_text())
            anthropic_client.assert_not_called()
            return result, report

        result, report = self._run_with_raw_file(scenario)
        self.assertEqual(result, [])
        self.assertEqual(report["count"], 1)
        self.assertIn("below threshold", report["items"][0]["reason"])

    def test_missing_total_google_result_routes_to_claude_required(self):
        def scenario(base, invoice):
            missing = google_invoice_result(fields={"total_amount": None})
            with patch("extraction.ai_extractor._parse_google_provider_group", return_value=missing), \
                 patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client:
                result = ai_extractor.run(assume_yes=True, provider_name="google")
            report = json.loads((base / "data/processed/claude_required.json").read_text())
            anthropic_client.assert_not_called()
            return result, report

        result, report = self._run_with_raw_file(scenario)
        self.assertEqual(result, [])
        self.assertIn("missing required field: total_amount", report["items"][0]["reason"])

    def test_claude_fallback_only_occurs_with_explicit_flag(self):
        def scenario(base, invoice):
            low = google_invoice_result(confidence_by_field={"total_amount": 0.5})
            claude_transaction = {
                "supplier_name": "Example Supplier LLC",
                "invoice_number": "INV-100",
                "invoice_date": "2026-06-01",
                "total_amount": 105.0,
                "tax_amount": 5.0,
                "line_items": [{"description": "Salon supplies", "amount": 105.0}],
                "primary_source_file": invoice.name,
            }
            with patch("extraction.ai_extractor._parse_google_provider_group", return_value=low), \
                 patch("extraction.ai_extractor._load_api_key", return_value="key"), \
                 patch("extraction.ai_extractor._classify_bill", return_value="other"), \
                 patch("extraction.ai_extractor._extract_group", return_value=[claude_transaction]) as extract_group, \
                 patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client:
                result = ai_extractor.run(
                    assume_yes=True,
                    provider_name="google",
                    allow_claude_fallback=True,
                )
            anthropic_client.assert_called_once()
            extract_group.assert_called_once()
            return result

        result = self._run_with_raw_file(scenario)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["supplier_name"], "Example Supplier LLC")

    def test_dry_run_shows_google_and_claude_cost_breakdown(self):
        def scenario(base, invoice):
            stdout = StringIO()
            with patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client, redirect_stdout(stdout):
                result = ai_extractor.run(dry_run=True, provider_name="google", max_docs=1)
            anthropic_client.assert_not_called()
            return result, stdout.getvalue()

        result, output = self._run_with_raw_file(scenario)
        self.assertEqual(result, [])
        self.assertIn("primary low-cost provider: google", output)
        self.assertIn("Google-routed document count: 1", output)
        self.assertIn("Google-accepted document count: 0", output)
        self.assertIn("Google-rejected/needs review count: 0", output)
        self.assertIn("Claude fallback required count: 1", output)
        self.assertIn("estimated cheap provider cost:", output)
        self.assertIn("estimated Claude fallback cost:", output)
        self.assertIn("estimated blended cost:", output)

    def test_google_no_claude_dry_run_routes_media_to_google_not_claude_required(self):
        def scenario(base, invoice):
            stdout = StringIO()
            with patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client, redirect_stdout(stdout):
                result = ai_extractor.run(dry_run=True, provider_name="google", no_claude=True, max_docs=1)
            anthropic_client.assert_not_called()
            return result, stdout.getvalue()

        result, output = self._run_with_raw_file(scenario)
        self.assertEqual(result, [])
        self.assertIn("primary low-cost provider: google", output)
        self.assertIn("Google-routed document count: 1", output)
        self.assertIn("provider document count: 1", output)
        self.assertIn("Claude fallback required count: 0", output)
        self.assertIn("Claude-required document count: 0", output)
        self.assertIn("Claude blocked document count: 0", output)
        self.assertIn("extraction_route: provider_parser", output)
        self.assertIn("estimated Claude fallback cost: AED 0.0000", output)
        self.assertNotIn("Provider parser unavailable because no primary provider is configured", output)

    def test_no_provider_no_claude_dry_run_still_routes_media_to_claude_required(self):
        def scenario(base, invoice):
            stdout = StringIO()
            with patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client, redirect_stdout(stdout):
                result = ai_extractor.run(dry_run=True, no_claude=True, max_docs=1)
            anthropic_client.assert_not_called()
            return result, stdout.getvalue()

        result, output = self._run_with_raw_file(scenario)
        self.assertEqual(result, [])
        self.assertIn("primary low-cost provider: not configured", output)
        self.assertIn("Claude-required document count: 1", output)
        self.assertIn("Claude blocked document count: 1", output)
        self.assertIn("extraction_route: claude_required", output)
        self.assertIn("Provider parser unavailable because no primary provider is configured", output)


if __name__ == "__main__":
    unittest.main()
