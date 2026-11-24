import unittest
from pathlib import Path

from benchmarks.provider_benchmark import (
    fallback_required,
    render_markdown_table,
    run_benchmark,
    score_provider_output,
)
from document_processing.document_parser import normalized_provider_result


def expected_invoice() -> dict:
    return {
        "supplier_name": "Example Supplier LLC",
        "invoice_number": "INV-100",
        "invoice_date": "2026-06-01",
        "subtotal_amount": 100.0,
        "tax_amount": 5.0,
        "total_amount": 105.0,
        "supplier_trn": "100000000000001",
        "document_type": "invoice",
        "payment_confirmed": False,
        "required_fields": ["supplier_name", "invoice_number", "invoice_date", "total_amount"],
        "line_items": [{"description": "Salon supplies", "amount": 105.0}],
    }


def provider_output(**overrides) -> dict:
    fields = {
        "supplier_name": "Example Supplier LLC",
        "invoice_number": "INV-100",
        "invoice_date": "2026-06-01",
        "subtotal_amount": 100.0,
        "tax_amount": 5.0,
        "total_amount": 105.0,
        "supplier_trn": "100000000000001",
        "payment_confirmed": False,
    }
    fields.update(overrides.pop("fields", {}))
    return normalized_provider_result(
        provider=overrides.pop("provider", "google"),
        document_type=overrides.pop("document_type", "invoice"),
        raw_text="Example Supplier LLC INV-100",
        fields=fields,
        line_items=overrides.pop("line_items", [{"description": "Salon supplies", "amount": 105.0}]),
        confidence_by_field=overrides.pop("confidence_by_field", {
            "supplier_name": 0.95,
            "invoice_number": 0.95,
            "invoice_date": 0.95,
            "subtotal_amount": 0.95,
            "tax_amount": 0.95,
            "total_amount": 0.95,
            "supplier_trn": 0.95,
        }),
        page_count=1,
        raw_provider_response=overrides.pop("raw_provider_response", {
            "cost_estimate": {"estimated_cost_aed": 0.04},
            "speed_estimate_ms": 2000,
        }),
        errors=overrides.pop("errors", []),
        **overrides,
    )


class ProviderBenchmarkTests(unittest.TestCase):
    def test_scores_correct_provider_output(self):
        scores = score_provider_output(provider_output(), expected_invoice())
        self.assertEqual(scores["overall_accuracy_score"], 1.0)
        self.assertFalse(scores["fallback_required"])
        self.assertEqual(scores["metrics"]["total"], 1.0)
        self.assertEqual(scores["cost_estimate"]["estimated_cost_aed"], 0.04)

    def test_scores_incorrect_missing_and_low_confidence_outputs(self):
        wrong = score_provider_output(
            provider_output(fields={"invoice_number": "WRONG"}),
            expected_invoice(),
        )
        self.assertEqual(wrong["metrics"]["invoice_number"], 0.0)
        self.assertEqual(wrong["metrics"]["confidence_quality"], 0.0)

        missing = score_provider_output(
            provider_output(fields={"total_amount": None}),
            expected_invoice(),
        )
        self.assertTrue(missing["fallback_required"])
        self.assertEqual(missing["metrics"]["total"], 0.0)

        low_confidence = score_provider_output(
            provider_output(confidence_by_field={
                "supplier_name": 0.7,
                "invoice_number": 0.7,
                "invoice_date": 0.7,
                "subtotal_amount": 0.7,
                "tax_amount": 0.7,
                "total_amount": 0.7,
                "supplier_trn": 0.7,
            }),
            expected_invoice(),
        )
        self.assertEqual(low_confidence["metrics"]["confidence_quality"], 0.0)

    def test_fallback_required_for_provider_errors_and_reconciliation_failure(self):
        self.assertTrue(fallback_required(provider_output(errors=["parse failed"]), expected_invoice()))
        self.assertTrue(fallback_required(provider_output(fields={"subtotal_amount": 100.0, "tax_amount": 10.0}), expected_invoice()))
        self.assertFalse(fallback_required(provider_output(), expected_invoice()))

    def test_benchmark_runner_compares_mocked_provider_outputs(self):
        manifest = {
            "providers": ["google", "local"],
            "samples": [
                {
                    "id": "invoice_001",
                    "document_path": "data/raw/invoice.pdf",
                    "expected": expected_invoice(),
                    "mock_provider_outputs": {
                        "google": provider_output(provider="google"),
                        "local": provider_output(
                            provider="local",
                            fields={"invoice_number": "WRONG"},
                            raw_provider_response={
                                "cost_estimate": {"estimated_cost_aed": 0.0},
                                "speed_estimate_ms": 500,
                            },
                        ),
                    },
                }
            ],
        }
        results = run_benchmark(manifest)
        self.assertEqual(results["summary"]["google"]["accuracy_score"], 1.0)
        self.assertLess(results["summary"]["local"]["accuracy_score"], 1.0)
        self.assertEqual(results["summary"]["google"]["estimated_cost_aed"], 0.04)
        table = render_markdown_table(results)
        self.assertIn("| Metric | Azure | Google | Mistral | Gemini | AWS | Local |", table)

    def test_example_manifest_runs_without_real_provider_apis(self):
        manifest_path = Path("benchmarks/sample_manifest.example.json")
        results = run_benchmark(manifest_path)
        self.assertIn("google", results["summary"])
        self.assertIn("local", results["summary"])
        self.assertGreaterEqual(results["summary"]["google"]["accuracy_score"], 0.9)

    def test_benchmark_module_has_no_real_provider_sdk_imports(self):
        source = Path("benchmarks/provider_benchmark.py").read_text()
        self.assertNotIn("import azure", source)
        self.assertNotIn("from azure", source)
        self.assertNotIn("google.cloud", source)
        self.assertNotIn("import boto3", source)
        self.assertNotIn("import mistral", source.lower())
        self.assertNotIn("from mistral", source.lower())
        self.assertNotIn("import anthropic", source.lower())


if __name__ == "__main__":
    unittest.main()
