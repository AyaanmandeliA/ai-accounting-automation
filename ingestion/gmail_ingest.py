"""
Phase 1 — Gmail Ingestion
Polls the configured Gmail account for emails with attachments (invoices, bills, receipts).
Downloads attachments to data/raw/ and logs what was pulled.
"""

if __name__ == "__main__":
    print("Direct Gmail ingestion is disabled. Use: python main.py --ingest")
    raise SystemExit(0)

import base64
import html
import json
import logging
import re
from pathlib import Path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from ingestion.source_metadata import write_source_metadata
from secrets_manager import load_secret_json, secret_path

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
BASE_DIR = Path(__file__).resolve().parents[1]
RAW_DIR = BASE_DIR / "data/raw"
PROCESSED_DIR = BASE_DIR / "data/processed"
PROCESSED_FILE = PROCESSED_DIR / "gmail_ingested.json"
TOKEN_FILE = secret_path("gmail_token", BASE_DIR / "config/gmail_token.json")
CREDENTIALS_FILE = secret_path("google_oauth", BASE_DIR / "config/gmail_oauth.json")
APP_CONFIG_FILE = secret_path("app_config", BASE_DIR / "config/credentials.json")
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".xlsx", ".xls", ".csv"}
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_EMAIL_BODY_CHARS = 12_000


def _load_expected_gmail_account() -> str | None:
    try:
        config = load_secret_json("app_config", BASE_DIR / "config/credentials.json")
    except RuntimeError as exc:
        log.warning("Could not read Gmail account from config: %s", exc)
        return None
    account = str(config.get("gmail_account", "")).strip().lower()
    return account or None


def _load_processed() -> set[str]:
    if not PROCESSED_FILE.exists():
        return set()
    try:
        data = json.loads(PROCESSED_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read processed Gmail ledger; starting fresh: %s", exc)
        return set()
    return set(data.get("attachments", []))


def _save_processed(processed: set[str]) -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = PROCESSED_FILE.with_suffix(".tmp")
    tmp_path.write_text(json.dumps({"attachments": sorted(processed)}, indent=2))
    tmp_path.replace(PROCESSED_FILE)


def _decode_gmail_data(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _header_value(headers: list[dict], name: str) -> str:
    return next(
        (h.get("value", "") for h in headers if h.get("name", "").lower() == name.lower()),
        "",
    )


def _sanitize_filename(filename: str, fallback: str = "attachment") -> str:
    basename = Path(filename.replace("\\", "/")).name.strip()
    if not basename:
        basename = fallback
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", basename)


def _is_inline_noise(part: dict) -> bool:
    filename = part.get("filename", "")
    ext = Path(filename).suffix.lower() if filename else ""
    if ext not in {".jpg", ".jpeg", ".png"}:
        return False

    headers = part.get("headers", [])
    disposition = _header_value(headers, "Content-Disposition").lower()
    mime_type = part.get("mimeType", "")
    size = int(part.get("body", {}).get("size", 0) or 0)
    return "inline" in disposition and mime_type.startswith("image/") and size < 100_000


def _attachment_key(message_id: str, part: dict) -> str:
    body = part.get("body", {})
    attachment_id = body.get("attachmentId")
    filename = _sanitize_filename(part.get("filename", "attachment"))
    size = body.get("size", "")
    return f"{message_id}:{attachment_id or filename}:{size}"


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for i in range(2, 1000):
        candidate = path.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not choose unique filename for {path.name}")


def _write_attachment(path: Path, data: bytes) -> Path:
    if path.exists() and path.read_bytes() == data:
        return path
    out_path = _unique_path(path)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_bytes(data)
    tmp_path.replace(out_path)
    return out_path


def _clean_email_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<br\s*/?>", "\n", text)
    text = re.sub(r"(?s)</p\s*>", "\n", text)
    text = re.sub(r"(?s)<.*?>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _extract_email_body(payload: dict) -> str:
    chunks = []

    def walk(part: dict):
        mime_type = part.get("mimeType", "")
        body = part.get("body", {})
        filename = part.get("filename")
        if filename:
            return
        if part.get("parts"):
            for child in part["parts"]:
                walk(child)
            return
        if mime_type not in {"text/plain", "text/html"}:
            return
        data = body.get("data")
        if not data:
            return
        try:
            text = _decode_gmail_data(data).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return
        chunks.append(_clean_email_text(text))

    walk(payload or {})
    body = "\n\n".join(chunk for chunk in chunks if chunk)
    return body[:MAX_EMAIL_BODY_CHARS]


def _save_email_context(message_id: str, msg: dict, subject: str) -> None:
    payload = msg.get("payload", {})
    headers = payload.get("headers", [])
    body = _extract_email_body(payload)
    context = {
        "message_id": message_id,
        "subject": subject,
        "from": _header_value(headers, "From"),
        "to": _header_value(headers, "To"),
        "date": _header_value(headers, "Date"),
        "snippet": msg.get("snippet", ""),
        "body": body,
    }
    out_path = RAW_DIR / f"{message_id}_email_context.json"
    tmp_path = out_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(context, indent=2, ensure_ascii=False))
    tmp_path.replace(out_path)


def authenticate() -> object:
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
        TOKEN_FILE.chmod(0o600)
    return build("gmail", "v1", credentials=creds)


def validate_mailbox(service) -> None:
    expected = _load_expected_gmail_account()
    if not expected:
        return
    profile = service.users().getProfile(userId="me").execute()
    actual = profile.get("emailAddress", "").strip().lower()
    if actual != expected:
        raise RuntimeError(f"Authenticated Gmail account is {actual}, expected {expected}")


def get_messages(service, max_results: int = 100) -> list:
    query = (
        "has:attachment "
        "-from:no-reply@accounts.google.com "
        "-from:forwarding-noreply@google.com "
        "-from:no-reply@google.com"
    )
    messages = []
    page_token = None
    while len(messages) < max_results:
        page_size = min(100, max_results - len(messages))
        list_kwargs = {"userId": "me", "q": query, "maxResults": page_size}
        if page_token:
            list_kwargs["pageToken"] = page_token
        request = service.users().messages().list(**list_kwargs)
        result = request.execute()
        messages.extend(result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return messages


def download_attachments(service, message_id: str, processed: set[str]) -> list[str]:
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    headers = msg.get("payload", {}).get("headers", [])
    subject = next(
        (h.get("value", "") for h in headers if h.get("name", "").lower() == "subject"),
        "no-subject"
    )
    _save_email_context(message_id, msg, subject)
    saved = []

    def process_parts(parts):
        for part in parts:
            if part.get("parts"):
                process_parts(part["parts"])
            filename = part.get("filename", "")
            ext = Path(filename).suffix.lower() if filename else ""
            if ext not in ALLOWED_EXTENSIONS:
                continue
            if _is_inline_noise(part):
                log.info("Skipped inline image asset: %s (from: %s)", filename, subject)
                continue
            body = part.get("body", {})
            size = int(body.get("size", 0) or 0)
            if size > MAX_ATTACHMENT_BYTES:
                log.warning("Skipped oversized attachment: %s (%s bytes)", filename, size)
                continue
            key = _attachment_key(message_id, part)
            if key in processed:
                log.info("Already ingested: %s (from: %s)", filename, subject)
                continue
            attachment_id = body.get("attachmentId")
            data = None
            if attachment_id:
                attachment = service.users().messages().attachments().get(
                    userId="me", messageId=message_id, id=attachment_id
                ).execute()
                data = _decode_gmail_data(attachment["data"])
            elif body.get("data"):
                data = _decode_gmail_data(body["data"])
            if not data:
                continue
            safe_name = f"{message_id}_{_sanitize_filename(filename)}"
            out_path = _write_attachment(RAW_DIR / safe_name, data)
            write_source_metadata(out_path, {
                "source": "gmail",
                "message_id": message_id,
                "attachment_id": attachment_id,
                "gmail_subject": subject,
                "original_filename": filename,
                "original_relative_source_path": filename,
                "source_path_hint": filename,
            })
            processed.add(key)
            log.info("Downloaded: %s (from: %s)", out_path.name, subject)
            saved.append(str(out_path))

    parts = msg.get("payload", {}).get("parts") or [msg.get("payload", {})]
    process_parts(parts)

    return saved


def run():
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    processed = _load_processed()
    log.info("Authenticating with Gmail...")
    service = authenticate()
    validate_mailbox(service)
    log.info("Fetching messages...")
    messages = get_messages(service)
    log.info(f"Found {len(messages)} matching emails")

    all_files = []
    for msg in messages:
        message_id = msg.get("id")
        if not message_id:
            continue
        try:
            files = download_attachments(service, message_id, processed)
        except (HttpError, OSError, KeyError, ValueError) as exc:
            log.exception("Failed to ingest Gmail message %s: %s", message_id, exc)
            continue
        all_files.extend(files)
        if files:
            _save_processed(processed)

    log.info(f"Total attachments downloaded: {len(all_files)}")
    return all_files
