"""
Document ingestion.

Supports:
  - Plain text (.txt) — used for pasted clauses / OCR output
  - PDF, via PyMuPDF (fitz) with a pdfplumber fallback
  - OCR fallback for scanned/image-only PDFs, via pytesseract, if the
    PDF has no extractable text layer at all
  - Public-URL fetch is intentionally NOT auto-scraped in this MVP (see
    README "Source access" section) — the adapter below is isolated so a
    future connector can slot in without touching the extraction pipeline.

Every document is hashed (sha256 of full raw text) so identical
documents ingested twice are deduplicated per spec §1.
"""
from __future__ import annotations

import hashlib
import io
import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class IngestedDocument:
    raw_text: str
    raw_text_full_hash: str
    page_count: Optional[int] = None
    used_ocr: bool = False


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def ingest_text(text: str) -> IngestedDocument:
    text = text.strip()
    return IngestedDocument(raw_text=text, raw_text_full_hash=hash_text(text))


def ingest_pdf_bytes(pdf_bytes: bytes) -> IngestedDocument:
    text = ""
    page_count = None
    used_ocr = False

    # 1. Try PyMuPDF first (fast, good layout handling)
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        page_count = doc.page_count
        text = "\n".join(page.get_text() for page in doc)
        doc.close()
    except Exception:
        text = ""

    # 2. Fallback to pdfplumber if PyMuPDF produced nothing usable
    if not text.strip():
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                page_count = len(pdf.pages)
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        except Exception:
            text = ""

    # 3. OCR fallback for scanned/image-only PDFs
    if not text.strip():
        try:
            from pdf2image import convert_from_bytes
            import pytesseract
            images = convert_from_bytes(pdf_bytes)
            page_count = page_count or len(images)
            ocr_text = []
            for img in images:
                ocr_text.append(pytesseract.image_to_string(img))
            text = "\n".join(ocr_text)
            used_ocr = True
        except Exception:
            # OCR stack not available / failed — return whatever we have
            # (possibly empty). Caller must handle empty text as
            # "extraction failed", not silently proceed.
            pass

    text = text.strip()
    return IngestedDocument(
        raw_text=text, raw_text_full_hash=hash_text(text), page_count=page_count, used_ocr=used_ocr
    )


def ingest_uploaded_file(file_bytes: bytes, filename: str) -> IngestedDocument:
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".pdf":
        return ingest_pdf_bytes(file_bytes)
    # treat everything else as text
    try:
        text = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = file_bytes.decode("latin-1", errors="ignore")
    return ingest_text(text)


def fetch_public_url(url: str) -> IngestedDocument:
    """
    Isolated adapter for future public-source ingestion.

    Deliberately NOT implemented with a live scraper in this MVP: the
    build instructions require respecting robots.txt/rate limits and
    forbid bypassing auth/CAPTCHA/anti-bot systems, and "faking" a
    government API is explicitly disallowed. Rather than ship a fragile
    or non-compliant scraper, this MVP routes all ingestion through the
    document-upload workflow (ingest_uploaded_file). Swap this function's
    body for a real, robots.txt-respecting fetcher when a specific,
    compliant source integration is scoped.
    """
    raise NotImplementedError(
        "Public-URL ingestion is not enabled in this MVP. Please download the "
        "document and use the upload workflow instead. See README 'Source access'."
    )
