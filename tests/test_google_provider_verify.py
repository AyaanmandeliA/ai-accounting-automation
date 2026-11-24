import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from benchmarks.google_provider_verify import print_verification, verify
from document_processing.document_parser import load_google_provider_runtime_config, validate_provider_configuration


class FakeResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "name": "projects/example-project/locations/us/processors/0123456789abcdef",
            "displayName": "Invoice Parser",
        }


class GoogleProviderVerifyTests(unittest.TestCase):
    def runtime_config(self):
        return load_google_provider_runtime_config(
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

    def test_runtime_config_merges_env_without_making_google_primary(self):
        with patch.dict("os.environ", {
            "GOOGLE_DOCUMENT_AI_PROJECT_ID": "example-project",
            "GOOGLE_DOCUMENT_AI_LOCATION": "us",
            "GOOGLE_DOCUMENT_AI_PROCESSOR_ID": "0123456789abcdef",
        }, clear=True):
            config = load_google_provider_runtime_config(allow_api_calls=True, secrets_path="/does/not/exist.json")
        google = config["providers"]["google"]
        self.assertIsNone(config["primary_provider"])
        self.assertTrue(google["enabled"])
        self.assertEqual(google["project_id"], "example-project")
        self.assertEqual(google["location"], "us")
        self.assertEqual(google["processor_id"], "0123456789abcdef")

    def test_adc_counts_as_valid_credentials_for_configuration(self):
        config = self.runtime_config()
        with patch("document_processing.document_parser.google_adc_credentials_available", return_value=True):
            report = validate_provider_configuration("google", config=config, confirm_provider_api=True)
        self.assertTrue(report["ready"])

    def test_verify_google_provider_reports_all_successes_without_processing_invoices(self):
        result = verify(
            config=self.runtime_config(),
            adc_checker=lambda: True,
            token_loader=lambda settings: "token",
            http_get=lambda *args, **kwargs: FakeResponse(),
        )
        self.assertTrue(all(result["checks"].values()))
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            print_verification(result)
        output = stdout.getvalue()
        self.assertIn("✓ ADC authenticated", output)
        self.assertIn("✓ Project loaded: example-project", output)
        self.assertIn("✓ Region loaded: us", output)
        self.assertIn("✓ Processor loaded: 0123456789abcdef", output)
        self.assertIn("✓ Document AI reachable", output)
        self.assertIn("✓ Credentials valid", output)
        self.assertIn("no invoices processed", output)


if __name__ == "__main__":
    unittest.main()
