import subprocess
import sys
import unittest
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]


class DirectEntrypointSafetyTests(unittest.TestCase):
    def _run_script(self, script: str) -> str:
        result = subprocess.run(
            [sys.executable, script],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return result.stdout + result.stderr

    def test_qbo_poster_direct_execution_is_safe(self):
        output = self._run_script("qbo/qbo_poster.py")
        self.assertIn("Direct QBO posting is disabled", output)

    def test_gmail_ingest_direct_execution_is_safe(self):
        output = self._run_script("ingestion/gmail_ingest.py")
        self.assertIn("Direct Gmail ingestion is disabled", output)

    def test_drive_ingest_direct_execution_is_safe(self):
        output = self._run_script("ingestion/drive_ingest.py")
        self.assertIn("Direct Google Drive ingestion is disabled", output)

    def test_ai_extractor_direct_execution_is_safe(self):
        output = self._run_script("extraction/ai_extractor.py")
        self.assertIn("Direct live extraction is disabled", output)


if __name__ == "__main__":
    unittest.main()

