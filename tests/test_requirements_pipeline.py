from core import pipeline
from db import database as db


def _ingest_cycle(cur, contract_id, title, nit_date, aoc_date):
    nit_text = f"{title}. Tendering authority: XYZ Hospital, Pune. AMC for a period of 1 year."
    r1 = pipeline.ingest_document(
        cur, raw_text=nit_text, doc_type="nit", contract_title=title,
        contract_id=contract_id, doc_date=nit_date, doc_title="NIT",
    )
    r2 = pipeline.ingest_document(
        cur, raw_text=f"Award Date: {aoc_date}.", doc_type="aoc",
        contract_id=r1.contract_id, doc_date=nit_date,
    )
    return r2


def test_differently_worded_titles_merge_into_one_requirement(cur):
    """
    This is the exact motivating example from the directive: four
    differently-worded NIT titles for the same underlying CCTV AMC
    requirement at the same organisation must merge into ONE requirement,
    not four.
    """
    titles = [
        ("Annual Maintenance Contract for CCTV System - 2019", "2019-01-15", "15-Feb-2019"),
        ("CCTV Surveillance System Comprehensive AMC - 2020", "2020-01-10", "28-Jan-2020"),
        ("Comprehensive Maintenance of CCTV Installation - 2021", "2021-01-20", "05-Feb-2021"),
        ("CCTV CAMC Services - 2022", "2022-01-12", "30-Jan-2022"),
    ]
    requirement_ids = set()
    last_result = None
    for title, nit_date, aoc_date in titles:
        result = _ingest_cycle(cur, None, title, nit_date, aoc_date)
        requirement_ids.add(result.requirement_link.requirement_id)
        last_result = result

    assert len(requirement_ids) == 1, f"expected exactly one requirement, got {requirement_ids}"

    req_id = requirement_ids.pop()
    aliases = db.list_aliases_for_requirement(cur, req_id)
    alias_texts = {a["alias_text"] for a in aliases}
    assert len(alias_texts) == 4, "all four original titles must be preserved as aliases"

    stats = last_result.requirement_link.cycle_stats
    assert stats.insufficient_data is False
    assert stats.n_cycles == 4
    assert 330 <= stats.median_interval_days <= 390  # near-annual


def test_unrelated_asset_at_same_organisation_does_not_merge(cur):
    """
    Case 6 from the directive: an unrelated tender from the same
    organisation must NOT be matched to an existing requirement.
    """
    r1 = _ingest_cycle(cur, None, "CCTV AMC for XYZ Hospital", "2023-01-01", "01-Feb-2023")
    cctv_req_id = r1.requirement_link.requirement_id

    housekeeping_nit = "Housekeeping Services. Tendering authority: XYZ Hospital, Pune."
    r2 = pipeline.ingest_document(
        cur, raw_text=housekeeping_nit, doc_type="nit",
        contract_title="Housekeeping Services at XYZ Hospital", doc_date="2023-03-01",
    )
    housekeeping_req_id = r2.requirement_link.requirement_id

    assert housekeeping_req_id != cctv_req_id
    assert r2.requirement_link.match_result.status in ("STRONG_MATCH",)  # new requirement, self-consistent
    assert r2.requirement_link.created_new_requirement is True


def test_different_organisation_same_asset_does_not_merge(cur):
    r1 = _ingest_cycle(cur, None, "CCTV AMC for XYZ Hospital", "2023-01-01", "01-Feb-2023")
    req_id_1 = r1.requirement_link.requirement_id

    other_org_nit = "CCTV AMC. Tendering authority: ABC College, Mumbai."
    r2 = pipeline.ingest_document(
        cur, raw_text=other_org_nit, doc_type="nit",
        contract_title="CCTV AMC for ABC College", doc_date="2023-06-01",
    )
    req_id_2 = r2.requirement_link.requirement_id
    assert req_id_2 != req_id_1, "confirmed different organisations must never merge"


def test_no_recognizable_asset_leaves_contract_unlinked(cur):
    r1 = pipeline.ingest_document(
        cur, raw_text="This is a generic administrative notice with no clear category.",
        doc_type="department_page", contract_title="Unclassifiable Notice",
    )
    assert r1.requirement_link.requirement_id is None
    assert r1.requirement_link.reason_unlinked is not None


def test_requirement_link_is_non_blocking_for_layer1_result(cur):
    """Layer 2 linking must never change Layer 1's own expiry/status
    result — it only adds information alongside it."""
    r1 = pipeline.ingest_document(
        cur, raw_text="AMC for a period of 2 years.", doc_type="nit",
        contract_title="CCTV AMC for Layer 1 Consistency Test", doc_date="2024-01-01",
    )
    r2 = pipeline.ingest_document(
        cur, raw_text="Award Date: 01-Feb-2024.", doc_type="aoc", contract_id=r1.contract_id,
    )
    contract = db.get_contract(cur, r1.contract_id)
    assert contract["current_expiry_estimate"] == "2026-02-01"
    assert r2.status == contract["status"]
