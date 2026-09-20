from core import pipeline
from core.procurement_events import derive_events_for_contract
from db import database as db


def test_nit_and_aoc_produce_tender_published_and_awarded_events(cur):
    r1 = pipeline.ingest_document(
        cur, raw_text="AMC for a period of 2 years.", doc_type="nit",
        contract_title="Event Derivation Test", doc_date="2024-01-01",
    )
    r2 = pipeline.ingest_document(
        cur, raw_text="Award Date: 01-Feb-2024.", doc_type="aoc", contract_id=r1.contract_id,
    )
    events = db.list_events_for_contract(cur, r1.contract_id)
    event_types = {e["event_type"] for e in events}
    assert "tender_published" in event_types
    assert "tender_awarded" in event_types

    # provenance must be traceable
    for e in events:
        assert e["document_id"] is not None or e["notes"]


def test_commencement_fact_produces_contract_started_event(cur):
    r1 = pipeline.ingest_document(
        cur, raw_text="AMC for a period of 2 years.", doc_type="nit",
        contract_title="Commencement Event Test",
    )
    pipeline.ingest_document(
        cur, raw_text="Work order. The work shall commence on 01-Mar-2024.",
        doc_type="work_order", contract_id=r1.contract_id,
    )
    events = db.list_events_for_contract(cur, r1.contract_id)
    assert any(e["event_type"] == "contract_started" and e["event_date"] == "2024-03-01" for e in events)


def test_extension_exercised_produces_contract_extended_event(cur):
    r1 = pipeline.ingest_document(
        cur, raw_text="AMC for a period of 2 years, renewable subject to performance.",
        doc_type="nit", contract_title="Extension Event Test",
    )
    pipeline.ingest_document(cur, raw_text="Award Date: 01-Jan-2024.", doc_type="aoc", contract_id=r1.contract_id)
    pipeline.ingest_document(
        cur, raw_text="Work order. The work shall commence on 01-Feb-2024.",
        doc_type="work_order", contract_id=r1.contract_id,
    )
    pipeline.ingest_document(
        cur, raw_text=("Work order. The contract period has been extended vide this work order "
                        "dated 15-Jan-2026; new end date shall be 01-Mar-2026."),
        doc_type="work_order", contract_id=r1.contract_id,
    )
    events = db.list_events_for_contract(cur, r1.contract_id)
    assert any(e["event_type"] == "contract_extended" and e["event_date"] == "2026-03-01" for e in events)


def test_re_deriving_events_is_idempotent(cur):
    r1 = pipeline.ingest_document(
        cur, raw_text="AMC for a period of 2 years.", doc_type="nit",
        contract_title="Idempotency Test", doc_date="2024-01-01",
    )
    events_before = db.list_events_for_contract(cur, r1.contract_id)
    derive_events_for_contract(cur, r1.contract_id)
    derive_events_for_contract(cur, r1.contract_id)
    events_after = db.list_events_for_contract(cur, r1.contract_id)
    assert len(events_before) == len(events_after), "re-deriving events must not create duplicates"
