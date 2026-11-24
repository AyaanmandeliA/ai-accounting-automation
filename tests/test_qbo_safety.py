import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import patch

from qbo.qbo_poster import (
    VendorMissingError,
    _account_type_block_reason,
    _backfill_fingerprints,
    _business_unit_block_reason,
    _dedupe_key,
    _duplicate_fingerprint,
    _find_or_create_vendor,
    _payment_bank_warning,
    _save_failure,
    run,
)


class QboSafetyTests(unittest.TestCase):
    def setUp(self):
        self.audit_patch = patch("qbo.qbo_poster.append_qbo_posting_attempt")
        self.audit_patch.start()

    def tearDown(self):
        self.audit_patch.stop()

    def _bill(self, **overrides):
        data = {
            "supplier_name": "Vendor A",
            "invoice_number": "INV-1",
            "invoice_date": "2026-06-23",
            "total_amount": 105,
            "tax_amount": 5,
            "overall_confidence": 0.97,
            "extraction_confidence": 1,
            "vendor_confidence": 1,
            "amount_confidence": 1,
            "vat_confidence": 1,
            "branch_confidence": 1,
            "account_confidence": 1,
            "brand": "Sample Brand A",
            "business_unit": "Sample Brand A",
            "branch": "Branch North",
            "allocation_type": "branch",
            "document_type": "invoice",
            "qbo_posting_type": "Bill",
            "qbo_class_name": "Brand A - Branch North",
            "qbo_location_name": "Branch North",
            "line_items": [{"description": "x", "amount": 105, "account_name": "Repairs"}],
            "notes": "Tax invoice TRN 100000000000003",
            "source_file": __file__,
            "all_source_files": [__file__],
        }
        data.update(overrides)
        return data

    def test_duplicate_key_includes_branch(self):
        first = self._bill(branch="Branch North")
        second = self._bill(branch="Branch East")
        self.assertNotEqual(_dedupe_key(first), _dedupe_key(second))

    def test_needs_review_is_not_posted(self):
        item = self._bill(needs_review=True, review_reason="Low confidence")
        with patch("qbo.qbo_poster._load_qbo_config", return_value={"environment": "sandbox"}), \
             patch("qbo.qbo_poster._load_processed", return_value={}), \
             patch("qbo.qbo_poster._load_fingerprints", return_value={}), \
             patch("qbo.qbo_poster.copy_to_review_folder"), \
             patch("qbo.qbo_poster._get_qbo_client") as get_client:
            posted = run([item])
        self.assertEqual(posted, [])
        get_client.assert_not_called()

    def test_unsupported_type_is_routed_not_posted(self):
        item = self._bill(payment_confirmed=True, payment_amount=105, payment_method="bank_transfer")
        with patch("qbo.qbo_poster._load_qbo_config", return_value={"environment": "sandbox"}), \
             patch("qbo.qbo_poster._load_processed", return_value={}), \
             patch("qbo.qbo_poster._load_fingerprints", return_value={}), \
             patch("qbo.qbo_poster.copy_to_review_folder"), \
             patch("qbo.qbo_poster._get_qbo_client") as get_client:
            posted = run([item])
        self.assertEqual(posted, [])
        get_client.assert_not_called()

    def test_non_bill_document_category_is_routed_not_posted(self):
        item = self._bill(document_category="Payment Proof", notes="payment proof only")
        with patch("qbo.qbo_poster._load_qbo_config", return_value={"environment": "sandbox"}), \
             patch("qbo.qbo_poster._load_processed", return_value={}), \
             patch("qbo.qbo_poster._load_fingerprints", return_value={}), \
             patch("qbo.qbo_poster.copy_to_review_folder"), \
             patch("qbo.qbo_poster._get_qbo_client") as get_client:
            posted = run([item])
        self.assertEqual(posted, [])
        get_client.assert_not_called()

    def test_missing_class_location_is_not_posted(self):
        item = self._bill(qbo_class_name=None)
        with patch("qbo.qbo_poster._load_qbo_config", return_value={"environment": "sandbox"}), \
             patch("qbo.qbo_poster._load_processed", return_value={}), \
             patch("qbo.qbo_poster._load_fingerprints", return_value={}), \
             patch("qbo.qbo_poster.copy_to_review_folder"), \
             patch("qbo.qbo_poster._get_qbo_client") as get_client:
            posted = run([item])
        self.assertEqual(posted, [])
        get_client.assert_not_called()

    def test_incompatible_account_type_is_not_posted(self):
        item = self._bill(line_items=[{"description": "x", "amount": 100, "account_name": "Uncategorized Income"}])
        coa = {
            "uncategorized income": {
                "name": "Uncategorized Income",
                "account_type": "Income",
                "detail_type": "Sales",
            }
        }
        self.assertIn("Incompatible account", _account_type_block_reason(item, coa))
        with patch("qbo.qbo_poster._load_qbo_config", return_value={"environment": "sandbox"}), \
             patch("qbo.qbo_poster._load_processed", return_value={}), \
             patch("qbo.qbo_poster._load_fingerprints", return_value={}), \
             patch("qbo.qbo_poster._load_local_coa", return_value=coa), \
             patch("qbo.qbo_poster.copy_to_review_folder"), \
             patch("qbo.qbo_poster._get_qbo_client") as get_client:
            posted = run([item])
        self.assertEqual(posted, [])
        self.assertTrue(item["needs_review"])
        self.assertIn("Incompatible account", item["review_reason"])
        get_client.assert_not_called()

    def test_failure_reason_is_stored(self):
        item = self._bill()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "failures.json"
            with patch("qbo.qbo_poster.FAILURES_FILE", path):
                _save_failure("invoice.extracted.json", item, "Attachment upload failed")
            data = json.loads(path.read_text())
        self.assertIn("invoice.extracted.json", data)
        self.assertEqual(data["invoice.extracted.json"]["reason"], "Attachment upload failed")

    def test_null_business_unit_has_explicit_block_reason(self):
        item = self._bill(brand=None, business_unit=None)
        reason = _business_unit_block_reason(item)
        self.assertIn("Unknown business unit", reason)

    def test_duplicate_fingerprint_same_when_filename_changes(self):
        first = self._bill(source_file=__file__)
        second = self._bill(source_file=__file__, extracted_file="/tmp/new-name.extracted.json")
        self.assertEqual(_duplicate_fingerprint(first), _duplicate_fingerprint(second))

    def test_duplicate_fingerprint_blocks_repost_before_qbo_connection(self):
        item = self._bill(extracted_file="/tmp/new-name.extracted.json")
        fingerprint = _duplicate_fingerprint(item)
        with patch("qbo.qbo_poster._load_qbo_config", return_value={"environment": "sandbox"}), \
             patch("qbo.qbo_poster._load_processed", return_value={}), \
             patch("qbo.qbo_poster._load_fingerprints", return_value={fingerprint: {"bill_id": "185", "file": "old.json"}}), \
             patch("qbo.qbo_poster.copy_to_review_folder"), \
             patch("qbo.qbo_poster._get_qbo_client") as get_client:
            posted = run([item])
        self.assertEqual(posted, [])
        get_client.assert_not_called()

    def test_orphaned_posted_entries_are_preserved_during_backfill(self):
        processed = {"missing.extracted.json": "185"}
        fingerprints, changed = _backfill_fingerprints(processed, {})
        self.assertEqual(fingerprints, {})
        self.assertEqual(changed, 0)
        self.assertEqual(processed["missing.extracted.json"], "185")

    def test_payment_bank_mismatch_surfaces_warning(self):
        item = self._bill(payment_confirmed=True, payment_bank_account="Bank Missing")
        fake_client = object()
        with patch("qbo.qbo_poster._find_bank_account", return_value=None), \
             patch("qbo.qbo_poster._available_bank_account_names", return_value=["Example Bank - Brand A"]):
            warning = _payment_bank_warning(fake_client, item)
        self.assertEqual(warning["status"], "posted_unpaid_due_to_missing_bank_account")
        self.assertEqual(warning["required_bank_account"], "Bank Missing")

    def test_missing_vendor_blocks_posting_by_default(self):
        item = self._bill()
        with patch("qbo.qbo_poster._load_qbo_config", return_value={"environment": "sandbox"}), \
             patch("qbo.qbo_poster._load_processed", return_value={}), \
             patch("qbo.qbo_poster._load_fingerprints", return_value={}), \
             patch("qbo.qbo_poster._get_qbo_client", return_value=(object(), {"access_token": "x", "realm_id": "1"})), \
             patch("qbo.qbo_poster._load_account_cache", return_value={}), \
             patch("qbo.qbo_poster._load_class_cache", return_value={}), \
             patch("qbo.qbo_poster._load_location_cache", return_value={}), \
             patch("qbo.qbo_poster.Vendor.filter", return_value=[]), \
             patch("qbo.qbo_poster._save_failure") as save_failure, \
             patch("qbo.qbo_poster.copy_to_review_folder"):
            posted = run([item])
        self.assertEqual(posted, [])
        self.assertTrue(item["needs_review"])
        self.assertTrue(item["manual_check"])
        self.assertIn("Vendor missing in QBO", item["review_reason"])
        save_failure.assert_called_once()

    def test_vendor_creation_requires_explicit_flag(self):
        item = self._bill()
        with patch("qbo.qbo_poster.Vendor.filter", return_value=[]):
            with self.assertRaises(VendorMissingError):
                _find_or_create_vendor(object(), "New Vendor", "AED", item)

    def test_vendor_creation_allowed_with_explicit_flag_records_audit(self):
        class FakeVendor:
            saved = False

            @staticmethod
            def filter(*args, **kwargs):
                return []

            def save(self, qb=None):
                self.Id = "999"
                FakeVendor.saved = True

        item = self._bill()
        with patch("qbo.qbo_poster.Vendor", FakeVendor):
            vendor = _find_or_create_vendor(
                object(),
                "New Vendor",
                "AED",
                item,
                allow_vendor_create=True,
            )
        self.assertTrue(FakeVendor.saved)
        self.assertEqual(vendor.Id, "999")
        self.assertEqual(item["audit_trail"]["qbo_ids"]["vendor_id"], "999")
        self.assertEqual(
            item["audit_trail"]["processing_history"][-1]["event"],
            "qbo_vendor_created",
        )


if __name__ == "__main__":
    unittest.main()
