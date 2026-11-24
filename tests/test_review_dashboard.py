import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import patch

import review_dashboard


class ReviewDashboardTests(unittest.TestCase):
    @contextmanager
    def dashboard_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            extracted = data / "extracted"
            processed = data / "processed"
            raw = data / "raw"
            config = root / "config"
            for path in (extracted, processed, raw, config):
                path.mkdir(parents=True, exist_ok=True)
            patches = [
                patch("review_dashboard.BASE_DIR", root),
                patch("review_dashboard.DATA_DIR", data),
                patch("review_dashboard.EXTRACTED_DIR", extracted),
                patch("review_dashboard.PROCESSED_DIR", processed),
                patch("review_dashboard.CONFIG_DIR", config),
                patch("review_dashboard.REVIEW_DECISIONS_FILE", processed / "review_decisions.json"),
                patch("review_dashboard.QBO_POSTED_FILE", processed / "qbo_posted.json"),
                patch("review_dashboard.DASHBOARD_INDEX_FILE", processed / "review_dashboard_index.json"),
                patch("review_dashboard.VENDOR_RULES_FILE", config / "vendor_rules.json"),
                patch("review_dashboard.VENDOR_BRANCH_RULES_FILE", config / "vendor_branch_rules.json"),
                patch("review_dashboard.ACCOUNT_RULES_FILE", config / "account_rules.json"),
                patch("review_dashboard.SUPPLIER_ACCOUNT_MAP_FILE", config / "supplier_account_map.json"),
                patch("ingestion.source_metadata.BASE_DIR", root),
                patch("ingestion.source_metadata.DATA_DIR", data),
                patch("ingestion.source_metadata.RAW_DIR", raw),
                patch("ingestion.source_metadata.PROCESSED_DIR", processed),
                patch("ingestion.source_metadata.SOURCE_METADATA_INDEX_FILE", processed / "source_metadata_index.json"),
                patch("allocation.rules.VENDOR_RULES_FILE", config / "vendor_rules.json"),
                patch("allocation.rules.VENDOR_BRANCH_RULES_FILE", config / "vendor_branch_rules.json"),
                patch("allocation.rules.ACCOUNT_RULES_FILE", config / "account_rules.json"),
                patch("allocation.branch_resolver.BUSINESS_UNITS_FILE", config / "business_units.json"),
                patch("allocation.branch_resolver.BRANCH_CLASS_MAP_FILE", config / "branch_class_map.json"),
                patch("accounting.rule_retrieval.VENDOR_RULES_FILE", config / "vendor_rules.json"),
                patch("accounting.rule_retrieval.VENDOR_BRANCH_RULES_FILE", config / "vendor_branch_rules.json"),
                patch("accounting.rule_retrieval.ACCOUNT_RULES_FILE", config / "account_rules.json"),
                patch("accounting.rule_retrieval.SUPPLIER_ACCOUNT_MAP_FILE", config / "supplier_account_map.json"),
                patch("accounting.decision_engine.VENDOR_RULES_FILE", config / "vendor_rules.json"),
                patch("review_dashboard.append_audit_event", side_effect=lambda event: event),
            ]
            with ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                self.write_config(config)
                yield root

    def write_config(self, config: Path):
        (config / "vendor_rules.json").write_text(json.dumps({"vendors": {}}))
        (config / "vendor_branch_rules.json").write_text(json.dumps({"vendors": {}}))
        (config / "account_rules.json").write_text(json.dumps({"vendors": {}, "keywords": {}}))
        (config / "supplier_account_map.json").write_text(json.dumps({
            "suppliers": {
                "Vendor LLC": {
                    "primary": "Salon Consumables",
                    "primary_treatment": "supplier_bill_unpaid",
                    "qbo_posting_type": "Bill",
                    "payment_method": "unpaid",
                    "confidence": "high",
                }
            }
        }))
        (config / "business_units.json").write_text(json.dumps({
            "brands": {
                "Sample Brand B": {
                    "aliases": ["sample brand b", "brand b"],
                    "branches": {"Branch South": ["branch south"], "Branch North": ["branch north", "north"]},
                },
                "Head Office": {"aliases": ["head office"], "branches": {}},
                "Shared": {"aliases": ["shared"], "branches": {}},
            }
        }))
        (config / "branch_class_map.json").write_text(json.dumps({
            "Sample Brand B/Branch South": {"class": "Brand B - Branch South", "location": "Branch South"},
            "Sample Brand B/Branch North": {"class": "Brand B - Branch North", "location": "Branch North"},
            "Head Office": {"class": "Head Office", "location": "Head Office"},
        }))

    def write_item(self, root: Path, name: str = "invoice.extracted.json", **overrides):
        source = root / "data" / "raw" / "invoice.pdf"
        source.write_bytes(b"%PDF-1.4\n")
        item = {
            "supplier_name": "Vendor LLC",
            "invoice_number": "INV-1",
            "invoice_date": "2026-06-01",
            "due_date": "2026-06-30",
            "total_amount": 105,
            "tax_amount": 5,
            "currency": "AED",
            "document_type": "invoice",
            "document_category": "Supplier Bill",
            "posting_action": "Create Bill",
            "qbo_object": "Bill",
            "qbo_posting_type": "Bill",
            "payment_status": "Unpaid",
            "payment_method": "Unknown",
            "brand": "Sample Brand A",
            "business_unit": "Sample Brand A",
            "branch": "Branch North",
            "qbo_class_name": "Brand A - Branch North",
            "qbo_location_name": "Branch North",
            "overall_confidence": 0.97,
            "line_items": [{"description": "Supplies", "amount": 105, "account_name": "Repairs"}],
            "source_file": str(source),
            "all_source_files": [str(source)],
            "accounting_decision": {
                "document_type": "Supplier Invoice",
                "posting_action": "Create Bill",
                "qbo_object": "Bill",
                "payment_status": "Unpaid",
                "payment_method": "Unknown",
                "confidence": 0.97,
                "requires_review": False,
                "decision_explanation": "Supplier invoice detected.",
            },
        }
        item.update(overrides)
        path = root / "data" / "extracted" / name
        path.write_text(json.dumps(item, indent=2))
        return path

    def test_dashboard_loads_without_live_apis(self):
        server, thread = review_dashboard.start_in_thread(port=0)
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=5) as response:
                html = response.read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertIn("CE Review Dashboard", html)

    def test_queue_index_builds_from_sample_extracted_json(self):
        with self.dashboard_paths() as root:
            self.write_item(root)
            index = review_dashboard.build_queue_index()
        self.assertEqual(index["total"], 1)
        self.assertEqual(index["items"][0]["vendor"], "Vendor LLC")
        self.assertEqual(index["items"][0]["posting_action"], "Create Bill")

    def test_document_route_blocks_path_traversal(self):
        server, thread = review_dashboard.start_in_thread(port=0)
        try:
            with self.assertRaises(HTTPError) as ctx:
                urlopen(f"http://127.0.0.1:{server.server_port}/api/document/..%2Fsecret.json", timeout=5)
            ctx.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_approve_action_writes_review_decision_and_does_not_post_qbo(self):
        with self.dashboard_paths() as root, patch("qbo.qbo_poster.run") as qbo_run:
            self.write_item(root)
            result = review_dashboard.perform_action("invoice.extracted.json", "approve", {"fields": {}})
            decisions = json.loads((root / "data" / "processed" / "review_decisions.json").read_text())
        self.assertTrue(result["ok"])
        self.assertEqual(decisions["invoice.extracted.json"], "approved")
        qbo_run.assert_not_called()

    def test_approve_and_learn_updates_rule_files(self):
        with self.dashboard_paths() as root:
            self.write_item(root)
            preview = review_dashboard.learning_preview(
                "invoice.extracted.json",
                {"fields": {"vendor": "Corrected Vendor", "account_name": "Salon Consumables", "branch": "Branch South"}},
            )
            self.assertTrue(preview["requires_confirmation"])
            review_dashboard.perform_action(
                "invoice.extracted.json",
                "approve_learn",
                {
                    "confirm_learning": True,
                    "fields": {"vendor": "Corrected Vendor", "account_name": "Salon Consumables", "branch": "Branch South"},
                },
            )
            vendor_rules = json.loads((root / "config" / "vendor_rules.json").read_text())
            branch_rules = json.loads((root / "config" / "vendor_branch_rules.json").read_text())
            account_rules = json.loads((root / "config" / "account_rules.json").read_text())
        self.assertIn("Corrected Vendor", vendor_rules["vendors"])
        self.assertEqual(branch_rules["vendors"]["Corrected Vendor"]["branch"], "Branch South")
        self.assertEqual(account_rules["vendors"]["Corrected Vendor"]["primary_account"], "Salon Consumables")

    def test_approve_and_learn_requires_confirmation(self):
        with self.dashboard_paths() as root:
            self.write_item(root)
            with self.assertRaises(ValueError):
                review_dashboard.perform_action("invoice.extracted.json", "approve_learn", {"fields": {}})

    def test_dashboard_index_refreshes_from_local_files_with_missing_extraction(self):
        with self.dashboard_paths() as root:
            local = root / "data" / "review" / "Sample Brand B" / "Branch South" / "Needs Review"
            local.mkdir(parents=True)
            (local / "june-local.pdf").write_bytes(b"%PDF-1.4\n")
            index = review_dashboard.build_queue_index(write_index=False)
        self.assertEqual(index["local_files_found"], 1)
        self.assertEqual(index["files_missing_extraction"], 1)
        self.assertEqual(index["extraction_status_counts"]["No Extracted JSON"], 1)
        item = index["items"][0]
        self.assertEqual(item["status"], "needs_extraction")
        self.assertEqual(item["branch"], "Branch South")
        self.assertEqual(item["qbo_class_name"], "Brand B - Branch South")

    def test_local_raw_file_matches_existing_extracted_json(self):
        with self.dashboard_paths() as root:
            self.write_item(root, source_file=str(root / "data" / "raw" / "invoice.pdf"), all_source_files=[])
            index = review_dashboard.build_queue_index(write_index=False)
        self.assertEqual(index["local_files_found"], 1)
        self.assertEqual(index["files_matched_to_extracted_json"], 1)
        self.assertEqual(index["files_missing_extraction"], 0)

    def test_branch_and_qbo_mapping_inferred_from_review_folder(self):
        with self.dashboard_paths() as root:
            local = root / "data" / "review" / "Sample Brand B" / "Branch South" / "Needs Review"
            local.mkdir(parents=True)
            source = local / "invoice.pdf"
            source.write_bytes(b"%PDF-1.4\n")
            self.write_item(
                root,
                source_file=str(source),
                all_source_files=[str(source)],
                branch=None,
                brand=None,
                business_unit=None,
                qbo_class_name=None,
                qbo_location_name=None,
            )
            detail = review_dashboard.item_detail("invoice.extracted.json")
        editable = detail["editable"]
        self.assertEqual(editable["branch"], "Branch South")
        self.assertEqual(editable["brand"], "Sample Brand B")
        self.assertEqual(editable["qbo_class_name"], "Brand B - Branch South")
        self.assertEqual(editable["qbo_location_name"], "Branch South")

    def test_local_branch_raw_folder_path_is_preserved_and_used(self):
        with self.dashboard_paths() as root:
            local = root / "data" / "raw" / "June Bills" / "Branch South"
            local.mkdir(parents=True)
            source = local / "Contoso.pdf"
            source.write_bytes(b"%PDF-1.4\n")
            index = review_dashboard.build_queue_index(write_index=False)
        self.assertEqual(index["local_files_found"], 1)
        item = index["items"][0]
        self.assertEqual(item["branch"], "Branch South")
        self.assertEqual(item["brand"], "Sample Brand B")
        self.assertEqual(item["qbo_class_name"], "Brand B - Branch South")

    def test_flattened_raw_file_maps_back_to_original_folder_metadata(self):
        with self.dashboard_paths() as root:
            source = root / "data" / "raw" / "drive_abc123_Contoso.pdf"
            source.write_bytes(b"%PDF-1.4\n")
            (source.with_name(f"{source.name}.metadata.json")).write_text(json.dumps({
                "source": "drive",
                "raw_filename": source.name,
                "original_relative_source_path": "June Bills/Branch South/Contoso.pdf",
                "folder_path_hint": "June Bills/Branch South",
            }))
            self.write_item(
                root,
                "contoso.extracted.json",
                source_file=str(source),
                all_source_files=[str(source)],
                branch=None,
                brand=None,
                business_unit=None,
                qbo_class_name=None,
                qbo_location_name=None,
            )
            detail = review_dashboard.item_detail("contoso.extracted.json")
        self.assertEqual(detail["editable"]["branch"], "Branch South")
        self.assertEqual(detail["editable"]["qbo_class_name"], "Brand B - Branch South")
        self.assertTrue(detail["item"]["source_metadata_available"])
        self.assertIn("June Bills/Branch South/Contoso.pdf", detail["item"]["source_path_hints"])

    def test_existing_flattened_raw_file_remains_compatible_without_metadata(self):
        with self.dashboard_paths() as root:
            source = root / "data" / "raw" / "drive_abc123_Contoso.pdf"
            source.write_bytes(b"%PDF-1.4\n")
            self.write_item(
                root,
                "old-flat.extracted.json",
                source_file=str(source),
                all_source_files=[str(source)],
                branch=None,
                brand=None,
                business_unit=None,
                qbo_class_name=None,
                qbo_location_name=None,
            )
            metadata = review_dashboard.refresh_source_metadata_index(write_index=True)
            detail = review_dashboard.item_detail("old-flat.extracted.json")
        entry = metadata["dashboard_documents"][str(source.resolve())]
        self.assertFalse(entry["source_metadata_available"])
        self.assertIn("branch", detail["missing_reasons"])
        self.assertEqual(
            detail["missing_reasons"]["branch"],
            "Source folder metadata unavailable.",
        )
        self.assertTrue(any(w["code"] == "missing_branch" and w.get("reason") == "Source folder metadata unavailable." for w in detail["warnings"]))

    def test_source_metadata_index_records_branch_and_extraction_match(self):
        with self.dashboard_paths() as root:
            local = root / "data" / "raw" / "June Bills" / "Branch South"
            local.mkdir(parents=True)
            source = local / "invoice.pdf"
            source.write_bytes(b"%PDF-1.4\n")
            self.write_item(
                root,
                source_file=str(source),
                all_source_files=[str(source)],
                branch=None,
                brand=None,
                business_unit=None,
                qbo_class_name=None,
                qbo_location_name=None,
            )
            (root / "data" / "raw" / "invoice.pdf").unlink(missing_ok=True)
            metadata = review_dashboard.refresh_source_metadata_index(write_index=False)
        summary = metadata["summary"]
        self.assertEqual(summary["total_local_files"], 1)
        self.assertEqual(summary["files_matched_to_extracted_json"], 1)
        self.assertEqual(summary["branch_counts"]["Branch South"], 1)
        entry = metadata["dashboard_documents"][str(source.resolve())]
        self.assertEqual(entry["original_relative_source_path"], "raw/June Bills/Branch South/invoice.pdf")
        self.assertEqual(entry["branch"], "Branch South")
        self.assertEqual(entry["brand"], "Sample Brand B")
        self.assertEqual(entry["qbo_class"], "Brand B - Branch South")
        self.assertEqual(entry["qbo_location"], "Branch South")
        self.assertEqual(entry["extraction_json_match"], "invoice.extracted.json")

    def test_supplier_memory_infers_account_payment_and_posting_fields(self):
        with self.dashboard_paths() as root:
            self.write_item(
                root,
                line_items=[{"description": "Supplies", "amount": 105, "account_name": ""}],
                payment_method="Unknown",
                posting_action="Unknown",
                qbo_object="None",
                accounting_decision={},
            )
            detail = review_dashboard.item_detail("invoice.extracted.json")
        editable = detail["editable"]
        self.assertEqual(editable["account_name"], "Salon Consumables")
        self.assertIn(editable["posting_action"], {"Create Bill", "Unknown", "Needs Human Review"})
        self.assertTrue(editable["payment_status"])

    def test_missing_reasons_and_field_confidence_labels(self):
        with self.dashboard_paths() as root:
            self.write_item(root, tax_amount=None, branch=None, line_items=[])
            detail = review_dashboard.item_detail("invoice.extracted.json")
        self.assertIn("tax_amount", detail["missing_reasons"])
        self.assertIn("branch", detail["missing_reasons"])
        self.assertIn(detail["field_confidences"]["tax_amount"]["confidence"], {"Missing", "Low"})
        self.assertIn(detail["field_confidences"]["vendor"]["confidence"], {"High", "Medium", "Low"})

    def test_structured_feedback_saved_to_audit_trail(self):
        with self.dashboard_paths() as root:
            self.write_item(root)
            review_dashboard.save_edits(
                "invoice.extracted.json",
                {
                    "vendor": "Changed Vendor",
                    "issue_type": "Supplier",
                    "correction": "Changed Vendor",
                    "feedback_reason": "Supplier name corrected from invoice header.",
                },
                reviewer="Reviewer",
            )
            item = json.loads((root / "data" / "extracted" / "invoice.extracted.json").read_text())
        event = item["review_history"][-1]
        self.assertEqual(event["structured_feedback"]["issue_type"], "Supplier")
        self.assertEqual(event["field_changes"][0]["field_changed"], "supplier_name")
        self.assertEqual(event["field_changes"][0]["user"], "Reviewer")

    def test_reject_action_saves_reason(self):
        with self.dashboard_paths() as root:
            self.write_item(root)
            review_dashboard.perform_action("invoice.extracted.json", "reject", {"reason": "Wrong company"})
            item = json.loads((root / "data" / "extracted" / "invoice.extracted.json").read_text())
            decisions = json.loads((root / "data" / "processed" / "review_decisions.json").read_text())
        self.assertEqual(decisions["invoice.extracted.json"], "rejected")
        self.assertEqual(item["review_reason"], "Wrong company")

    def test_duplicate_action_saves_status(self):
        with self.dashboard_paths() as root:
            self.write_item(root)
            review_dashboard.perform_action("invoice.extracted.json", "duplicate", {"duplicate_of": "old.json"})
            item = json.loads((root / "data" / "extracted" / "invoice.extracted.json").read_text())
            decisions = json.loads((root / "data" / "processed" / "review_decisions.json").read_text())
        self.assertEqual(decisions["invoice.extracted.json"], "duplicate")
        self.assertEqual(item["duplicate_of"], "old.json")

    def test_metrics_calculation(self):
        with self.dashboard_paths() as root:
            self.write_item(root)
            self.write_item(
                root,
                "low.extracted.json",
                overall_confidence=0.5,
                branch=None,
                accounting_decision={
                    "document_type": "Supplier Invoice",
                    "posting_action": "Create Bill",
                    "qbo_object": "Bill",
                    "confidence": 0.5,
                    "requires_review": True,
                },
            )
            (root / "data" / "processed" / "review_decisions.json").write_text(json.dumps({
                "invoice.extracted.json": "approved",
                "low.extracted.json": "needs_review",
            }))
            metrics = review_dashboard.metrics_summary()
        self.assertEqual(metrics["total_items"], 2)
        self.assertEqual(metrics["approved"], 1)
        self.assertEqual(metrics["needs_review"], 1)
        self.assertGreater(metrics["low_confidence"], 0)

    def test_missing_file_handling(self):
        with self.dashboard_paths() as root:
            self.write_item(root, source_file=str(root / "data" / "raw" / "missing.pdf"), all_source_files=[])
            detail = review_dashboard.item_detail("invoice.extracted.json")
        self.assertFalse(detail["sources"][0]["exists"])
        self.assertIsNone(detail["sources"][0]["document_url"])
        self.assertTrue(any(w["code"] == "attachment_missing" for w in detail["warnings"]))


if __name__ == "__main__":
    unittest.main()
