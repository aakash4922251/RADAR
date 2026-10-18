from datetime import date, timedelta

from core.backtesting import backtest_requirement_as_of, evaluate_backtest_results
from core.prediction_engine import (
    assess_prediction_confidence,
    build_prediction_for_requirement,
    establish_prediction_status,
)
from core.prediction_matching import match_prediction_event


def _create_requirement_with_events(cur, org_name, asset_keyword, title, event_dates):
    cur.execute("INSERT INTO organisations (name) VALUES (?)", (org_name,))
    org_id = cur.lastrowid
    cur.execute(
        "INSERT INTO requirements (org_id, asset_keyword, location, normalized_title) VALUES (?, ?, ?, ?)",
        (org_id, asset_keyword, "pune", title),
    )
    requirement_id = cur.lastrowid
    for event_date in event_dates:
        cur.execute(
            "INSERT INTO contracts (title, org_id, status, current_expiry_estimate) VALUES (?, ?, 'VERIFIED', '2027-12-31')",
            (f"{title} contract", org_id),
        )
        contract_id = cur.lastrowid
        cur.execute(
            "INSERT INTO procurement_events (contract_id, event_type, event_date) VALUES (?, 'tender_published', ?)",
            (contract_id, event_date),
        )
        event_id = cur.lastrowid
        cur.execute(
            "INSERT INTO requirement_event_links (requirement_id, event_id, match_status, match_score, match_evidence) VALUES (?, ?, 'STRONG_MATCH', 1.0, '{}')",
            (requirement_id, event_id),
        )
    return requirement_id


def test_prediction_engine_and_confidence_floor(cur):
    requirement_id = _create_requirement_with_events(
        cur,
        "Org A",
        "cctv",
        "cctv amc pune",
        ["2019-01-15", "2020-01-10", "2021-01-20", "2022-01-12", "2023-01-18"],
    )
    prediction = build_prediction_for_requirement(cur, requirement_id)
    assert prediction["prediction_id"] is not None
    assert prediction["prediction_status"] == "PREDICTED"
    assert prediction["window_start"] != prediction["window_end"]
    evidence = cur.execute("SELECT signal_name FROM prediction_evidence WHERE prediction_id = ?", (prediction["prediction_id"],)).fetchall()
    names = {row[0] for row in evidence}
    assert {"historical_interval_regularity", "contract_expiry_offset", "tender_expiry_relationship", "extension_behaviour", "recurrence_strength", "timing_stability", "lifecycle_state"}.issubset(names)
    assert assess_prediction_confidence(requirement_id=requirement_id, n_cycles=1, dispersion=0.0, extension_active=False, conflict_present=False)["confidence"] == "INSUFFICIENT_DATA"
    assert assess_prediction_confidence(requirement_id=requirement_id, n_cycles=2, dispersion=120.0, extension_active=False, conflict_present=False)["confidence"] == "LOW"


def test_prediction_status_lifecycle(cur):
    requirement_id = _create_requirement_with_events(
        cur,
        "Org B",
        "security",
        "security services mumbai",
        ["2020-01-10", "2021-01-14", "2022-01-18"],
    )
    prediction = build_prediction_for_requirement(cur, requirement_id)
    assert prediction["prediction_status"] == "PREDICTED"
    assert establish_prediction_status(cur, prediction["prediction_id"], "WATCHING") == "WATCHING"
    assert establish_prediction_status(cur, prediction["prediction_id"], "TENDER_DETECTED") == "TENDER_DETECTED"
    assert establish_prediction_status(cur, prediction["prediction_id"], "CONFIRMED") == "CONFIRMED"
    assert cur.execute("SELECT prediction_status FROM predictions WHERE prediction_id = ?", (prediction["prediction_id"],)).fetchone()[0] == "CONFIRMED"


def test_prediction_matching_requires_multiple_signals(cur):
    requirement_id = _create_requirement_with_events(
        cur,
        "Org C",
        "fire_safety",
        "fire alarm system delhi",
        ["2020-01-10", "2021-01-14", "2022-01-18"],
    )
    prediction = build_prediction_for_requirement(cur, requirement_id)
    org_id = cur.execute("SELECT id FROM organisations WHERE name = ?", ("Org C",)).fetchone()[0]
    cur.execute(
        "INSERT INTO contracts (title, org_id, status, current_expiry_estimate) VALUES (?, ?, 'VERIFIED', '2027-12-31')",
        ("Fire Alarm UG", org_id),
    )
    contract_id = cur.lastrowid
    cur.execute(
        "INSERT INTO procurement_events (contract_id, event_type, event_date) VALUES (?, 'tender_published', '2027-01-09')",
        (contract_id,),
    )
    event_id = cur.lastrowid
    result = match_prediction_event(cur, requirement_id, event_id, prediction_id=prediction["prediction_id"], org_id=org_id, title_text='fire alarm system delhi')
    assert result["status"] in {"CONFIRMED_MATCH", "PROBABLE_MATCH"}
    weak_match = match_prediction_event(cur, requirement_id, event_id, prediction_id=prediction["prediction_id"], org_id=org_id, title_text='fire alarm')
    assert weak_match["status"] in {"UNRELATED_TENDER", "REVIEW_REQUIRED"}


def test_negative_outcome_wording_and_backtest_no_leakage(cur):
    requirement_id = _create_requirement_with_events(
        cur,
        "Org D",
        "housekeeping",
        "housekeeping services pune",
        ["2019-01-15", "2020-01-10", "2021-01-20", "2022-01-12", "2023-01-18"],
    )

    result = backtest_requirement_as_of(cur, requirement_id, date(2023, 12, 31))
    assert result["used_events"] == 5
    assert result["prediction"]["predicted_window_start"] is not None
    assert result["prediction"]["predicted_window_end"] is not None

    future_eq = cur.execute(
        "SELECT COUNT(*) FROM procurement_events WHERE event_date = '2024-01-11'"
    ).fetchone()[0]
    assert future_eq == 0

    no_match_note = "No matching procurement was detected within the monitored sources"
    assert no_match_note.startswith("No matching")


def test_evaluation_report_not_enough_data(cur):
    report = evaluate_backtest_results(
        cur,
        [{"match_status": "NO_MATCH", "timing_error_days": 0}],
        minimum_dataset_size=10,
    )
    assert report["dataset_size"] == 1
    assert report["status"] == "NOT_ENOUGH_DATA_FOR_RELIABLE_PERFORMANCE_ESTIMATE"
