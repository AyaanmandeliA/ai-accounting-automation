"""
Phase 1 — Google Drive Ingestion
Polls the CE Accounting Inbox Drive folder for new files (invoices, bills, receipts).
Downloads new files to data/raw/ and logs what was pulled.
"""

if __name__ == "__main__":
    print("Direct Google Drive ingestion is disabled. Use: python main.py --ingest")
    raise SystemExit(0)

import json
import logging
import re
import io
from pathlib import Path
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload
from ingestion.source_metadata import write_source_metadata
from secrets_manager import load_secret_json, secret_path

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
BASE_DIR = Path(__file__).resolve().parents[1]
RAW_DIR = BASE_DIR / "data/raw"
PROCESSED_DIR = BASE_DIR / "data/processed"
PROCESSED_FILE = PROCESSED_DIR / "drive_ingested.json"
TOKEN_FILE = secret_path("drive_token", BASE_DIR / "config/drive_token.json")
CREDENTIALS_FILE = secret_path("google_oauth", BASE_DIR / "config/gmail_oauth.json")
APP_CONFIG_FILE = secret_path("app_config", BASE_DIR / "config/credentials.json")

ALLOWED_MIME_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "text/csv",
}
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".xlsx", ".xls", ".csv"}
MAX_FILE_BYTES = 25 * 1024 * 1024
GOOGLE_APPS_PREFIX = "application/vnd.google-apps."


def _load_drive_folder_ids() -> list[str]:
    config = load_secret_json("app_config", BASE_DIR / "config/credentials.json")
    # support list (new) or single string (legacy)
    ids = config.get("drive_folder_ids") or config.get("drive_folder_id")
    if isinstance(ids, str):
        ids = [ids]
    ids = [str(i).strip() for i in (ids or []) if str(i).strip()]
    if not ids:
        raise RuntimeError("drive_folder_ids missing from app config")
    return ids


def _load_processed() -> set[str]:
    if not PROCESSED_FILE.exists():
        return set()
    try:
        data = json.loads(PROCESSED_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read Drive ingestion ledger; starting fresh: %s", exc)
        return set()
    return set(data.get("file_ids", []))


def _save_processed(processed: set[str]) -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = PROCESSED_FILE.with_suffix(".tmp")
    tmp_path.write_text(json.dumps({"file_ids": sorted(processed)}, indent=2))
    tmp_path.replace(PROCESSED_FILE)


def _sanitize_filename(filename: str, fallback: str = "attachment") -> str:
    basename = Path(filename.replace("\\", "/")).name.strip()
    if not basename:
        basename = fallback
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", basename)


def _is_drive_native(mime_type: str) -> bool:
    return mime_type.startswith(GOOGLE_APPS_PREFIX)


def authenticate() -> object:
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
        if not creds.has_scopes(SCOPES):
            log.warning("Drive token is missing required scope; reauthenticating.")
            creds = None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                log.warning("Drive token refresh failed; reauthenticating.")
                creds = None
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    if TOKEN_FILE.exists():
        TOKEN_FILE.chmod(0o600)
    return build("drive", "v3", credentials=creds)


def list_folder_files(service, folder_id: str) -> list[dict]:
    files = []
    page_token = None
    query = (
        f"'{folder_id}' in parents "
        "and trashed = false "
        "and mimeType != 'application/vnd.google-apps.folder' "
        "and mimeType != 'application/vnd.google-apps.shortcut'"
    )
    while True:
        kwargs = {
            "q": query,
            "fields": "nextPageToken, files(id, name, mimeType, size, modifiedTime, parents)",
            "pageSize": 100,
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.files().list(**kwargs).execute()
        files.extend(
            file_meta
            for file_meta in result.get("files", [])
            if not _is_drive_native(file_meta.get("mimeType", ""))
        )
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return files


def _drive_item_name(service, file_id: str) -> str:
    meta = service.files().get(
        fileId=file_id,
        fields="id,name,parents",
        supportsAllDrives=True,
    ).execute()
    return _sanitize_filename(meta.get("name", file_id), file_id)


def _folder_path_hint(service, file_meta: dict, root_folder_id: str) -> str:
    parents = list(file_meta.get("parents") or [])
    if not parents:
        return ""
    current = parents[0]
    parts = []
    seen = set()
    while current and current not in seen:
        seen.add(current)
        try:
            meta = service.files().get(
                fileId=current,
                fields="id,name,parents",
                supportsAllDrives=True,
            ).execute()
        except HttpError:
            break
        parts.append(_drive_item_name(service, current))
        if current == root_folder_id:
            break
        next_parents = meta.get("parents") or []
        current = next_parents[0] if next_parents else None
    return "/".join(reversed(parts))


def _write_source_metadata(out_path: Path, file_meta: dict, folder_path: str) -> None:
    original_relative_path = "/".join(part for part in (folder_path, file_meta.get("name", "")) if part)
    metadata = {
        "source": "drive",
        "drive_file_id": file_meta.get("id"),
        "drive_name": file_meta.get("name"),
        "mime_type": file_meta.get("mimeType"),
        "modified_time": file_meta.get("modifiedTime"),
        "folder_path": folder_path,
        "folder_path_hint": folder_path,
        "original_relative_source_path": original_relative_path,
        "source_path_hint": original_relative_path or folder_path,
    }
    write_source_metadata(out_path, metadata)


def download_file(service, file_meta: dict, processed: set[str], folder_path: str = "") -> str | None:
    file_id = file_meta["id"]
    name = file_meta.get("name", "attachment")
    mime_type = file_meta.get("mimeType", "")
    size = int(file_meta.get("size", 0) or 0)

    if _is_drive_native(mime_type):
        log.info("Skipped Drive-native item: %s (%s)", name, mime_type)
        return None

    ext = Path(name).suffix.lower()
    if mime_type not in ALLOWED_MIME_TYPES and ext not in ALLOWED_EXTENSIONS:
        log.info("Skipped unsupported file type: %s (%s)", name, mime_type)
        return None

    if size > MAX_FILE_BYTES:
        log.warning("Skipped oversized file: %s (%d bytes)", name, size)
        return None

    if file_id in processed:
        log.info("Already ingested: %s", name)
        return None

    safe_name = f"drive_{file_id}_{_sanitize_filename(name)}"
    out_path = RAW_DIR / safe_name
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")

    request = service.files().get_media(fileId=file_id)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    tmp_path.write_bytes(buffer.getvalue())
    tmp_path.replace(out_path)
    _write_source_metadata(out_path, file_meta, folder_path)
    processed.add(file_id)
    log.info("Downloaded: %s", safe_name)
    return str(out_path)


def run() -> list[str]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    processed = _load_processed()
    folder_ids = _load_drive_folder_ids()

    log.info("Authenticating with Google Drive...")
    service = authenticate()

    all_downloaded = []
    for folder_id in folder_ids:
        log.info("Listing files in Drive folder %s...", folder_id)
        files = list_folder_files(service, folder_id)
        log.info("Found %d files in Drive folder %s", len(files), folder_id)
        for file_meta in files:
            try:
                folder_path = _folder_path_hint(service, file_meta, folder_id)
                path = download_file(service, file_meta, processed, folder_path)
            except (HttpError, OSError, KeyError, ValueError) as exc:
                log.exception("Failed to ingest Drive file %s: %s", file_meta.get("id"), exc)
                continue
            if path:
                all_downloaded.append(path)
                _save_processed(processed)

    log.info("Total files downloaded: %d", len(all_downloaded))
    return all_downloaded
