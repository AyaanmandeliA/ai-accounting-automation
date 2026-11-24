import unittest
from unittest.mock import patch

import main


class MainCliTests(unittest.TestCase):
    def test_default_is_status_only(self):
        with patch("sys.argv", ["main.py"]), patch("main.print_status") as status:
            main.main()
        status.assert_called_once()

    def test_dry_run_does_not_import_live_actions(self):
        with patch("sys.argv", ["main.py", "--dry-run", "--post", "--allow-vendor-create"]), \
             patch("main.load_config", return_value=({"anthropic_api_key": "x"}, {"environment": "sandbox"})), \
             patch("main.print_status") as status:
            main.main()
        status.assert_called_once()

    def test_post_alone_requires_confirm_live_posting(self):
        with patch("sys.argv", ["main.py", "--post"]), \
             patch("main.load_config") as load_config:
            main.main()
        load_config.assert_not_called()

    def test_confirmed_post_passes_vendor_create_flag(self):
        with patch("sys.argv", ["main.py", "--post", "--confirm-live-posting", "--allow-vendor-create"]), \
             patch("main.load_config", return_value=({"anthropic_api_key": "x"}, {"environment": "sandbox"})), \
             patch("qbo.qbo_poster.run") as post_run:
            main.main()
        post_run.assert_called_once_with(None, allow_vendor_create=True)

    def test_extract_dry_run_calls_extraction_plan_only(self):
        with patch("sys.argv", ["main.py", "--dry-run", "--extract"]), \
             patch("extraction.ai_extractor.run") as extract_run:
            main.main()
        extract_run.assert_called_once_with(
            dry_run=True,
            max_docs=None,
            max_cost_aed=None,
            confirm_high_cost=False,
            no_claude=False,
            low_cost_extract=False,
            provider_name=None,
            allow_claude_fallback=False,
        )

    def test_extract_passes_max_docs_and_high_cost_confirmation(self):
        with patch("sys.argv", ["main.py", "--extract", "--max-docs", "10", "--max-cost-aed", "5", "--confirm-high-cost"]), \
             patch("main.load_config", return_value=({"anthropic_api_key": "x"}, {"environment": "sandbox"})), \
             patch("extraction.ai_extractor.run", return_value=[]) as extract_run:
            main.main()
        extract_run.assert_called_once_with(
            assume_yes=False,
            max_docs=10,
            max_cost_aed=5.0,
            confirm_high_cost=True,
            no_claude=False,
            low_cost_extract=False,
            provider_name=None,
            allow_claude_fallback=False,
        )

    def test_extract_passes_no_claude_flag(self):
        with patch("sys.argv", ["main.py", "--extract", "--no-claude"]), \
             patch("main.load_config", return_value=({"anthropic_api_key": "x"}, {"environment": "sandbox"})), \
             patch("extraction.ai_extractor.run", return_value=[]) as extract_run:
            main.main()
        extract_run.assert_called_once_with(
            assume_yes=False,
            max_docs=None,
            max_cost_aed=None,
            confirm_high_cost=False,
            no_claude=True,
            low_cost_extract=False,
            provider_name=None,
            allow_claude_fallback=False,
        )

    def test_extract_passes_low_cost_flag(self):
        with patch("sys.argv", ["main.py", "--low-cost-extract"]), \
             patch("main.load_config", return_value=({"anthropic_api_key": "x"}, {"environment": "sandbox"})), \
             patch("extraction.ai_extractor.run", return_value=[]) as extract_run:
            main.main()
        extract_run.assert_called_once_with(
            assume_yes=False,
            max_docs=None,
            max_cost_aed=None,
            confirm_high_cost=False,
            no_claude=False,
            low_cost_extract=True,
            provider_name=None,
            allow_claude_fallback=False,
        )

    def test_extract_passes_google_provider_and_claude_fallback_flag(self):
        with patch("sys.argv", ["main.py", "--extract", "--provider", "google", "--allow-claude-fallback"]), \
             patch("main.load_config", return_value=({"anthropic_api_key": "x"}, {"environment": "sandbox"})), \
             patch("extraction.ai_extractor.run", return_value=[]) as extract_run:
            main.main()
        extract_run.assert_called_once_with(
            assume_yes=False,
            max_docs=None,
            max_cost_aed=None,
            confirm_high_cost=False,
            no_claude=False,
            low_cost_extract=False,
            provider_name="google",
            allow_claude_fallback=True,
        )

    def test_qbo_auth_only_does_not_post(self):
        with patch("sys.argv", ["main.py", "--qbo-auth-only"]), \
             patch("qbo.qbo_poster.authorize_only", return_value={
                 "environment": "production",
                 "company_id": "1234567890123456",
                 "realm_id": "1234567890123456",
                 "token_file": "config/secrets.local/qbo_tokens.json",
             }) as authorize_only, \
             patch("qbo.qbo_poster.run") as post_run:
            main.main()
        authorize_only.assert_called_once()
        post_run.assert_not_called()

    def test_benchmark_providers_dry_run_uses_planner_only(self):
        with patch("sys.argv", ["main.py", "benchmark_providers", "--dry-run", "--provider", "azure", "--max-docs", "20"]), \
             patch("benchmarks.provider_benchmark_plan.run") as benchmark_run:
            main.main()
        benchmark_run.assert_called_once_with(
            dry_run=True,
            provider="azure",
            max_docs=20,
            confirm_provider_api=False,
        )

    def test_benchmark_provider_api_confirmation_does_not_call_live_adapter(self):
        with patch("sys.argv", ["main.py", "benchmark_providers", "--provider", "azure", "--confirm-provider-api"]), \
             patch("benchmarks.provider_benchmark_plan.run") as benchmark_run:
            main.main()
        benchmark_run.assert_called_once_with(
            dry_run=False,
            provider="azure",
            max_docs=None,
            confirm_provider_api=True,
        )

    def test_benchmark_google_routes_only_to_google_benchmark_runner(self):
        with patch("sys.argv", ["main.py", "benchmark_google", "--max-docs", "20", "--dry-run"]), \
             patch("main.load_config") as load_config, \
             patch("benchmarks.google_document_ai_benchmark.run") as benchmark_run:
            main.main()
        benchmark_run.assert_called_once_with(
            dry_run=True,
            max_docs=20,
            confirm_provider_api=False,
        )
        load_config.assert_not_called()

    def test_verify_google_provider_does_not_load_live_pipeline_config(self):
        with patch("sys.argv", ["main.py", "verify_google_provider"]), \
             patch("main.load_config") as load_config, \
             patch("benchmarks.google_provider_verify.run") as verify_run:
            main.main()
        verify_run.assert_called_once_with()
        load_config.assert_not_called()

    def test_qbo_auth_only_dry_run_does_not_authorize(self):
        with patch("sys.argv", ["main.py", "--qbo-auth-only", "--dry-run"]), \
             patch("qbo.qbo_poster.auth_target", return_value={
                 "environment": "production",
                 "company_id": "1234567890123456",
                 "redirect_uri": "http://localhost:8000/callback",
                 "token_file": "config/secrets.local/qbo_tokens.json",
             }) as auth_target, \
             patch("qbo.qbo_poster.authorize_only") as authorize_only:
            main.main()
        auth_target.assert_called_once()
        authorize_only.assert_not_called()

    def test_review_dashboard_starts_local_server_only(self):
        with patch("sys.argv", ["main.py", "--review-dashboard", "--review-dashboard-port", "8766"]), \
             patch("review_dashboard.serve") as serve, \
             patch("main.load_config") as load_config:
            main.main()
        serve.assert_called_once_with(port=8766)
        load_config.assert_not_called()

    def test_single_document_routes_to_local_preview(self):
        with patch("sys.argv", ["main.py", "--single-document", "/tmp/invoice.pdf"]), \
             patch("workflows.single_document.build_single_document_preview", return_value={
                 "source_file": "/tmp/invoice.pdf",
                 "extracted_file": None,
                 "accounting_decision": {"posting_type": "Bill", "qbo_posting_type": "Bill"},
                 "review": {"needs_review": True, "review_reason": "check"},
                 "posting_preview": {"qbo_api_entity": "Bill", "blocked_from_live_posting": True, "validation_errors": []},
             }) as preview:
            main.main()
        preview.assert_called_once_with("/tmp/invoice.pdf", save=False)

    def test_maintenance_command_does_not_load_live_config(self):
        with patch("sys.argv", ["main.py", "reset_review_queue", "--dry-run"]), \
             patch("main.load_config") as load_config, \
             patch("tools.maintenance.run_command") as run_command:
            main.main()
        load_config.assert_not_called()
        run_command.assert_called_once_with("reset_review_queue", dry_run=True, confirm=False)


if __name__ == "__main__":
    unittest.main()
