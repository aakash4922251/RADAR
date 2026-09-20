from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from acquisition.base import AcquisitionBlocked
from acquisition.http import PublicHttpClient, HttpResponse
from acquisition.models import DocumentCandidate


SUPPORTED_EXTENSIONS = {".pdf", ".txt", ".html", ".htm", ".doc", ".docx", ".zip"}


class DocumentDownloadError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def download_document(client: PublicHttpClient, candidate: DocumentCandidate, *, storage_root: str, source_id: str) -> dict:
    response = client.get(candidate.document_url)
    if response.status in (401, 403, 407):
        raise AcquisitionBlocked(f"AUTHENTICATION_REQUIRED_HTTP_{response.status}")
    if response.status == 429:
        raise DocumentDownloadError("RATE_LIMITED", "HTTP 429 after bounded retries")
    if response.status >= 400:
        raise DocumentDownloadError(f"HTTP_{response.status}", f"Document request returned HTTP {response.status}")
    if _looks_like_captcha(response):
        raise AcquisitionBlocked("CAPTCHA_REQUIRED")
    if _looks_like_html_error(response):
        raise DocumentDownloadError("HTML_ERROR_PAGE", "Response was an HTML error or access page")

    content_type = response.content_type
    filename = _safe_filename(candidate.filename or _filename_from_url(candidate.document_url))
    extension = Path(filename).suffix.lower()
    extension = extension if extension in SUPPORTED_EXTENSIONS else _extension_for_body(response.body)
    if extension not in SUPPORTED_EXTENSIONS and not _supported_content_type(content_type):
        raise DocumentDownloadError("UNSUPPORTED_DOCUMENT_TYPE", f"Unsupported content type: {content_type or 'unknown'}")
    if not extension:
        extension = _extension_for_type(content_type)
    if not Path(filename).suffix and extension:
        filename = f"{filename}{extension}"

    digest = hashlib.sha256(response.body).hexdigest()
    destination = Path(storage_root) / source_id / digest[:2] / f"{digest}{extension}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        destination.write_bytes(response.body)
    return {
        "path": str(destination),
        "filename": filename,
        "sha256": digest,
        "content_type": content_type,
        "content_length": len(response.body),
        "http_status": response.status,
        "downloaded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "body": response.body,
    }


def _looks_like_captcha(response: HttpResponse) -> bool:
    if "html" not in response.content_type:
        return False
    body = response.body[:200_000].decode("utf-8", errors="ignore").lower()
    return "captcha" in body or "access denied" in body or "verify you are human" in body


def _looks_like_html_error(response: HttpResponse) -> bool:
    if "html" not in response.content_type and not response.body.lstrip().startswith((b"<!doctype", b"<html", b"<HTML")):
        return False
    body = response.body[:200_000].decode("utf-8", errors="ignore").lower()
    return any(token in body for token in ("error", "not found", "access denied", "captcha", "login"))


def _supported_content_type(content_type: str) -> bool:
    return content_type in {"application/pdf", "text/plain", "text/html", "application/zip", "application/msword", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


def _extension_for_type(content_type: str) -> str:
    return {"application/pdf": ".pdf", "text/plain": ".txt", "text/html": ".html", "application/zip": ".zip"}.get(content_type, ".bin")


def _extension_for_body(body: bytes) -> str:
    if body.startswith(b"%PDF-"):
        return ".pdf"
    if body.startswith(b"PK\x03\x04"):
        return ".zip"
    if body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        return ".html"
    return ""


def _filename_from_url(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0] or "document.bin"


def _safe_filename(filename: str) -> str:
    filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
    return filename[:180] or "document.bin"
