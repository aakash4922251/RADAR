import io

from core import ingestion


def _make_pdf_bytes(lines: list[str]) -> bytes:
    """Builds a real, minimal PDF using reportlab so ingestion tests exercise
    actual PDF parsing rather than mocking it away."""
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    y = 750
    for line in lines:
        c.drawString(100, y, line)
        y -= 20
    c.save()
    return buf.getvalue()


def test_ingest_text_hashes_and_strips():
    doc = ingestion.ingest_text("  AMC for a period of 2 years.  \n")
    assert doc.raw_text == "AMC for a period of 2 years."
    assert len(doc.raw_text_full_hash) == 64  # sha256 hex digest length


def test_identical_text_produces_identical_hash():
    a = ingestion.ingest_text("AMC for a period of 2 years.")
    b = ingestion.ingest_text("AMC for a period of 2 years.")
    assert a.raw_text_full_hash == b.raw_text_full_hash


def test_different_text_produces_different_hash():
    a = ingestion.ingest_text("AMC for a period of 2 years.")
    b = ingestion.ingest_text("AMC for a period of 3 years.")
    assert a.raw_text_full_hash != b.raw_text_full_hash


def test_ingest_real_pdf_extracts_text_via_fallback_chain():
    """
    Exercises the actual PyMuPDF -> pdfplumber -> OCR fallback chain
    against a real, reportlab-generated PDF (not a mock), so a broken
    extraction path would be caught here even if PyMuPDF happens to be
    unavailable in a given environment (pdfplumber is the fallback).
    """
    pdf_bytes = _make_pdf_bytes([
        "AMC of Fire Fighting System, Terminal Building.",
        "AMC for a period of 2 years.",
        "Award Date: 14-Mar-2025.",
    ])
    doc = ingestion.ingest_pdf_bytes(pdf_bytes)
    assert doc.raw_text.strip(), "expected non-empty extracted text from a real PDF"
    assert "period of 2 years" in doc.raw_text
    assert "14-Mar-2025" in doc.raw_text
    assert doc.used_ocr is False
    assert doc.page_count == 1


def test_ingest_uploaded_file_routes_pdf_extension_to_pdf_path():
    pdf_bytes = _make_pdf_bytes(["Contract duration: 12 months."])
    doc = ingestion.ingest_uploaded_file(pdf_bytes, "notice.pdf")
    assert "12 months" in doc.raw_text


def test_ingest_uploaded_file_routes_txt_extension_to_text_path():
    doc = ingestion.ingest_uploaded_file(b"Contract duration: 12 months.", "notice.txt")
    assert doc.raw_text == "Contract duration: 12 months."


def test_extraction_pipeline_runs_end_to_end_on_real_pdf_text(cur):
    """Full loop: real PDF -> extracted text -> pipeline ingestion -> facts."""
    from core import pipeline
    pdf_bytes = _make_pdf_bytes([
        "AMC of Fire Fighting System, Terminal Building.",
        "AMC for a period of 2 years.",
    ])
    result = pipeline.ingest_document(
        cur, raw_bytes=pdf_bytes, filename="nit.pdf", doc_type="nit",
        contract_title="PDF Ingestion Test Contract",
    )
    assert result.n_facts_extracted >= 1


def test_public_url_ingestion_is_not_silently_faked():
    import pytest
    with pytest.raises(NotImplementedError):
        ingestion.fetch_public_url("https://eprocure.gov.in/example")


def test_empty_pdf_yields_empty_text_not_a_crash():
    # A PDF with no drawable content at all
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.showPage()
    c.save()
    doc = ingestion.ingest_pdf_bytes(buf.getvalue())
    assert doc.raw_text == ""
