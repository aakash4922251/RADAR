"""
End-to-end pipeline, per the MVP priority order in the build spec:

  document -> extract text -> extract facts -> capture evidence ->
  resolve start date -> resolve duration -> calculate expiry ->
  calculate confidence -> generate Proof Packet -> save to SQLite ->
  display in Streamlit
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core import extraction, ingestion, reconciliation, matching
from core.expiry_engine import compute_expiry
from core.confidence import assess_confidence, derive_status
from core.corrigendum import classify_corrigendum, is_duration_relevant
from db import database as db


@dataclass
class IngestResult:
    document_id: int
    contract_id: int
    n_facts_extracted: int
    deduplicated: bool
    reconciliation_summary: dict
    status: str
    expiry_confidence: str
    similar_contracts_warning: Optional[list] = None
    requirement_link: Optional[object] = None


def ingest_document(
    cur,
    *,
    raw_bytes: Optional[bytes] = None,
    raw_text: Optional[str] = None,
    filename: Optional[str] = None,
    doc_type: str,
    contract_id: Optional[int] = None,
    contract_title: Optional[str] = None,
    source_url: Optional[str] = None,
    source_org: Optional[str] = None,
    doc_title: Optional[str] = None,
    doc_date: Optional[str] = None,
    page_number: Optional[int] = None,
) -> IngestResult:
    """
    Full ingest of one document. If contract_id is None, a new contract
    is created using contract_title (evidence-first: we do not try to
    fuzzy-match to an existing contract in this MVP beyond exact title
    match — see README "Known limitations").
    """
    if raw_bytes is not None:
        ingested = ingestion.ingest_uploaded_file(raw_bytes, filename or "upload.txt")
    elif raw_text is not None:
        ingested = ingestion.ingest_text(raw_text)
    else:
        raise ValueError("Must supply raw_bytes or raw_text")

    if not ingested.raw_text.strip():
        raise ValueError(
            "No extractable text found in this document (PDF may be scanned and OCR "
            "unavailable, or the file is empty). Ingestion aborted — nothing was saved."
        )

    existing = db.find_document_by_hash(cur, ingested.raw_text_full_hash)
    deduplicated = existing is not None
    similar_warning = None
    if deduplicated:
        document_id = existing["id"]
        contract_id = existing["contract_id"] or contract_id
    else:
        if contract_id is None:
            if not contract_title:
                raise ValueError("contract_title required when creating a new contract")
            # Surface (but never auto-act on) near-duplicate contract titles,
            # so a human can choose to link this document to an existing
            # contract instead of accidentally creating a fork of it.
            similar = matching.find_similar_contracts(cur, contract_title)
            if similar:
                similar_warning = [
                    {"contract_id": m.contract_id, "title": m.title, "similarity": m.similarity}
                    for m in similar
                ]
            contract_id = db.create_contract(cur, title=contract_title)
        doc_subtype = None
        if doc_type == "corrigendum":
            doc_subtype = classify_corrigendum(ingested.raw_text).value
        document_id = db.insert_document(
            cur, contract_id=contract_id, doc_type=doc_type, doc_subtype=doc_subtype,
            source_url=source_url, source_org=source_org, doc_title=doc_title,
            doc_date=doc_date, page_number=page_number, raw_text=ingested.raw_text,
            raw_text_full_hash=ingested.raw_text_full_hash,
        )

        facts = extraction.extract_all_facts(ingested.raw_text)
        for f in facts:
            db.insert_fact(
                cur, contract_id=contract_id, document_id=document_id, fact_type=f.fact_type,
                fact_value=f.value, evidence_quote=f.evidence_quote, context_window=f.context_window,
                char_offset_start=f.char_offset_start, char_offset_end=f.char_offset_end,
                extraction_rule_id=f.extraction_rule_id, fact_confidence=f.fact_confidence,
            )

        # Corrigendum timeline logging (§4.4): only substantive changes feed the timeline.
        if doc_type == "corrigendum":
            subtype = classify_corrigendum(ingested.raw_text)
            event_type = (
                "corrigendum_substantive" if is_duration_relevant(subtype)
                else "corrigendum_bid_deadline_only"
            )
            db.insert_timeline_event(
                cur, contract_id=contract_id, event_type=event_type, document_id=document_id,
                narrative=f"Corrigendum classified as '{subtype.value}'. "
                          f"{'Feeds expiry recalculation.' if is_duration_relevant(subtype) else 'No effect on contract duration.'}",
            )

    reconciliation_summary = reconciliation.reconcile_new_document(cur, contract_id, document_id) \
        if not deduplicated else {"conflicts": [], "superseded": [], "corroborated": []}

    result = recalculate_contract(cur, contract_id)

    # Layer 2: link this contract to its underlying requirement, derive
    # procurement events, and refresh cycle statistics. This runs after
    # Layer 1's own result is fully computed, and never alters it.
    from core.requirements_pipeline import link_contract_to_requirement
    requirement_link = link_contract_to_requirement(cur, contract_id)
    if requirement_link.requirement_id:
        from core.radar import refresh_prediction
        refresh_prediction(cur, requirement_link.requirement_id)

    return IngestResult(
        document_id=document_id,
        contract_id=contract_id,
        n_facts_extracted=len(db.get_facts_for_document(cur, document_id)),
        deduplicated=deduplicated,
        reconciliation_summary=reconciliation_summary,
        status=result["status"],
        expiry_confidence=result["expiry_confidence"],
        similar_contracts_warning=similar_warning,
        requirement_link=requirement_link,
    )


def _sync_org_vendor(cur, contract_id: int, facts: list) -> None:
    """
    Populates contracts.org_id / vendor_id from the highest-confidence
    organisation_name / vendor_name facts, if any exist and the contract
    doesn't already have them set. This was previously never wired up —
    org_id/vendor_id stayed NULL forever — which Layer 2's requirement
    identity depends on (organisation match is a hard gate). Idempotent
    and additive: never overwrites a value that's already set, so a
    later, lower-confidence mention can't flip an established identity.
    """
    contract = db.get_contract(cur, contract_id)
    if contract is None:
        return
    updates = {}
    if contract["org_id"] is None:
        org_facts = sorted(
            (f for f in facts if f["fact_type"] == "organisation_name"),
            key=lambda f: 0 if f["fact_confidence"] == "HIGH" else 1,
        )
        if org_facts:
            updates["org_id"] = db.get_or_create(cur, "organisations", org_facts[0]["fact_value"])
    if contract["vendor_id"] is None:
        vendor_facts = sorted(
            (f for f in facts if f["fact_type"] == "vendor_name"),
            key=lambda f: 0 if f["fact_confidence"] == "HIGH" else 1,
        )
        if vendor_facts:
            updates["vendor_id"] = db.get_or_create(cur, "vendors", vendor_facts[0]["fact_value"])
    if updates:
        db.update_contract_status(cur, contract_id, **updates)


def recalculate_contract(cur, contract_id: int) -> dict:
    """Recomputes expiry + confidence + status for a contract from its
    current (non-superseded) facts, and persists the results. This is the
    single function that should be called any time facts change."""
    facts = db.get_facts_for_contract(cur, contract_id)
    documents = db.list_documents_for_contract(cur, contract_id)
    documents_by_id = {d["id"]: d for d in documents}

    _sync_org_vendor(cur, contract_id, facts)

    expiry_result = compute_expiry(facts)

    award_facts = [f for f in facts if f["fact_type"] == "award_date"]
    award_fact = award_facts[0] if award_facts else None

    open_conflicts = db.list_open_conflicts(cur, contract_id)

    assessment = assess_confidence(expiry_result, facts, documents_by_id, award_fact)
    status = derive_status(assessment, expiry_result.duration_conflict, bool(open_conflicts))

    db.insert_confidence_assessment(
        cur, contract_id=contract_id,
        award_date_confidence=assessment.award_date_confidence,
        duration_confidence=assessment.duration_confidence,
        start_date_confidence=assessment.start_date_confidence,
        source_reliability=assessment.source_reliability,
        cross_source_verified=assessment.cross_source_verified,
        cross_source_detail=assessment.cross_source_detail,
        expiry_confidence=assessment.expiry_confidence,
        limiting_factor=assessment.limiting_factor,
    )

    calc_id = db.insert_expiry_calculation(
        cur, contract_id=contract_id, calculation_type=expiry_result.calculation_type,
        formula_text=expiry_result.base_formula,
        input_fact_ids=expiry_result.input_fact_ids,
        result_date=expiry_result.current_expiry_estimate.isoformat() if expiry_result.current_expiry_estimate else None,
        confidence=assessment.expiry_confidence,
    )

    # supersede prior calculations of the same type
    prior = db.list_calculations_for_contract(cur, contract_id)
    for p in prior:
        if p["id"] != calc_id and p["superseded_by_calculation_id"] is None:
            cur.execute("UPDATE expiry_calculations SET superseded_by_calculation_id = ? WHERE id = ?",
                        (calc_id, p["id"]))

    db.update_contract_status(
        cur, contract_id,
        status=status,
        current_expiry_estimate=expiry_result.current_expiry_estimate.isoformat() if expiry_result.current_expiry_estimate else None,
        current_expiry_ceiling=expiry_result.current_expiry_ceiling.isoformat() if expiry_result.current_expiry_ceiling else None,
        last_reconciled_at="datetime_placeholder",
    )
    # last_reconciled_at needs the real timestamp function, not a placeholder string
    cur.execute("UPDATE contracts SET last_reconciled_at = datetime('now') WHERE id = ?", (contract_id,))

    return {
        "status": status,
        "expiry_confidence": assessment.expiry_confidence,
        "current_expiry_estimate": expiry_result.current_expiry_estimate,
        "current_expiry_ceiling": expiry_result.current_expiry_ceiling,
    }
