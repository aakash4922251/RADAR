from db import database as db
from core.reconciliation import reconcile_new_document
from core.pipeline import recalculate_contract


def test_contradictory_same_rank_documents_flag_conflict_not_autoresolve(cur):
    contract_id = db.create_contract(cur, title="Conflict Test Contract")

    doc_a = db.insert_document(
        cur, contract_id=contract_id, doc_type="nit", doc_date="2024-01-01",
        raw_text="AMC for a period of 2 years.", raw_text_full_hash="hash_a",
    )
    db.insert_fact(
        cur, contract_id=contract_id, document_id=doc_a, fact_type="base_duration_months",
        fact_value="24", evidence_quote="2 years", extraction_rule_id="TEST", fact_confidence="HIGH",
    )

    doc_b = db.insert_document(
        cur, contract_id=contract_id, doc_type="nit", doc_date="2024-06-01",
        raw_text="AMC for a period of 3 years.", raw_text_full_hash="hash_b",
    )
    db.insert_fact(
        cur, contract_id=contract_id, document_id=doc_b, fact_type="base_duration_months",
        fact_value="36", evidence_quote="3 years", extraction_rule_id="TEST", fact_confidence="HIGH",
    )

    summary = reconcile_new_document(cur, contract_id, doc_b)

    assert summary["conflicts"], "equal-rank contradictory facts must be flagged as a conflict"
    assert summary["superseded"] == [], "must NOT auto-resolve by silently overwriting"

    # both facts must survive, non-superseded
    facts = db.get_facts_for_contract(cur, contract_id, fact_type="base_duration_months")
    values = {f["fact_value"] for f in facts}
    assert values == {"24", "36"}

    open_conflicts = db.list_open_conflicts(cur, contract_id)
    assert len(open_conflicts) == 1

    # status must reflect the unresolved conflict
    result = recalculate_contract(cur, contract_id)
    contract = db.get_contract(cur, contract_id)
    assert contract["status"] == "CONFLICTING_EVIDENCE"


def test_compatible_values_are_corroborated_not_flagged(cur):
    """'2 years' (24 months) vs '730 days' (~23.98 months) are compatible
    within tolerance and should corroborate, not conflict."""
    contract_id = db.create_contract(cur, title="Corroboration Test Contract")

    doc_a = db.insert_document(
        cur, contract_id=contract_id, doc_type="nit", doc_date="2024-01-01",
        raw_text="period of 2 years", raw_text_full_hash="hash_c",
    )
    db.insert_fact(
        cur, contract_id=contract_id, document_id=doc_a, fact_type="base_duration_months",
        fact_value="24", evidence_quote="2 years", extraction_rule_id="TEST", fact_confidence="HIGH",
    )
    doc_b = db.insert_document(
        cur, contract_id=contract_id, doc_type="aoc", doc_date="2024-02-01",
        raw_text="730 days", raw_text_full_hash="hash_d",
    )
    db.insert_fact(
        cur, contract_id=contract_id, document_id=doc_b, fact_type="base_duration_months",
        fact_value="23.98", evidence_quote="730 days", extraction_rule_id="TEST", fact_confidence="HIGH",
    )
    summary = reconcile_new_document(cur, contract_id, doc_b)
    assert summary["corroborated"], "compatible values within tolerance should corroborate"
    assert summary["conflicts"] == []
