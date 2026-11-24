import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from allocation.branch_resolver import apply_folder_allocation, apply_qbo_mapping, resolve_from_folder_path
from allocation.confidence import apply_review_gate
from allocation.review_router import copy_to_review_folder


class AllocationTests(unittest.TestCase):
    def test_folder_path_branch_allocation(self):
        result = resolve_from_folder_path("/Sample Brand B/Branch North/Bills/")
        self.assertEqual(result["brand"], "Sample Brand B")
        self.assertEqual(result["branch"], "Branch North")
        self.assertEqual(result["allocation_type"], "branch")
        self.assertEqual(result["branch_confidence"], 1.0)

    def test_unique_branch_folder_without_brand_allocation(self):
        result = resolve_from_folder_path("/June Bills/Branch South/Contoso.pdf")
        self.assertEqual(result["brand"], "Sample Brand B")
        self.assertEqual(result["branch"], "Branch South")
        self.assertEqual(result["allocation_type"], "branch")
        self.assertEqual(result["branch_confidence"], 0.9)

    def test_head_office_allocation(self):
        result = resolve_from_folder_path("/Head Office/Bills/")
        self.assertEqual(result["brand"], "Head Office")
        self.assertIsNone(result["branch"])
        self.assertEqual(result["allocation_type"], "head_office")

    def test_unknown_branch_needs_review(self):
        extracted = {
            "supplier_name": "Test Vendor",
            "total_amount": 100,
            "line_items": [{"description": "x", "amount": 100, "account_name": "Repairs"}],
        }
        enriched = apply_folder_allocation(extracted, "/Sample Brand B/New Future Branch/Bills/")
        enriched = apply_qbo_mapping(enriched)
        enriched = apply_review_gate(enriched)
        self.assertTrue(enriched["needs_review"])
        self.assertIn("Branch is missing", enriched["review_reason"])

    def test_folder_path_conflict_needs_review(self):
        extracted = {
            "brand": "Sample Brand A",
            "business_unit": "Sample Brand A",
            "branch": "Branch East",
            "allocation_type": "branch",
            "supplier_name": "Test Vendor",
            "total_amount": 100,
            "line_items": [{"description": "x", "amount": 100, "account_name": "Repairs"}],
        }
        enriched = apply_folder_allocation(extracted, "/Sample Brand B/Branch North/Bills/")
        self.assertTrue(enriched["needs_review"])
        self.assertIn("Folder path conflict", enriched["review_reason"])

    def test_shared_bill_needs_review(self):
        extracted = {
            "supplier_name": "Marketing Vendor",
            "total_amount": 100,
            "line_items": [{"description": "x", "amount": 100, "account_name": "Marketing expenses"}],
        }
        enriched = apply_folder_allocation(extracted, "/Shared/Bills/")
        enriched = apply_qbo_mapping(enriched)
        enriched = apply_review_gate(enriched)
        self.assertTrue(enriched["needs_review"])
        self.assertIn("Shared expense", enriched["review_reason"])

    def test_class_mapping_applied(self):
        extracted = {
            "supplier_name": "Test Vendor",
            "total_amount": 100,
            "line_items": [{"description": "x", "amount": 100, "account_name": "Repairs"}],
        }
        enriched = apply_folder_allocation(extracted, "/Sample Brand A/Branch North/Bills/")
        enriched = apply_qbo_mapping(enriched)
        self.assertEqual(enriched["qbo_class_name"], "Brand A - Branch North")
        self.assertEqual(enriched["qbo_location_name"], "Branch North")

    def test_review_folder_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "invoice.pdf"
            source.write_text("fake")
            extracted = {
                "source_file": str(source),
                "all_source_files": [str(source)],
                "brand": "Sample Brand B",
                "branch": "Branch East",
                "needs_review": True,
                "review_reason": "Test reason",
                "overall_confidence": 0.8,
            }
            with patch("allocation.review_router.REVIEW_ROOT", Path(tmp) / "review"):
                folder = copy_to_review_folder(extracted)
            self.assertIsNotNone(folder)
            self.assertTrue((folder / "invoice.pdf").exists())
            self.assertTrue(any(p.name.endswith(".review_reason.json") for p in folder.iterdir()))


if __name__ == "__main__":
    unittest.main()
