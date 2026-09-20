"""
Procurement event derivation (spec §5).

Deliberately does NOT re-extract anything from raw text. Every event is
derived from evidence Layer 1 already produced (documents, extracted_facts,
contract_timeline_events), so Layer 2 stays consistent with Layer 1 by
construction rather than by convention. Provenance (document_id and, where
applicable, source_fact_id) is preserved on every event.
"""
from __future__ import annotations

from db import database as db

# doc_type -> event_type this document type is evidence for. A single
# document can produce more than one event (e.g. an NIT is evidence for
# 'tender_published'; an AOC is evidence for 'tender_awarded').
_DOC_TYPE_TO_EVENT = {
    "nit": "tender_published",
    "bid_document": "tender_published",
    "aoc": "tender_awarded",
    "loa": "tender_awarded",
    "termination_notice": "contract_ended",
}


def derive_events_for_contract(cur, contract_id: int) -> list[int]:
    """
    Scans this contract's documents and facts and inserts (idempotently)
    the procurement_events they are evidence for. Returns the list of
    event ids touched (inserted or already-existing).
    """
    event_ids: list[int] = []

    documents = db.list_documents_for_contract(cur, contract_id)
    for doc in documents:
        event_type = _DOC_TYPE_TO_EVENT.get(doc["doc_type"])
        if event_type and doc["doc_date"]:
            eid = db.insert_procurement_event(
                cur, contract_id=contract_id, event_type=event_type, event_date=doc["doc_date"],
                document_id=doc["id"], notes=f"Derived from {doc['doc_type']} document.",
            )
            event_ids.append(eid)

    facts = db.get_facts_for_contract(cur, contract_id)
    for f in facts:
        if f["fact_type"] == "award_date":
            eid = db.insert_procurement_event(
                cur, contract_id=contract_id, event_type="tender_awarded", event_date=f["fact_value"],
                document_id=f["document_id"], source_fact_id=f["id"],
                notes="Derived from award_date fact.",
            )
            event_ids.append(eid)
        elif f["fact_type"] in ("commencement_date_explicit", "site_handover_date", "work_order_date"):
            eid = db.insert_procurement_event(
                cur, contract_id=contract_id, event_type="contract_started", event_date=f["fact_value"],
                document_id=f["document_id"], source_fact_id=f["id"],
                notes=f"Derived from {f['fact_type']} fact.",
            )
            event_ids.append(eid)
        elif f["fact_type"] == "extension_exercised_new_end_date":
            eid = db.insert_procurement_event(
                cur, contract_id=contract_id, event_type="contract_extended", event_date=f["fact_value"],
                document_id=f["document_id"], source_fact_id=f["id"],
                notes="Derived from extension_exercised_new_end_date fact.",
            )
            event_ids.append(eid)
        elif f["fact_type"] == "termination_date":
            eid = db.insert_procurement_event(
                cur, contract_id=contract_id, event_type="contract_ended", event_date=f["fact_value"],
                document_id=f["document_id"], source_fact_id=f["id"],
                notes="Derived from termination_date fact.",
            )
            event_ids.append(eid)

    # contract_ended from the contract's own resolved expiry, once it's
    # actually in the past — this is a DERIVED signal from Layer 1's own
    # calculation, not a new extraction, and is safe to re-derive on every
    # call (idempotent insert keys on contract_id+event_type+event_date+document_id).
    contract = db.get_contract(cur, contract_id)
    if contract and contract["current_expiry_estimate"]:
        from datetime import date
        try:
            expiry = date.fromisoformat(contract["current_expiry_estimate"])
            if expiry <= date.today():
                eid = db.insert_procurement_event(
                    cur, contract_id=contract_id, event_type="contract_ended",
                    event_date=contract["current_expiry_estimate"], document_id=None,
                    notes="Derived from contract's own resolved current_expiry_estimate (in the past).",
                )
                event_ids.append(eid)
        except ValueError:
            pass

    return event_ids
