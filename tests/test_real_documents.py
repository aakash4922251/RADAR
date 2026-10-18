"""Regression tests from real CPPP documents a user uploaded (Oct 2026).

Each test pins a bug those documents exposed:
* a tender notice's bid-download window ("from 31.08.2026 to 28.09.2026") was
  read as the contract period and shown as a HIGH-confidence end date;
* "LOA" inside "Download" was read as a Letter of Award date;
* the portal banner was taken as the buyer and fee/EMD amounts as contract
  values, making a plain tender look "conflicting";
* the CPPP award summary's "Work Completion Period in days : 30" was missed.
"""
from datetime import date
from pathlib import Path

import pytest

from core.contract_tracker import portfolio, tender_keys, track_documents
from core.extraction import extract_all_facts
from core.ingestion import ingest_uploaded_file, looks_scanned
from db import database as db

DOCS = Path(__file__).parent / "fixtures" / "real_docs"
NOTICE = DOCS / "morth_nh66_tender_notice.pdf"
DETAILS = DOCS / "morth_nh66_tender_details.pdf"
AOC = DOCS / "vnit_aoc_summary.pdf"
SCANNED_WO = Path(__file__).parent / "fixtures" / "WONEWELECTRIFICATIONATNEWLYPROPOSEDFACULTYROOMSNEWMETALLURGICAL.pdf"

pytestmark = pytest.mark.skipif(not NOTICE.exists(), reason="real document fixtures not present")


def _text(path):
    return ingest_uploaded_file(path.read_bytes(), path.name).raw_text


def _facts(path):
    return {(f.fact_type, f.value) for f in extract_all_facts(_text(path))}


def test_tender_notice_has_no_end_date(cur):
    facts = _facts(NOTICE)
    assert not any(t == "base_duration_explicit_end_date" for t, _ in facts)  # bid-download window rejected
    assert not any(t == "loa_date" for t, _ in facts)                         # "Download" is not an LOA
    assert ("base_duration_months", "6") in facts                             # "06 Months" work period
    result = track_documents(cur, [(NOTICE.name, NOTICE.read_bytes())])[0]
    assert result.end_date is None


def test_tender_documents_link_by_reference_and_stay_tender_stage(cur):
    results = track_documents(cur, [(NOTICE.name, NOTICE.read_bytes()), (DETAILS.name, DETAILS.read_bytes())])
    assert results[0].contract_id == results[1].contract_id
    assert "tender reference" in results[1].message
    row = portfolio(cur).iloc[0]
    assert row["stage"] == "TENDER"
    assert row["status"] == "INSUFFICIENT_EVIDENCE"     # not CONFLICTING: no real contradiction
    assert row["buyer"].startswith("Ministry of Road Transport and Highways")
    assert row["title"].startswith("Repairs to damaged Service road")
    values = {r["fact_value"] for r in cur.execute(
        "SELECT fact_value FROM extracted_facts WHERE fact_type = 'contract_value'")}
    assert values == {"3625764.0"}                       # fees, EMD and GST are not contract values


def test_cppp_award_summary_gives_end_date(cur):
    result = track_documents(cur, [(AOC.name, AOC.read_bytes())])[0]
    assert result.doc_type == "aoc"
    assert result.end_date == "2026-10-21"               # contract date 21-Sep-2026 + 30 days
    assert result.expiry_confidence == "MEDIUM"          # start is the contract date, used as a proxy
    row = portfolio(cur, today=date(2026, 10, 18)).iloc[0]
    assert "Visvesvaraya National Institute of Technology" in row["buyer"]
    assert row["days_left"] == 3


def test_tender_keys_read_ids_and_references():
    assert tender_keys(_text(DETAILS)) == ["2026_morth_924179_1", "06/nhd/rtn/ab/tc/2165/for2026-27"]
    assert tender_keys(_text(NOTICE)) == ["06/nhd/rtn/ab/tc/2165/for2026-27"]


@pytest.mark.skipif(not SCANNED_WO.exists(), reason="scanned work order fixture not present")
def test_scanned_work_order_is_reported_not_saved_empty(cur, monkeypatch):
    import core.contract_tracker as tracker
    import core.ingestion as ingestion

    monkeypatch.setattr(ingestion, "_ocr_pdf", lambda data, dpi=200: (_ for _ in ()).throw(RuntimeError("no OCR")))
    monkeypatch.setattr(tracker, "ocr_available", lambda: False)
    assert looks_scanned(ingestion.ingest_pdf_bytes(SCANNED_WO.read_bytes()).raw_text)
    result = track_documents(cur, [(SCANNED_WO.name, SCANNED_WO.read_bytes())])[0]
    assert result.status == "FAILED" and "Scanned image" in result.message
    assert db.list_contracts(cur) == []


# The same work order as typed text (what OCR produces), to pin the work-order rules.
WORK_ORDER_TEXT = """VISVESVARAYA NATIONAL INSTITUTE OF TECHNOLOGY, SOUTH AMBAZARI ROAD, NAGPUR
WORK ORDER
Ref. No.: VNIT/STR/ELM/RJS/2026-27/97/4743
Work order dated 16 SEP 2026
To, Bhagyshree Electrical,
Sub.: Work Order for New Electrification Work at Newly Proposed Faculty Rooms Adjacent to Conference Room of New Metallurgical and Materials Engineering Department at VNIT Campus, Nagpur.
3) Delivery Period: Work to be delivered/completed within 20 Days from the date of work order.
6) Warranty/Guarantee: Normal comprehensive Warrantee/Guarantee for 01 Year shall be applicable for all items.
"""


def test_work_order_completion_term_counts_days_from_work_order(cur):
    result = track_documents(cur, [("wo.txt", WORK_ORDER_TEXT.encode())])[0]
    assert result.doc_type == "work_order"
    assert result.end_date == "2026-10-06"               # 16 Sep + 20 days, not + 1 month, not + warranty
    assert result.contract_title.startswith("Work Order for New Electrification Work")


def test_work_order_outranks_award_summary_and_keeps_the_superseded_term(cur):
    results = track_documents(cur, [(AOC.name, AOC.read_bytes()), ("wo.txt", WORK_ORDER_TEXT.encode())],
                              same_contract=True)
    contract_id = results[0].contract_id
    # The portal summary says 30 days from 21 Sep; the work order says 20 days
    # from 16 Sep. A work order outranks an award summary (spec §4.3), so its
    # term wins, and the 30-day figure stays on record as superseded evidence.
    assert db.get_contract(cur, contract_id)["current_expiry_estimate"] == "2026-10-06"
    durations = {(f["fact_value"], f["superseded_by_fact_id"] is not None)
                 for f in db.get_facts_for_contract(cur, contract_id, include_superseded=True)
                 if f["fact_type"] == "base_duration_months"}
    assert durations == {("0.99", True), ("0.66", False)}


def test_reupload_rereads_a_document_added_under_older_rules(cur):
    from core import pipeline

    # What the database held after the first upload: typed as a tender notice,
    # and the old extractor never read the 30-day completion period.
    first = pipeline.ingest_document(cur, raw_text=_text(AOC), doc_type="nit",
                                     contract_title="Test AOC 1", doc_title=AOC.name)
    cur.execute("DELETE FROM extracted_facts WHERE fact_type = 'base_duration_months'")
    pipeline.recalculate_contract(cur, first.contract_id)
    assert db.get_contract(cur, first.contract_id)["current_expiry_estimate"] is None

    again = track_documents(cur, [(AOC.name, AOC.read_bytes())])[0]
    assert again.status == "ALREADY_TRACKED" and "read again" in again.message
    assert again.contract_id == first.contract_id
    assert again.doc_type == "aoc"
    assert again.end_date == "2026-10-21"


def test_reread_all_clears_a_stale_wrong_end_date(cur):
    from core import pipeline
    from core.contract_tracker import reread_all

    first = pipeline.ingest_document(cur, raw_text=_text(NOTICE), doc_type="nit",
                                     contract_title="Tendernotice 1 (1)", doc_title=NOTICE.name)
    # The old extractor read the bid-download window as the contract period.
    db.insert_fact(cur, contract_id=first.contract_id, document_id=first.document_id,
                   fact_type="base_duration_explicit_end_date", fact_value="2026-08-31|2026-09-28",
                   evidence_quote="from 31.08.2026 to 28.09.2026", context_window="", char_offset_start=0,
                   char_offset_end=0, extraction_rule_id="P_EXPLICIT_RANGE_v1", fact_confidence="HIGH")
    pipeline.recalculate_contract(cur, first.contract_id)
    assert db.get_contract(cur, first.contract_id)["current_expiry_estimate"] == "2026-09-28"

    assert reread_all(cur) == 1
    assert db.get_contract(cur, first.contract_id)["current_expiry_estimate"] is None


def _photo_of_scan(rotate=0, scale=1.0, quality=85):
    """A JPEG 'phone photo' of page 1 of the scanned VNIT work order."""
    import io
    import fitz
    from PIL import Image

    doc = fitz.open(str(SCANNED_WO))
    img = Image.open(io.BytesIO(doc[0].get_pixmap(dpi=150).tobytes("png"))).convert("RGB")
    if scale != 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    if rotate:
        img = img.rotate(rotate, expand=True)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


needs_ocr = pytest.mark.skipif(not SCANNED_WO.exists() or not __import__("core.ingestion").ingestion.ocr_available(),
                               reason="needs the scanned fixture and OCR (Tesseract)")


@needs_ocr
@pytest.mark.parametrize("rotate", [0, 90])
def test_photo_of_work_order_upright_or_sideways(cur, rotate):
    result = track_documents(cur, [("wo_photo.jpg", _photo_of_scan(rotate=rotate))])[0]
    assert result.status == "ADDED" and result.doc_type == "work_order"
    assert result.end_date == "2026-10-06"           # 16 Sep 2026 + 20 days
    assert portfolio(cur).iloc[0]["vendor"] == "Bhagyshree Electrical"


@needs_ocr
def test_low_resolution_photo_carries_a_warning(cur):
    result = track_documents(cur, [("tiny.jpg", _photo_of_scan(scale=0.5, quality=40))])[0]
    assert "resolution is low" in result.message


@needs_ocr
def test_scanned_work_order_pdf_reads_with_ocr(cur):
    result = track_documents(cur, [(SCANNED_WO.name, SCANNED_WO.read_bytes())])[0]
    assert result.end_date == "2026-10-06"
    assert result.expiry_confidence == "MEDIUM"      # work-order date is a stated proxy for the start
