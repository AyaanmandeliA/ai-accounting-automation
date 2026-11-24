import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from secrets_manager import SECRET_FILES, load_secret_json, secret_gitignore_patterns, secret_path


BASE_DIR = Path(__file__).resolve().parents[1]


class SecretSafetyTests(unittest.TestCase):
    def test_secret_patterns_are_ignored(self):
        ignored = (BASE_DIR / ".gitignore").read_text()
        for pattern in secret_gitignore_patterns():
            self.assertIn(pattern, ignored)

    def test_default_secret_path_prefers_ignored_local_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret_dir = Path(tmp) / "config/secrets.local"
            with patch("secrets_manager.SECRETS_DIR", secret_dir):
                path = secret_path("qbo_tokens", for_write=True)
        self.assertEqual(path, secret_dir / SECRET_FILES["qbo_tokens"])

    def test_env_secret_path_wins(self):
        with patch.dict("os.environ", {"CE_QBO_CONFIG_FILE": "/tmp/qbo_config.local.json"}):
            self.assertEqual(secret_path("qbo_config"), Path("/tmp/qbo_config.local.json"))

    def test_local_secret_file_is_used_before_legacy_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            secret_dir = base / "config/secrets.local"
            legacy_dir = base / "config"
            secret_dir.mkdir(parents=True)
            legacy_dir.mkdir(exist_ok=True)
            local_config = secret_dir / "credentials.json"
            legacy_config = legacy_dir / "credentials.json"
            local_config.write_text(json.dumps({"anthropic_api_key": "local-only"}))
            legacy_config.write_text("{")

            with patch("secrets_manager.SECRETS_DIR", secret_dir):
                loaded = load_secret_json("app_config", legacy_config)

        self.assertEqual(loaded["anthropic_api_key"], "local-only")

    def test_legacy_secret_templates_do_not_contain_working_secret_values(self):
        legacy_secret_files = [
            BASE_DIR / "config/credentials.json",
            BASE_DIR / "config/gmail_oauth.json",
            BASE_DIR / "config/gmail_token.json",
            BASE_DIR / "config/drive_token.json",
            BASE_DIR / "config/qbo_config.json",
            BASE_DIR / "config/qbo_tokens.json",
        ]
        allowed_exact_values = {
            "",
            "FILL_IN_LOCAL_SECRET_ONLY",
            "http://localhost",
            "http://localhost:8000/callback",
            "https://accounts.google.com/o/oauth2/auth",
            "https://oauth2.googleapis.com/token",
            "googleapis.com",
            "sandbox",
            "config/secrets.local/gmail_oauth.json",
            "config/secrets.local/qbo_tokens.json",
        }

        def sensitive_values(value):
            if isinstance(value, dict):
                for child in value.values():
                    yield from sensitive_values(child)
            elif isinstance(value, list):
                for child in value:
                    yield from sensitive_values(child)
            elif isinstance(value, str):
                yield value

        for path in legacy_secret_files:
            if not path.exists():
                continue  # local-only file; absent in a clean checkout
            data = json.loads(path.read_text())
            for value in sensitive_values(data):
                self.assertIn(value, allowed_exact_values, f"{path} contains a non-template value")


if __name__ == "__main__":
    unittest.main()
