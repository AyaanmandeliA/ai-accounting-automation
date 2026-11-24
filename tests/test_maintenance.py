import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import maintenance


class MaintenanceTests(unittest.TestCase):
    def test_archive_old_raw_data_dry_run_does_not_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw = base / "data/raw"
            raw.mkdir(parents=True)
            invoice = raw / "invoice.pdf"
            invoice.write_text("fake")
            with patch("tools.maintenance.BASE_DIR", base), \
                 patch("tools.maintenance.DATA_DIR", base / "data"), \
                 patch("tools.maintenance.ARCHIVE_DIR", base / "data/archive"):
                result = maintenance.archive_old_raw_data(dry_run=True)
            self.assertEqual(result["archived"], ["data/raw/invoice.pdf"])
            self.assertTrue(invoice.exists())

    def test_reset_review_queue_archives_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            review = base / "data/review"
            review.mkdir(parents=True)
            (review / "needs_review.json").write_text("{}")
            processed = base / "data/processed"
            processed.mkdir(parents=True)
            decisions = processed / "review_decisions.json"
            decisions.write_text('{"x": "needs_review"}')
            with patch("tools.maintenance.BASE_DIR", base), \
                 patch("tools.maintenance.DATA_DIR", base / "data"), \
                 patch("tools.maintenance.ARCHIVE_DIR", base / "data/archive"):
                result = maintenance.reset_review_queue(dry_run=False)
            self.assertFalse((review / "needs_review.json").exists())
            self.assertEqual(decisions.read_text(), "{}")
            self.assertGreaterEqual(len(result["archived"]), 2)

    def test_reset_sandbox_data_archives_old_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for folder in ("raw", "extracted", "review", "processed"):
                (base / "data" / folder).mkdir(parents=True, exist_ok=True)
            (base / "data/raw/invoice.pdf").write_text("fake")
            (base / "data/extracted/invoice.pdf.extracted.json").write_text("{}")
            (base / "data/review/needs_review.json").write_text("{}")
            (base / "data/processed/ai_extracted.json").write_text('{"version": 1, "files": ["invoice.pdf"]}')
            (base / "data/processed/review_decisions.json").write_text('{"invoice": "approved"}')

            with patch("tools.maintenance.BASE_DIR", base), \
                 patch("tools.maintenance.DATA_DIR", base / "data"), \
                 patch("tools.maintenance.ARCHIVE_DIR", base / "data/archive"):
                result = maintenance.reset_sandbox_data(dry_run=False, confirm=True)

            archive_root = base / result["archive_root"]
            self.assertTrue((archive_root / "raw/invoice.pdf").exists())
            self.assertTrue((archive_root / "extracted/invoice.pdf.extracted.json").exists())
            self.assertTrue((archive_root / "review/needs_review.json").exists())
            self.assertTrue((archive_root / "processed/ai_extracted.json").exists())
            self.assertTrue((archive_root / "processed/review_decisions.json").exists())
            self.assertTrue((base / "data/raw").is_dir())
            self.assertEqual((base / "data/processed/review_decisions.json").read_text(), "{}")

    def test_reset_sandbox_data_does_not_remove_qbo_posted_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            processed = base / "data/processed"
            processed.mkdir(parents=True, exist_ok=True)
            posted = processed / "qbo_posted.json"
            posted.write_text('{"posted": {"x": true}}')
            (processed / "ai_extracted.json").write_text("{}")

            with patch("tools.maintenance.BASE_DIR", base), \
                 patch("tools.maintenance.DATA_DIR", base / "data"), \
                 patch("tools.maintenance.ARCHIVE_DIR", base / "data/archive"):
                maintenance.reset_sandbox_data(dry_run=False, confirm=True)

            self.assertTrue(posted.exists())
            self.assertEqual(posted.read_text(), '{"posted": {"x": true}}')


if __name__ == "__main__":
    unittest.main()
