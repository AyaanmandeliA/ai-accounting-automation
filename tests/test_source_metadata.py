import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ingestion import source_metadata


class SourceMetadataTests(unittest.TestCase):
    def test_write_and_read_flattened_raw_source_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "data" / "raw"
            processed = root / "data" / "processed"
            raw.mkdir(parents=True)
            processed.mkdir(parents=True)
            file_path = raw / "drive_abc123_Contoso.pdf"
            file_path.write_bytes(b"%PDF-1.4\n")
            with patch.object(source_metadata, "BASE_DIR", root), \
                 patch.object(source_metadata, "DATA_DIR", root / "data"), \
                 patch.object(source_metadata, "RAW_DIR", raw), \
                 patch.object(source_metadata, "PROCESSED_DIR", processed), \
                 patch.object(source_metadata, "SOURCE_METADATA_INDEX_FILE", processed / "source_metadata_index.json"):
                source_metadata.write_source_metadata(file_path, {
                    "source": "drive",
                    "original_relative_source_path": "June Bills/Branch South/Contoso.pdf",
                    "folder_path_hint": "June Bills/Branch South",
                })
                metadata = source_metadata.read_source_metadata(file_path)
        self.assertEqual(metadata["original_relative_source_path"], "June Bills/Branch South/Contoso.pdf")
        self.assertEqual(metadata["source_path_hint"], "June Bills/Branch South/Contoso.pdf")


if __name__ == "__main__":
    unittest.main()
