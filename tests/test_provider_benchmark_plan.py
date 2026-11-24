import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks import provider_benchmark_plan


class ProviderBenchmarkPlanTests(unittest.TestCase):
    def test_benchmark_plan_caps_documents_at_twenty(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            raw.mkdir()
            for index in range(25):
                (raw / f"invoice_{index:02d}.pdf").write_text("fake")
            with patch.object(provider_benchmark_plan, "RAW_DIR", raw):
                plan = provider_benchmark_plan.build_benchmark_plan(
                    provider="google",
                    max_docs=50,
                    config={"providers": {"google": {"estimated_cost_aed_per_page": 0.04}}},
                )
        self.assertEqual(plan["document_count"], 20)
        self.assertEqual(len(plan["documents"]), 20)
        self.assertEqual(plan["max_live_benchmark_docs"], 20)

    def test_confirmed_benchmark_stops_before_adapter_when_config_disabled(self):
        class ExplodingAdapter:
            def __init__(self):
                raise AssertionError("adapter should not be constructed")

        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            raw.mkdir()
            (raw / "invoice.pdf").write_text("fake")
            config = {
                "providers": {
                    "google": {
                        "enabled": False,
                        "api_calls_enabled": False,
                        "requires_credentials": True,
                        "estimated_cost_aed_per_page": 0.04,
                    }
                }
            }
            with patch.object(provider_benchmark_plan, "RAW_DIR", raw), \
                 patch.dict(provider_benchmark_plan.PROVIDER_ADAPTERS, {"google": ExplodingAdapter}):
                plan = provider_benchmark_plan.build_benchmark_plan(provider="google", max_docs=20, config=config)
                result = provider_benchmark_plan.run_confirmed_provider_benchmark(plan, config=config)

        report = result["providers"]["google"]
        self.assertFalse(report["ready"])
        self.assertFalse(report["api_call_attempted"])
        self.assertEqual(report["result_files"], [])
        self.assertIn("google provider is disabled in config.", report["errors"])


if __name__ == "__main__":
    unittest.main()
