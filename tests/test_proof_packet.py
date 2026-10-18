from core import pipeline
from core.proof_packet import build_proof_packet_text
from db import database as db


def test_proof_packet_contains_required_blocks(cur):
    nit_text = "AMC of Fire Fighting System. AMC for a period of 2 years."
    aoc_text = "Award Date: 14-Mar-2025."

    r1 = pipeline.ingest_document(cur, raw_text=nit_text, doc_type="nit",
                                   contract_title="Fire Fighting AMC")
    r2 = pipeline.ingest_document(cur, raw_text=aoc_text, doc_type="aoc",
                                   contract_id=r1.contract_id)

    packet = build_proof_packet_text(cur, r1.contract_id)

    for required_block in [
        "CONTRACT EXPIRY PROOF", "FACTS AND EVIDENCE", "CALCULATION",
        "CONFIDENCE BREAKDOWN", "EXCEPTIONS", "STATUS:",
        "CURRENT EXPIRY ESTIMATE", "CURRENT EXPIRY CEILING",
    ]:
        assert required_block in packet, f"missing required block: {required_block}"

    # the calculation shown must match the persisted estimate
    contract = db.get_contract(cur, r1.contract_id)
    assert contract["current_expiry_estimate"] == "2027-03-14"
    assert "2027" in packet


def test_proof_packet_states_unverified_when_never_human_checked(cur):
    r1 = pipeline.ingest_document(cur, raw_text="A contract with no useful facts at all.",
                                   doc_type="nit", contract_title="Vague Contract")
    packet = build_proof_packet_text(cur, r1.contract_id)
    contract = db.get_contract(cur, r1.contract_id)
    assert contract["last_human_verified_at"] is None
    assert contract["current_expiry_estimate"] is None
    assert "UNKNOWN" in packet or "not stated" in packet.lower()
