from core.matching import find_similar_contracts
from core import pipeline
from db import database as db


def test_similar_titles_are_detected(cur):
    db.create_contract(cur, title="AMC of Fire Fighting System, Terminal Building")
    matches = find_similar_contracts(cur, "AMC of Fire-Fighting System Terminal Building")
    assert matches, "near-duplicate title should be detected"
    assert matches[0].similarity >= 0.82


def test_exact_title_is_not_reported_as_similar(cur):
    db.create_contract(cur, title="AMC of Fire Fighting System, Terminal Building")
    matches = find_similar_contracts(cur, "AMC of Fire Fighting System, Terminal Building")
    assert matches == [], "an exact match is the same contract, not a 'similar' one needing review"


def test_unrelated_titles_are_not_flagged(cur):
    db.create_contract(cur, title="AMC of Fire Fighting System, Terminal Building")
    matches = find_similar_contracts(cur, "Housekeeping Services at IIT Kanpur")
    assert matches == []


def test_pipeline_surfaces_warning_but_never_auto_merges(cur):
    """Ingesting a document under a near-duplicate title still creates a
    SEPARATE contract (no silent merge) but returns a warning for the UI."""
    r1 = pipeline.ingest_document(
        cur, raw_text="AMC for a period of 2 years.", doc_type="nit",
        contract_title="AMC of Fire Fighting System, Terminal Building",
    )
    r2 = pipeline.ingest_document(
        cur, raw_text="AMC for a period of 3 years.", doc_type="nit",
        contract_title="AMC of Fire-Fighting System, Terminal Building",  # near-duplicate, hyphenated
    )
    assert r1.contract_id != r2.contract_id, "must never silently merge into the same contract"
    assert r2.similar_contracts_warning, "a near-duplicate title must surface a warning"
    assert r2.similar_contracts_warning[0]["contract_id"] == r1.contract_id


def test_pipeline_no_warning_for_genuinely_new_contract(cur):
    r1 = pipeline.ingest_document(
        cur, raw_text="AMC for a period of 2 years.", doc_type="nit",
        contract_title="AMC of Fire Fighting System, Terminal Building",
    )
    r2 = pipeline.ingest_document(
        cur, raw_text="Housekeeping for a period of 1 year.", doc_type="nit",
        contract_title="Housekeeping Services at IIT Kanpur",
    )
    assert not r2.similar_contracts_warning
