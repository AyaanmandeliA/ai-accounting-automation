import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks import google_document_ai_benchmark as benchmark
from document_processing import document_parser
from document_processing.document_parser import normalized_provider_result


class ExplodingAdapter:
    def parse(self, *args, **kwargs):
        raise AssertionError("Google adapter should not run")


class FakeGoogleAdapter:
    def parse(self, file_path, *, text_preview=None, config=None, confirm_provider_api=False):
        return normalized_provider_result(
            provider="google",
            document_type="invoice",
            raw_text="Example Supplier LLC Tax Invoice INV-100 Total AED 105.00",
            fields={
                "supplier_name": "Example Supplier LLC",
                "invoice_number": "INV-100",
                "invoice_date": "2026-06-01",
                "subtotal_amount": 100.0,
                "tax_amount": 5.0,
                "total_amount": 105.0,
                "supplier_trn": "100000000000001",
            },
            line_items=[{"description": "Salon supplies", "amount": 105.0}],
            confidence_by_field={
                "supplier_name": 0.98,
                "invoice_number": 0.97,
                "invoice_date": 0.96,
                "total_amount": 0.99,
            },
            page_count=1,
            raw_provider_response={
                "provider_response": {"document": {"text": "raw google"}},
                "cost_estimate": {"estimated_cost_aed": 0.04},
            },
        )


class GoogleDocumentAIBenchmarkTests(unittest.TestCase):
    def test_dry_run_selects_documents_and_makes_no_google_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            raw.mkdir()
            for index in range(25):
                (raw / f"invoice_{index:02d}.pdf").write_text("fake")
            with patch.object(benchmark, "RAW_DIR", raw), \
                 patch.object(document_parser, "GOOGLE_PROVIDER_SECRETS_FILE", Path(tmp) / "missing.json"), \
                 patch("document_processing.document_parser.google_adc_credentials_available", return_value=False):
                result = benchmark.run(dry_run=True, max_docs=20, confirm_provider_api=False, adapter=ExplodingAdapter())

        self.assertEqual(result["plan"]["document_count"], 20)
        self.assertIsNone(result["live_result"])

    def test_missing_credentials_blocks_safely_before_adapter(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {
            "GOOGLE_DOCUMENT_AI_PROJECT_ID": "project-id",
            "GOOGLE_DOCUMENT_AI_LOCATION": "us",
            "GOOGLE_DOCUMENT_AI_PROCESSOR_ID": "processor-id",
        }, clear=True):
            raw = Path(tmp) / "raw"
            raw.mkdir()
            (raw / "invoice.pdf").write_text("fake")
            with patch.object(benchmark, "RAW_DIR", raw), \
                 patch("document_processing.document_parser.google_adc_credentials_available", return_value=False), \
                 patch.object(benchmark, "RESULTS_ROOT", Path(tmp) / "results"), \
                 patch.object(document_parser, "GOOGLE_PROVIDER_SECRETS_FILE", Path(tmp) / "missing.json"):
                result = benchmark.run(dry_run=False, max_docs=20, confirm_provider_api=True, adapter=ExplodingAdapter())

        self.assertTrue(result["live_result"]["blocked"])
        self.assertFalse(result["live_result"]["api_calls_made"])
        self.assertIn("google provider credentials not present", " ".join(result["live_result"]["errors"]))

    def test_live_benchmark_requires_confirmation_flag(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {
            "GOOGLE_DOCUMENT_AI_PROJECT_ID": "project-id",
            "GOOGLE_DOCUMENT_AI_LOCATION": "us",
            "GOOGLE_DOCUMENT_AI_PROCESSOR_ID": "processor-id",
            "GOOGLE_DOCUMENT_AI_BEARER_TOKEN": "token",
        }, clear=True):
            raw = Path(tmp) / "raw"
            raw.mkdir()
            (raw / "invoice.pdf").write_text("fake")
            with patch.object(benchmark, "RAW_DIR", raw), \
                 patch.object(document_parser, "GOOGLE_PROVIDER_SECRETS_FILE", Path(tmp) / "missing.json"):
                result = benchmark.run(dry_run=False, max_docs=20, confirm_provider_api=False, adapter=ExplodingAdapter())

        self.assertTrue(result["live_result"]["blocked"])
        self.assertIn("Live benchmark requires --confirm-provider-api.", result["live_result"]["errors"])

    def test_live_benchmark_writes_required_artifacts_and_normalized_fields(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {
            "GOOGLE_DOCUMENT_AI_PROJECT_ID": "project-id",
            "GOOGLE_DOCUMENT_AI_LOCATION": "us",
            "GOOGLE_DOCUMENT_AI_PROCESSOR_ID": "processor-id",
            "GOOGLE_DOCUMENT_AI_BEARER_TOKEN": "token",
        }, clear=True):
            raw = Path(tmp) / "raw"
            raw.mkdir()
            (raw / "invoice.pdf").write_text("fake")
            results = Path(tmp) / "results"
            with patch.object(benchmark, "RAW_DIR", raw), \
                 patch.object(benchmark, "RESULTS_ROOT", results), \
                 patch.object(document_parser, "GOOGLE_PROVIDER_SECRETS_FILE", Path(tmp) / "missing.json"):
                result = benchmark.run(
                    dry_run=False,
                    max_docs=20,
                    confirm_provider_api=True,
                    adapter=FakeGoogleAdapter(),
                )

            output_dir = Path(result["live_result"]["results_dir"])
            normalized_path = next(output_dir.glob("*.normalized.json"))
            raw_path = next(output_dir.glob("*.raw_google_response.json"))
            summary_path = output_dir / "summary.csv"
            errors_path = output_dir / "errors.jsonl"
            normalized = json.loads(normalized_path.read_text())
            with summary_path.open() as handle:
                summary_rows = list(csv.DictReader(handle))

            self.assertTrue(raw_path.exists())
            self.assertTrue(errors_path.exists())
            self.assertEqual(normalized["provider"], "google")
            for key in ("supplier_name", "invoice_number", "invoice_date", "subtotal_amount", "tax_amount", "total_amount", "supplier_trn"):
                self.assertIn(key, normalized["fields"])
        self.assertEqual(summary_rows[0]["supplier"], "Example Supplier LLC")
        self.assertEqual(summary_rows[0]["invoice_number"], "INV-100")
        self.assertEqual(summary_rows[0]["total"], "105.0")

    def test_benchmark_reuses_shared_verified_provider_configuration(self):
        config = document_parser.load_google_provider_runtime_config(
            allow_api_calls=True,
            config={
                "primary_provider": None,
                "providers": {
                    "google": {
                        "project_id": "example-project",
                        "location": "us",
                        "processor_id": "0123456789abcdef",
                    }
                },
            },
            secrets_path="/does/not/exist.json",
        )
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(benchmark, "RAW_DIR", Path(tmp)), \
             patch("benchmarks.google_document_ai_benchmark.load_google_provider_runtime_config", return_value=config), \
             patch("document_processing.document_parser.google_adc_credentials_available", return_value=True):
            plan = benchmark.build_plan(max_docs=20, allow_api_calls=True)
        report = document_parser.validate_provider_configuration("google", config=plan["config"], confirm_provider_api=True)
        self.assertTrue(report["ready"])
        self.assertEqual(plan["config"]["providers"]["google"]["project_id"], "example-project")

    def test_benchmark_has_no_duplicate_google_configuration_path(self):
        source = Path("benchmarks/google_document_ai_benchmark.py").read_text()
        self.assertNotIn("def load_google_benchmark_config", source)
        self.assertNotIn("GOOGLE_DOCUMENT_AI_PROJECT_ID", source)
        self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", source)
        self.assertNotIn("SECRETS_FILE", source)


if __name__ == "__main__":
    unittest.main()
