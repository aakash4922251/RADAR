import pytest

from core import pipeline
from db import database as db


def test_end_to_end_happy_path_produces_medium_confidence_like_spec_example(cur):
    nit_text = "AMC of Fire Fighting System, Terminal Building. AMC for a period of 2 years."
    aoc_text = "Award of Contract. Award Date: 14-Mar-2025."

    r1 = pipeline.ingest_document(cur, raw_text=nit_text, doc_type="nit",
                                   contract_title="AMC of Fire Fighting System")
    r2 = pipeline.ingest_document(cur, raw_text=aoc_text, doc_type="aoc",
                                   contract_id=r1.contract_id)

    contract = db.get_contract(cur, r1.contract_id)
    assert contract["current_expiry_estimate"] == "2027-03-14"
    assert r2.expiry_confidence == "MEDIUM"
    assert r2.status == "VERIFIED"


def test_duplicate_document_is_deduplicated_by_hash(cur):
    text = "AMC for a period of 2 years. Award Date: 01-Jan-2024."
    r1 = pipeline.ingest_document(cur, raw_text=text, doc_type="nit", contract_title="Dedup Test")
    r2 = pipeline.ingest_document(cur, raw_text=text, doc_type="nit", contract_id=r1.contract_id)
    assert r2.deduplicated is True
    assert r2.document_id == r1.document_id
    docs = db.list_documents_for_contract(cur, r1.contract_id)
    assert len(docs) == 1


def test_bid_deadline_corrigendum_never_alters_expiry(cur):
    nit_text = "AMC for a period of 2 years. Award Date: 01-Jan-2024."
    r1 = pipeline.ingest_document(cur, raw_text=nit_text, doc_type="nit", contract_title="Corrigendum Non-Event Test")
    before = db.get_contract(cur, r1.contract_id)["current_expiry_estimate"]

    corrigendum_text = ("The last date for submission of bids is hereby extended "
                         "up to 15.02.2024. All other terms remain unchanged.")
    r2 = pipeline.ingest_document(cur, raw_text=corrigendum_text, doc_type="corrigendum",
                                   contract_id=r1.contract_id)
    after = db.get_contract(cur, r1.contract_id)["current_expiry_estimate"]

    assert before == after, "a bid-deadline corrigendum must never change the expiry estimate"

    events = db.list_timeline_for_contract(cur, r1.contract_id)
    assert any(e["event_type"] == "corrigendum_bid_deadline_only" for e in events)
    assert not any(e["event_type"] == "corrigendum_substantive" for e in events)


def test_substantive_corrigendum_is_flagged_for_review(cur):
    nit_text = "AMC for a period of 2 years. Award Date: 01-Jan-2024."
    r1 = pipeline.ingest_document(cur, raw_text=nit_text, doc_type="nit", contract_title="Substantive Corrigendum Test")

    corrigendum_text = ("This corrigendum revises the contract period of the agreement; "
                         "the duration is hereby extended by 6 months.")
    r2 = pipeline.ingest_document(cur, raw_text=corrigendum_text, doc_type="corrigendum",
                                   contract_id=r1.contract_id)

    events = db.list_timeline_for_contract(cur, r1.contract_id)
    assert any(e["event_type"] == "corrigendum_substantive" for e in events)


def test_missing_start_date_and_duration_yields_unknown_status(cur):
    r1 = pipeline.ingest_document(
        cur, raw_text="This is a generic notice with no extractable dates or durations at all.",
        doc_type="department_page", contract_title="No Evidence Contract",
    )
    contract = db.get_contract(cur, r1.contract_id)
    assert contract["current_expiry_estimate"] is None
    assert contract["status"] == "INSUFFICIENT_EVIDENCE"


def test_empty_document_raises_instead_of_silently_ingesting(cur):
    with pytest.raises(ValueError):
        pipeline.ingest_document(cur, raw_text="   ", doc_type="nit", contract_title="Empty Doc Test")


def test_extension_exercised_via_work_order_updates_estimate_not_ceiling_only(cur):
    nit = "This NIT is for AMC services for a period of 2 years, renewable subject to performance."
    aoc = "Award Date: 10-Jan-2026."
    wo1 = "Work order. The work shall commence on 01-Feb-2026."
    wo2 = ("Work order. The contract period has been extended vide this work order "
           "dated 15-Feb-2028; new end date shall be 31-Mar-2028.")

    r1 = pipeline.ingest_document(cur, raw_text=nit, doc_type="nit", contract_title="Extension E2E Test")
    pipeline.ingest_document(cur, raw_text=aoc, doc_type="aoc", contract_id=r1.contract_id)
    pipeline.ingest_document(cur, raw_text=wo1, doc_type="work_order", contract_id=r1.contract_id)

    contract = db.get_contract(cur, r1.contract_id)
    assert contract["current_expiry_estimate"] == "2028-02-01"

    pipeline.ingest_document(cur, raw_text=wo2, doc_type="work_order", contract_id=r1.contract_id)
    contract = db.get_contract(cur, r1.contract_id)
    assert contract["current_expiry_estimate"] == "2028-03-31"
    assert contract["status"] != "CONFLICTING_EVIDENCE"
