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
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class IngestedDocument:
    raw_text: str
    raw_text_full_hash: str
    page_count: Optional[int] = None
    used_ocr: bool = False
    warning: Optional[str] = None   # e.g. a photo too small to read reliably


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def ingest_text(text: str) -> IngestedDocument:
    text = text.strip()
    return IngestedDocument(raw_text=text, raw_text_full_hash=hash_text(text))


# Text that eProcure stamps on scanned PDFs when they are digitally signed.
# A PDF whose only text is this stamp is still an image-only document.
_SIGNATURE_STAMP_RE = re.compile(
    r"^\s*(digitally\s+signed\s+by.*|signature\s+not\s+verified|date\s*:\s*\d{4}\.\d{2}\.\d{2}.*|"
    r"location\s*:.*|reason\s*:.*|page\s*\d+\s*(of\s*\d+)?)\s*$",
    re.I | re.M,
)
_MIN_REAL_TEXT_CHARS = 120

_TESSERACT_WINDOWS_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)


def looks_scanned(text: str) -> bool:
    """True when a PDF's text layer is empty or only a signature stamp, i.e.
    the content is an image that needs OCR. A short but genuine text PDF is
    not "scanned"."""
    if not (text or "").strip():
        return True
    if not _SIGNATURE_STAMP_RE.search(text):
        return False
    remaining = _SIGNATURE_STAMP_RE.sub("", text)
    return len(re.sub(r"\s+", "", remaining)) < _MIN_REAL_TEXT_CHARS


def _point_pytesseract_at_install() -> None:
    """Windows installers do not add Tesseract to PATH; use the standard location."""
    import shutil
    import pytesseract

    if not shutil.which("tesseract"):
        for path in _TESSERACT_WINDOWS_PATHS:
            if os.path.exists(path):
                pytesseract.pytesseract.tesseract_cmd = path
                return


def _ocr_pdf(pdf_bytes: bytes, dpi: int = 200) -> str:
    """OCR every page. Pages are rendered with PyMuPDF (no Poppler needed),
    falling back to pdf2image; recognition needs the Tesseract program."""
    import pytesseract

    _point_pytesseract_at_install()
    images = []
    try:
        import fitz
        from PIL import Image
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        for page in doc:
            pix = page.get_pixmap(dpi=dpi)
            images.append(Image.open(io.BytesIO(pix.tobytes("png"))))
        doc.close()
    except Exception:
        from pdf2image import convert_from_bytes
        images = convert_from_bytes(pdf_bytes, dpi=dpi)
    return "\f".join(pytesseract.image_to_string(img) for img in images)


def ocr_available() -> bool:
    try:
        import pytesseract
        import shutil
        if shutil.which("tesseract") or any(os.path.exists(p) for p in _TESSERACT_WINDOWS_PATHS):
            return True
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


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

    # 3. OCR fallback for scanned/image-only PDFs. Some eProcure PDFs contain
    # only a digital-signature text layer, so a non-empty text layer can still
    # be effectively image-only. OCR when extracted text is suspiciously short.
    if looks_scanned(text):
        try:
            ocr_text = _ocr_pdf(pdf_bytes)
            if ocr_text.strip():
                page_count = page_count or ocr_text.count("\f") or None
                text = ocr_text
                used_ocr = True
        except Exception:
            # OCR stack not available / failed — return whatever we have
            # (possibly empty or signature-only). Callers check looks_scanned()
            # and report "scanned, OCR unavailable" instead of proceeding.
            pass

    text = text.strip()
    return IngestedDocument(
        raw_text=text, raw_text_full_hash=hash_text(text), page_count=page_count, used_ocr=used_ocr
    )


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
TEXT_EXTENSIONS = {".txt", ".csv"}
OFFICE_EXTENSIONS = {".docx", ".xlsx"}
LEGACY_OFFICE_EXTENSIONS = {".doc", ".xls"}
SUPPORTED_EXTENSIONS = {".pdf"} | IMAGE_EXTENSIONS | TEXT_EXTENSIONS | OFFICE_EXTENSIONS
MIN_PHOTO_PIXELS = 1200  # long side; an A4 page below this is under ~100 dpi


def _prepare_photo(img):
    """Make a phone photo or scan OCR-friendly: apply the camera's rotation
    flag, turn the page upright if Tesseract detects it sideways, and convert
    to greyscale with stretched contrast."""
    from PIL import ImageOps
    import pytesseract

    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    try:
        osd = pytesseract.image_to_osd(img, output_type=pytesseract.Output.DICT)
        if osd.get("rotate") and osd.get("orientation_conf", 0) >= 2:
            img = img.rotate(-osd["rotate"], expand=True)
    except Exception:
        pass  # too little text to judge orientation; read it as it is
    gray = ImageOps.autocontrast(ImageOps.grayscale(img))
    # Small photos read badly; upscale so text is roughly 300 dpi-sized.
    if max(gray.size) < 2000:
        scale = 2000 / max(gray.size)
        gray = gray.resize((int(gray.width * scale), int(gray.height * scale)))
    return gray


def ingest_image_bytes(image_bytes: bytes) -> IngestedDocument:
    """OCR a photo or scanned image (JPG, PNG, TIFF incl. multi-page, ...)."""
    from PIL import Image, ImageSequence
    import pytesseract

    _point_pytesseract_at_install()
    pages, smallest = [], None
    with Image.open(io.BytesIO(image_bytes)) as img:
        for frame in ImageSequence.Iterator(img):
            smallest = min(smallest or max(frame.size), max(frame.size))
            pages.append(pytesseract.image_to_string(_prepare_photo(frame.copy())))
    text = "\f".join(pages).strip()
    warning = None
    if smallest is not None and smallest < MIN_PHOTO_PIXELS:
        warning = (f"Photo resolution is low ({smallest} px on the long side); dates and numbers may be misread. "
                   "Retake it closer, or upload the original PDF.")
    return IngestedDocument(raw_text=text, raw_text_full_hash=hash_text(text), page_count=len(pages),
                            used_ocr=True, warning=warning)


def _office_text(file_bytes: bytes, ext: str) -> str:
    if ext == ".docx":
        import docx
        document = docx.Document(io.BytesIO(file_bytes))
        parts = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text.strip() for cell in row.cells))
        return "\n".join(parts)
    import openpyxl
    book = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    lines = []
    for sheet in book.worksheets:
        for row in sheet.iter_rows(values_only=True):
            cells = [str(c) for c in row if c is not None and str(c).strip()]
            if cells:
                lines.append(" | ".join(cells))
    return "\n".join(lines)


def ingest_uploaded_file(file_bytes: bytes, filename: str) -> IngestedDocument:
    ext = os.path.splitext(filename)[1].lower()
    if ext == ".pdf":
        return ingest_pdf_bytes(file_bytes)
    if ext in IMAGE_EXTENSIONS:
        return ingest_image_bytes(file_bytes)
    if ext in OFFICE_EXTENSIONS:
        return ingest_text(_office_text(file_bytes, ext))
    if ext in LEGACY_OFFICE_EXTENSIONS:
        raise ValueError(f"Old {ext} format is not supported; save it as {ext}x or PDF and upload again.")
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
