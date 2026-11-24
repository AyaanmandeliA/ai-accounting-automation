import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from document_processing.document_parser import (
    DEFAULT_PROVIDER_CONFIG,
    GoogleDocumentAIParserProvider,
    MockDocumentParserProvider,
    ProviderApiConfirmationRequired,
    get_enabled_provider_names,
    get_primary_provider_name,
    load_provider_config,
    normalized_provider_result,
    validate_provider_configuration,
    provider_result_to_transaction,
)


class DocumentParserProviderTests(unittest.TestCase):
    def provider_result(self) -> dict:
        return normalized_provider_result(
            provider="mock",
            document_type="invoice",
            raw_text="Example Supplier LLC tax invoice INV-100 total AED 105.00",
            fields={
                "supplier_name": "Example Supplier LLC",
                "invoice_number": "INV-100",
                "invoice_date": "2026-06-01",
                "subtotal_amount": 100.0,
                "tax_amount": 5.0,
                "total_amount": 105.0,
                "supplier_trn": "100000000000001",
                "payment_confirmed": False,
            },
            line_items=[
                {
                    "description": "Salon supplies",
                    "amount": 105.0,
                }
            ],
            confidence_by_field={
                "supplier_name": 0.96,
                "invoice_number": 0.97,
                "invoice_date": 0.94,
                "subtotal_amount": 0.95,
                "tax_amount": 0.93,
                "total_amount": 0.98,
                "supplier_trn": 0.9,
            },
            page_count=1,
            raw_provider_response={"fixture": True},
        )

    def test_normalized_provider_result_has_exact_required_shape(self):
        result = normalized_provider_result(provider="mock")
        self.assertEqual(
            list(result.keys()),
            [
                "provider",
                "document_type",
                "raw_text",
                "fields",
                "line_items",
                "confidence_by_field",
                "page_count",
                "raw_provider_response",
                "needs_llm_normalization",
                "errors",
            ],
        )
        self.assertEqual(result["provider"], "mock")
        self.assertEqual(result["fields"], {})
        self.assertEqual(result["line_items"], [])

    def test_mock_provider_returns_deterministic_fixture_without_network_clients(self):
        fixture = self.provider_result()
        provider = MockDocumentParserProvider(fixtures={"invoice.pdf": fixture})
        with patch("socket.socket", side_effect=AssertionError("network should not be used")):
            result = provider.parse("invoice.pdf")
        self.assertEqual(result["provider"], "mock")
        self.assertEqual(result["fields"]["invoice_number"], "INV-100")
        self.assertEqual(provider.estimate_cost(2)["estimated_cost_aed"], 0.0)

    def test_provider_config_loads_defaults_and_safe_repo_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            self.assertEqual(load_provider_config(missing), DEFAULT_PROVIDER_CONFIG)
            custom = Path(tmp) / "providers.json"
            custom.write_text(json.dumps({"enabled_providers": ["mock", "local"], "primary_provider": None}))
            config = load_provider_config(custom)
        self.assertEqual(get_enabled_provider_names(config), ["mock", "local"])
        self.assertIsNone(get_primary_provider_name(config))

        repo_config = load_provider_config()
        self.assertEqual(repo_config["default_strategy"], "benchmark_first")
        self.assertEqual(repo_config["enabled_providers"], ["mock"])
        self.assertIsNone(repo_config["primary_provider"])
        self.assertTrue(repo_config["require_benchmark_before_primary_provider"])

    def test_provider_result_converts_to_existing_transaction_schema(self):
        transaction = provider_result_to_transaction(
            self.provider_result(),
            source_file="invoice.pdf",
            folder_path_hint="/Sample Brand A/Branch North/Bills",
        )
        self.assertEqual(transaction["supplier_name"], "Example Supplier LLC")
        self.assertEqual(transaction["invoice_number"], "INV-100")
        self.assertEqual(transaction["total_amount"], 105.0)
        self.assertEqual(transaction["tax_amount"], 5.0)
        self.assertEqual(transaction["primary_source_file"], "invoice.pdf")
        self.assertEqual(transaction["extraction_method"], "provider_parser")
        self.assertEqual(transaction["provider_name"], "mock")
        self.assertTrue(transaction["needs_review"])
        self.assertIn("account mapping", transaction["review_reason"])
        self.assertIsNone(transaction["line_items"][0]["account_name"])

    def test_provider_transaction_passes_through_existing_save_flow(self):
        from extraction import ai_extractor

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            invoice = root / "invoice.pdf"
            invoice.write_text("fake local invoice")
            extracted_dir = root / "extracted"
            review_dir = root / "review"
            transaction = provider_result_to_transaction(
                self.provider_result(),
                source_file=invoice,
                folder_path_hint="/Sample Brand A/Branch North/Bills",
            )
            with patch("extraction.ai_extractor.EXTRACTED_DIR", extracted_dir), \
                 patch("allocation.review_router.REVIEW_ROOT", review_dir), \
                 patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client:
                out = ai_extractor._save_result(transaction, invoice, [invoice], index=0, total=1)
            saved = json.loads(out.read_text())

        anthropic_client.assert_not_called()
        self.assertEqual(saved["source_file"], str(invoice.resolve()))
        self.assertEqual(saved["all_source_files"], [str(invoice.resolve())])
        self.assertEqual(saved["primary_source_file"], invoice.name)
        self.assertEqual(saved["extraction_method"], "provider_parser")
        self.assertEqual(saved["provider_name"], "mock")
        self.assertTrue(saved["needs_review"])

    def test_new_provider_layer_has_no_real_provider_sdk_imports(self):
        parser_source = Path("document_processing/document_parser.py").read_text()
        self.assertNotIn("import azure", parser_source)
        self.assertNotIn("from azure", parser_source)
        self.assertNotIn("google.cloud", parser_source)
        self.assertNotIn("import boto3", parser_source)
        self.assertNotIn("import mistral", parser_source.lower())
        self.assertNotIn("from mistral", parser_source.lower())
        self.assertNotIn("import anthropic", parser_source.lower())

    def test_google_provider_refuses_live_parse_without_confirmation_before_transport(self):
        provider = GoogleDocumentAIParserProvider(http_post=lambda *args, **kwargs: self.fail("transport should not run"))
        with self.assertRaises(ProviderApiConfirmationRequired):
            provider.parse("invoice.pdf", config={"providers": {"google": {"enabled": True, "api_calls_enabled": True}}})

    def test_google_provider_validation_uses_enabled_repo_config_without_primary_default(self):
        config = load_provider_config()
        report = validate_provider_configuration("google", config=config, confirm_provider_api=True)
        self.assertTrue(config["providers"]["google"]["enabled"])
        self.assertTrue(config["providers"]["google"]["api_calls_enabled"])
        self.assertIsNone(get_primary_provider_name(config))
        self.assertNotIn("google provider is disabled in config.", report["errors"])
        self.assertNotIn("google provider API calls are disabled in config.", report["errors"])

    def test_google_provider_normalizes_document_ai_response_with_injected_transport(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "document": {
                        "text": "Example Supplier LLC invoice INV-100 total AED 105.00",
                        "pages": [{"pageNumber": 1}],
                        "entities": [
                            {"type": "supplier_name", "mentionText": "Example Supplier LLC", "confidence": 0.98},
                            {"type": "invoice_id", "mentionText": "INV-100", "confidence": 0.97},
                            {
                                "type": "invoice_date",
                                "normalizedValue": {"dateValue": {"year": 2026, "month": 6, "day": 1}},
                                "confidence": 0.96,
                            },
                            {
                                "type": "total_amount",
                                "normalizedValue": {"moneyValue": {"units": 105, "nanos": 0}},
                                "confidence": 0.99,
                            },
                            {
                                "type": "line_item",
                                "properties": [
                                    {"type": "line_item_description", "mentionText": "Salon supplies", "confidence": 0.9},
                                    {
                                        "type": "line_item_amount",
                                        "normalizedValue": {"moneyValue": {"units": 105, "nanos": 0}},
                                        "confidence": 0.9,
                                    },
                                ],
                            },
                        ],
                    }
                }

        calls = []

        def fake_post(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeResponse()

        config = {
            "providers": {
                "google": {
                    "enabled": True,
                    "api_calls_enabled": True,
                    "requires_credentials": True,
                    "estimated_cost_aed_per_page": 0.04,
                    "project_id": "project-id",
                    "location": "us",
                    "processor_id": "processor-id",
                    "bearer_token_env_var": "GOOGLE_DOCUMENT_AI_TEST_TOKEN",
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"GOOGLE_DOCUMENT_AI_TEST_TOKEN": "token"}, clear=False):
            invoice = Path(tmp) / "invoice.pdf"
            invoice.write_bytes(b"fake pdf")
            provider = GoogleDocumentAIParserProvider(http_post=fake_post)
            result = provider.parse(invoice, config=config, confirm_provider_api=True)

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["provider"], "google")
        self.assertEqual(result["fields"]["supplier_name"], "Example Supplier LLC")
        self.assertEqual(result["fields"]["invoice_number"], "INV-100")
        self.assertEqual(result["fields"]["invoice_date"], "2026-06-01")
        self.assertEqual(result["fields"]["total_amount"], 105.0)
        self.assertEqual(result["line_items"][0]["description"], "Salon supplies")
        self.assertEqual(result["line_items"][0]["amount"], 105.0)
        self.assertIn("cost_estimate", result["raw_provider_response"])


if __name__ == "__main__":
    unittest.main()
