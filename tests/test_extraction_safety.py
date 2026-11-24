import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from extraction.ai_extractor import _remove_stale_outputs, _stale_ledger_info


class ExtractionSafetyTests(unittest.TestCase):
    def test_stale_version_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ai_extracted.json"
            ledger.write_text(json.dumps({"version": 5, "files": ["a.pdf"]}))
            with patch("extraction.ai_extractor.PROCESSED_FILE", ledger), \
                 patch("extraction.ai_extractor.PROCESSED_VERSION", 14):
                info = _stale_ledger_info()
        self.assertTrue(info["stale"])
        self.assertEqual(info["old_version"], 5)
        self.assertEqual(info["files"], ["a.pdf"])

    def test_old_extracted_files_are_archived_before_replacement(self):
        with tempfile.TemporaryDirectory() as tmp:
            extracted_dir = Path(tmp) / "extracted"
            extracted_dir.mkdir()
            old = extracted_dir / "invoice.pdf.extracted.json"
            old.write_text('{"old": true}')
            with patch("extraction.ai_extractor.EXTRACTED_DIR", extracted_dir):
                archived = _remove_stale_outputs([Path("invoice.pdf")], archive_label="version_5")
            self.assertFalse(old.exists())
            self.assertEqual(len(archived), 1)
            self.assertTrue((extracted_dir / "archive_version_5" / "invoice.pdf.extracted.json").exists())


if __name__ == "__main__":
    unittest.main()

