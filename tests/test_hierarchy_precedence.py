from core.expiry_engine import compute_expiry
from core import pipeline
from db import database as db


class FakeRow(dict):
    def __getitem__(self, key):
        return dict.__getitem__(self, key)


def test_explicit_calendar_range_outranks_duration_arithmetic():
    """
    Spec §4.3 rank 1: an explicit calendar start-and-end date pair outranks
    everything else, including a separately-stated base_duration_months
    fact that would otherwise drive the calculation.
    """
    facts = [
        FakeRow(id=1, document_id=1, fact_type="base_duration_months",
                fact_value="24", fact_confidence="HIGH"),
        FakeRow(id=2, document_id=2, fact_type="base_duration_explicit_end_date",
                fact_value="2022-06-15|2023-05-14", fact_confidence="HIGH"),
        FakeRow(id=3, document_id=1, fact_type="award_date",
                fact_value="2022-01-01", fact_confidence="HIGH"),
    ]
    result = compute_expiry(facts)
    assert result.base_expiry.isoformat() == "2023-05-14"
    assert "Explicit calendar dates" in result.base_formula


def test_recency_alone_does_not_override_authority(cur):
    """
    Spec §5.1: a LOW-ranked department web page dated later than a
    HIGH-ranked NIT must NOT override it. Both facts are retained and a
    conflict is raised instead of a silent supersession.
    """
    contract_id = db.create_contract(cur, title="Hierarchy Test Contract")

    nit_doc_id = db.insert_document(
        cur, contract_id=contract_id, doc_type="nit", doc_date="2020-01-01",
        raw_text="AMC for a period of 2 years.", raw_text_full_hash="hash_nit",
    )
    db.insert_fact(
        cur, contract_id=contract_id, document_id=nit_doc_id, fact_type="base_duration_months",
        fact_value="24", evidence_quote="2 years", extraction_rule_id="TEST", fact_confidence="HIGH",
    )

    # A later-dated, but much lower-ranked, department page claims a
    # different (wrong) duration.
    page_doc_id = db.insert_document(
        cur, contract_id=contract_id, doc_type="department_page", doc_date="2024-01-01",
        raw_text="Contract duration: 3 years.", raw_text_full_hash="hash_page",
    )
    db.insert_fact(
        cur, contract_id=contract_id, document_id=page_doc_id, fact_type="base_duration_months",
        fact_value="36", evidence_quote="3 years", extraction_rule_id="TEST", fact_confidence="MEDIUM",
    )

    from core.reconciliation import reconcile_new_document
    summary = reconcile_new_document(cur, contract_id, page_doc_id)

    # must NOT be superseded — recency alone is not authority
    assert summary["superseded"] == []
    # the NIT's fact must still be present and non-superseded
    nit_fact = db.get_facts_for_contract(cur, contract_id, fact_type="base_duration_months")
    values = {f["fact_value"] for f in nit_fact}
    assert "24" in values, "the higher-ranked NIT fact must survive un-superseded"
