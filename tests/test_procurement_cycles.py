from db import database as db
from core.procurement_cycles import compute_cycle_stats, recompute_and_store


def _make_requirement(cur, asset="cctv"):
    org_id = db.get_or_create(cur, "organisations", "Test Organisation")
    return db.create_requirement(cur, org_id=org_id, asset_keyword=asset, location="pune",
                                  normalized_title="cctv amc")


def _link_event(cur, requirement_id, event_type, event_date):
    event_id = db.insert_procurement_event(cur, contract_id=None, event_type=event_type, event_date=event_date)
    db.link_event_to_requirement(cur, requirement_id=requirement_id, event_id=event_id,
                                  match_status="STRONG_MATCH", match_score=1.0, match_evidence_json="{}")
    return event_id


def test_single_event_is_insufficient_data(cur):
    """Case 2 from the directive: only one historical procurement event."""
    req_id = _make_requirement(cur)
    _link_event(cur, req_id, "tender_published", "2023-01-15")
    stats = compute_cycle_stats(cur, req_id)
    assert stats.insufficient_data is True
    assert stats.n_cycles == 1
    assert stats.median_interval_days is None


def test_no_events_is_insufficient_data(cur):
    req_id = _make_requirement(cur)
    stats = compute_cycle_stats(cur, req_id)
    assert stats.insufficient_data is True
    assert stats.n_cycles == 0


def test_strong_annual_recurrence_produces_low_dispersion_stats(cur):
    """Case 1 from the directive: 5 yearly cycles, low interval variance."""
    req_id = _make_requirement(cur)
    dates = ["2019-01-15", "2020-01-10", "2021-01-20", "2022-01-12", "2023-01-18"]
    for d in dates:
        _link_event(cur, req_id, "tender_published", d)
    stats = compute_cycle_stats(cur, req_id)
    assert stats.insufficient_data is False
    assert stats.n_cycles == 5
    assert len(stats.interval_days) == 4
    assert 340 <= stats.median_interval_days <= 380
    assert stats.stddev_interval_days < 15, "low variance expected for strong annual recurrence"


def test_irregular_cycles_produce_high_dispersion(cur):
    """Case 3 from the directive: irregular cycles should show high stddev."""
    req_id = _make_requirement(cur)
    dates = ["2019-01-01", "2019-08-01", "2021-11-01"]  # wildly irregular gaps
    for d in dates:
        _link_event(cur, req_id, "tender_published", d)
    stats = compute_cycle_stats(cur, req_id)
    assert stats.insufficient_data is False
    assert stats.stddev_interval_days > 100


def test_anchor_priority_prefers_tender_published_over_award(cur):
    req_id = _make_requirement(cur)
    _link_event(cur, req_id, "tender_awarded", "2020-02-01")
    _link_event(cur, req_id, "tender_awarded", "2021-02-01")
    _link_event(cur, req_id, "tender_published", "2020-01-01")
    _link_event(cur, req_id, "tender_published", "2021-01-01")
    stats = compute_cycle_stats(cur, req_id)
    assert stats.anchor_event_type == "tender_published"


def test_anchor_falls_back_when_preferred_type_insufficient(cur):
    req_id = _make_requirement(cur)
    _link_event(cur, req_id, "tender_published", "2020-01-01")  # only one — not enough
    _link_event(cur, req_id, "tender_awarded", "2020-02-01")
    _link_event(cur, req_id, "tender_awarded", "2021-02-01")
    stats = compute_cycle_stats(cur, req_id)
    assert stats.anchor_event_type == "tender_awarded"


def test_recompute_and_store_persists_and_supersedes(cur):
    req_id = _make_requirement(cur)
    _link_event(cur, req_id, "tender_published", "2020-01-01")
    _link_event(cur, req_id, "tender_published", "2021-01-01")
    first = recompute_and_store(cur, req_id)
    _link_event(cur, req_id, "tender_published", "2022-01-01")
    second = recompute_and_store(cur, req_id)

    latest = db.get_latest_cycle(cur, req_id)
    assert latest["n_cycles"] == 3

    all_rows = cur.execute("SELECT * FROM procurement_cycles WHERE requirement_id = ?", (req_id,)).fetchall()
    assert len(all_rows) == 2
    superseded = [r for r in all_rows if r["id"] != latest["id"]]
    assert superseded[0]["superseded_by_cycle_id"] == latest["id"]
