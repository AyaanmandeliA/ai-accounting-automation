"""
CE Accounting AI — Entry Point
Run: python main.py --status
"""

import argparse
import json
import logging
from pathlib import Path

from secrets_manager import load_secret_json

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "run.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


def load_config():
    config_dir = BASE_DIR / "config"
    creds = load_secret_json("app_config", config_dir / "credentials.json")
    qbo = load_secret_json("qbo_config", config_dir / "qbo_config.json")
    return creds, qbo


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def print_status() -> None:
    data_dir = BASE_DIR / "data"
    processed_dir = data_dir / "processed"
    raw_count = len(list((data_dir / "raw").glob("*"))) if (data_dir / "raw").exists() else 0
    extracted_count = len(list((data_dir / "extracted").glob("*.json"))) if (data_dir / "extracted").exists() else 0
    review_count = len(list((data_dir / "review").rglob("*"))) if (data_dir / "review").exists() else 0

    ai_ledger = _read_json(processed_dir / "ai_extracted.json", {})
    qbo_ledger = _read_json(processed_dir / "qbo_posted.json", {})
    review_decisions = _read_json(processed_dir / "review_decisions.json", {})
    extracted_dir = data_dir / "extracted"
    required_schema = {"accounting_treatment", "qbo_posting_type", "brand", "branch", "allocation_type", "overall_confidence"}
    stale_schema_count = 0
    extracted_names = set()
    if extracted_dir.exists():
        for path in extracted_dir.glob("*.json"):
            extracted_names.add(path.name)
            data = _read_json(path, {})
            if not required_schema.issubset(data):
                stale_schema_count += 1

    posted = qbo_ledger.get("posted", {})
    orphaned_posted = [name for name in posted if name not in extracted_names]
    approved = sum(1 for status in review_decisions.values() if status == "approved")
    review_routed = sum(1 for status in review_decisions.values() if status in {"needs_review", "manual_check", "skipped"})
    pending = max(0, extracted_count - len(review_decisions))
    current_extraction_version = 15

    print("\nCE Accounting AI status")
    print(f"  raw files: {raw_count}")
    print(f"  extracted JSON files: {extracted_count}")
    print(f"  review folder items: {review_count}")
    print(f"  extraction ledger version: {ai_ledger.get('version', 'none')}")
    if ai_ledger.get("version") != current_extraction_version:
        print(f"  extraction version mismatch: ledger {ai_ledger.get('version', 'none')} -> current {current_extraction_version}")
    print(f"  extraction ledger files: {len(ai_ledger.get('files', []))}")
    print(f"  stale extracted schema files: {stale_schema_count}")
    print(f"  review decisions: {len(review_decisions)} ({approved} approved, {review_routed} review/manual, {pending} pending)")
    print(f"  QBO posted ledger entries: {len(qbo_ledger.get('posted', {}))}")
    print(f"  QBO posted orphaned filename entries: {len(orphaned_posted)}")
    if extracted_count and not approved:
        print("  warning: extracted files exist but review_decisions.json has no approvals; review will show items as pending.")
    print("\nSafe modes:")
    print("  --ingest       live Gmail/Drive ingestion")
    print("  --parse        local spreadsheet parsing")
    print("  --extract      live Claude extraction")
    print("  --review       local human review")
    print("  --review-dashboard local web review dashboard; localhost only")
    print("  --qbo-auth-only connect QBO OAuth only; does not post")
    print("  --post         live QBO posting; also requires --confirm-live-posting")
    print("  --dry-run      do not call live services or post")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CE Accounting AI local pipeline runner")
    parser.add_argument(
        "command",
        nargs="?",
        choices=[
            "archive_old_raw_data",
            "archive_old_processed_data",
            "reset_review_queue",
            "reset_test_data",
            "reset_sandbox_data",
            "benchmark_providers",
            "benchmark_google",
            "verify_google_provider",
        ],
        help="Local archive/reset utility command. Archives first; never posts or calls live APIs.",
    )
    parser.add_argument("--status", action="store_true", help="Show local pipeline status only")
    parser.add_argument("--ingest", action="store_true", help="Run live Gmail and Drive ingestion")
    parser.add_argument("--parse", action="store_true", help="Parse local spreadsheets from data/raw")
    parser.add_argument("--extract", action="store_true", help="Run live Claude extraction")
    parser.add_argument("--no-claude", action="store_true", help="Run extraction without Claude; queue Claude-required documents for review")
    parser.add_argument("--low-cost-extract", action="store_true", help="Run provider-first low-cost extraction planning/execution")
    parser.add_argument("--provider", help="Provider name for benchmark planning or extraction, e.g. google")
    parser.add_argument("--allow-claude-fallback", action="store_true", help="Allow Claude fallback after an explicit provider extraction failure")
    parser.add_argument("--confirm-provider-api", action="store_true", help="Explicitly allow confirmed provider benchmark API calls")
    parser.add_argument("--max-docs", type=int, help="Limit extraction planning/run to the first N document groups")
    parser.add_argument("--max-cost-aed", type=float, help="Block extraction when planned cost exceeds this AED amount")
    parser.add_argument("--review", action="store_true", help="Run local human review")
    parser.add_argument("--review-dashboard", action="store_true", help="Run local localhost-only web review dashboard")
    parser.add_argument("--review-dashboard-port", type=int, default=8765, help="Local review dashboard port")
    parser.add_argument("--reviewer", help="Reviewer name to stamp on review audit decisions")
    parser.add_argument("--qbo-auth-only", action="store_true", help="Connect/refresh QBO OAuth tokens only; never posts")
    parser.add_argument("--post", action="store_true", help="Post approved bills to QBO; explicit live action")
    parser.add_argument("--confirm-live-posting", action="store_true", help="Required second key for live QBO posting")
    parser.add_argument("--allow-vendor-create", action="store_true", help="Allow QBO vendor creation during confirmed live posting")
    parser.add_argument("--dry-run", action="store_true", help="Plan actions without live API calls or QBO posting")
    parser.add_argument("--confirm", action="store_true", help="Confirm an archive/reset command")
    parser.add_argument("--confirm-high-cost", action="store_true", help="Allow extraction when estimated run cost exceeds the configured cap")
    parser.add_argument("--yes", action="store_true", help="Confirm intentional stale re-extraction when running --extract")
    parser.add_argument("--all-local", action="store_true", help="Run local parse and review only")
    parser.add_argument("--single-document", help="Run one local PDF or extracted JSON through the standard preview workflow")
    parser.add_argument("--save-single-document", action="store_true", help="Save the single-document extracted JSON preview")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.command:
        if args.command == "benchmark_providers":
            from benchmarks.provider_benchmark_plan import run as benchmark_plan_run

            benchmark_plan_run(
                dry_run=args.dry_run,
                provider=args.provider,
                max_docs=args.max_docs,
                confirm_provider_api=args.confirm_provider_api,
            )
            return
        if args.command == "benchmark_google":
            from benchmarks.google_document_ai_benchmark import run as benchmark_google_run

            benchmark_google_run(
                dry_run=args.dry_run,
                max_docs=args.max_docs,
                confirm_provider_api=args.confirm_provider_api,
            )
            return
        if args.command == "verify_google_provider":
            from benchmarks.google_provider_verify import run as verify_google_provider_run

            verify_google_provider_run()
            return
        from tools.maintenance import run_command

        run_command(args.command, dry_run=args.dry_run, confirm=args.confirm)
        return

    if args.low_cost_extract:
        args.extract = True

    if args.review_dashboard:
        from review_dashboard import serve

        serve(port=args.review_dashboard_port)
        return

    if args.single_document:
        from workflows.single_document import build_single_document_preview

        preview = build_single_document_preview(args.single_document, save=args.save_single_document)
        decision = preview.get("accounting_decision") or {}
        review = preview.get("review") or {}
        posting = preview.get("posting_preview") or {}
        print("\nSingle-document preview")
        print(f"  source: {preview.get('source_file')}")
        if preview.get("extracted_file"):
            print(f"  saved extraction: {preview.get('extracted_file')}")
        print(f"  decision: {decision.get('posting_type')} ({decision.get('qbo_posting_type')})")
        print(f"  review: {'Needs Review' if review.get('needs_review') else 'Ready for review approval'}")
        if review.get("review_reason"):
            print(f"  review reason: {review.get('review_reason')}")
        print(f"  dry-run entity: {posting.get('qbo_api_entity')}")
        print(f"  live blocked: {posting.get('blocked_from_live_posting')}")
        if posting.get("validation_errors"):
            print(f"  validation errors: {len(posting.get('validation_errors'))}")
        return

    if args.status or not any((args.ingest, args.parse, args.extract, args.review, args.review_dashboard, args.qbo_auth_only, args.post, args.all_local)):
        print_status()
        return

    log.info("CE Accounting AI starting...")

    if args.dry_run:
        log.info("Dry run selected. No live Gmail/Drive/Claude/QBO actions will run.")
        if args.extract:
            from extraction.ai_extractor import run as extract_run
            extract_run(
                dry_run=True,
                max_docs=args.max_docs,
                max_cost_aed=args.max_cost_aed,
                confirm_high_cost=args.confirm_high_cost,
                no_claude=args.no_claude,
                low_cost_extract=args.low_cost_extract,
                provider_name=args.provider,
                allow_claude_fallback=args.allow_claude_fallback,
            )
            return
        if args.qbo_auth_only:
            from qbo.qbo_poster import auth_target

            target = auth_target()
            print("\nQBO auth dry run")
            print(f"  environment: {target.get('environment')}")
            print(f"  company_id: {target.get('company_id')}")
            print(f"  redirect_uri: {target.get('redirect_uri')}")
            print(f"  token file: {target.get('token_file')}")
            print("  no OAuth or QBO API calls made")
            return
        print_status()
        return

    if args.post and not args.confirm_live_posting:
        log.error("Refusing QBO posting. Add --confirm-live-posting for intentional live posting.")
        return

    if args.ingest or args.extract or args.post:
        creds, qbo = load_config()

        # Phase 0 check — make sure config is filled in before live API actions.
        placeholders = [v for v in list(creds.values()) + list(qbo.values()) if "FILL_IN" in str(v)]
        if placeholders:
            log.error(f"Config incomplete — {len(placeholders)} placeholder(s) still need filling.")
            log.error("Complete Phase 0 setup before running live actions.")
            return

    log.info("Config OK. Starting selected safe mode(s)...")

    extracted = None
    approved = None

    if args.ingest:
        from ingestion.gmail_ingest import run as gmail_run
        from ingestion.drive_ingest import run as drive_run

        log.info("--- Phase 1: Live Ingest ---")
        gmail_files = gmail_run()
        drive_files = drive_run()
        log.info("Ingested %d Gmail + %d Drive files", len(gmail_files), len(drive_files))

    if args.parse or args.all_local:
        from ingestion.excel_parser import run as excel_run

        log.info("--- Local Parse ---")
        parsed = excel_run()
        log.info("Parsed %d spreadsheet(s)", len(parsed))

    if args.extract:
        from extraction.ai_extractor import run as extract_run

        log.info("--- Phase 2: Live AI Extraction ---")
        extracted = extract_run(
            assume_yes=args.yes,
            max_docs=args.max_docs,
            max_cost_aed=args.max_cost_aed,
            confirm_high_cost=args.confirm_high_cost,
            no_claude=args.no_claude,
            low_cost_extract=args.low_cost_extract,
            provider_name=args.provider,
            allow_claude_fallback=args.allow_claude_fallback,
        )
        log.info("Extracted %d document(s)", len(extracted))

    if args.review or args.all_local:
        from review import run as review_run

        log.info("--- Phase 3: Human Review ---")
        approved = review_run(extracted, reviewer_name=args.reviewer)
        log.info("Approved %d transaction(s) for QuickBooks", len(approved))

    if args.qbo_auth_only:
        from qbo.qbo_poster import authorize_only

        log.info("--- QBO OAuth Only: no posting will run ---")
        auth = authorize_only()
        print("\nQBO OAuth complete")
        print(f"  environment: {auth.get('environment')}")
        print(f"  company_id: {auth.get('company_id')}")
        print(f"  realm_id: {auth.get('realm_id')}")
        print(f"  token file: {auth.get('token_file')}")
        return

    if args.post:
        from qbo.qbo_poster import run as post_run

        log.info("--- Phase 4: Explicit QBO Posting ---")
        posted = post_run(approved, allow_vendor_create=args.allow_vendor_create)
        log.info("Posted %d bill(s) to QuickBooks", len(posted))
    else:
        log.info("QBO posting not requested. Use --post explicitly to post approved bills.")

    log.info("Pipeline complete.")


if __name__ == "__main__":
    main()
