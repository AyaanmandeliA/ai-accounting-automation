from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SECRETS_DIR = BASE_DIR / "config/secrets.local"

SECRET_FILES = {
    "app_config": "credentials.json",
    "google_oauth": "gmail_oauth.json",
    "gmail_token": "gmail_token.json",
    "drive_token": "drive_token.json",
    "qbo_config": "qbo_config.json",
    "qbo_tokens": "qbo_tokens.json",
}

SECRET_PATH_ENV = {
    "app_config": "CE_APP_CONFIG_FILE",
    "google_oauth": "CE_GOOGLE_OAUTH_FILE",
    "gmail_token": "CE_GMAIL_TOKEN_FILE",
    "drive_token": "CE_DRIVE_TOKEN_FILE",
    "qbo_config": "CE_QBO_CONFIG_FILE",
    "qbo_tokens": "CE_QBO_TOKENS_FILE",
}


def secret_path(name: str, legacy_path: Path | None = None, *, for_write: bool = False) -> Path:
    env_name = SECRET_PATH_ENV[name]
    env_path = os.environ.get(env_name)
    if env_path:
        return Path(env_path).expanduser()

    local_path = SECRETS_DIR / SECRET_FILES[name]
    if local_path.exists() or for_write:
        return local_path

    if legacy_path and legacy_path.exists():
        return legacy_path
    return local_path


def load_secret_json(name: str, legacy_path: Path | None = None) -> dict:
    path = secret_path(name, legacy_path)
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read required local secret/config file for {name}: {path}") from exc


def secret_gitignore_patterns() -> set[str]:
    return {
        "config/secrets.local/",
        "config/secrets.local/*",
        ".env",
        "*.env.local",
        "config/credentials.json",
        "config/gmail_oauth.json",
        "config/gmail_token.json",
        "config/drive_token.json",
        "config/qbo_config.json",
        "config/qbo_tokens.json",
    }

