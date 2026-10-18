"""Recurring-award signals from historical AOC Parquet partitions."""
import datetime as dt

import pytest

duckdb = pytest.importorskip("duckdb")

from core.dataset_import import backtest_aoc_signals, historical_aoc_cycle_signals

ROWS = [
    # Annual CCTV AMC at one buyer in Pune, Maharashtra: 3 awards a year apart.
    ("15-Apr-2023 05:30 PM", "AIIMS Pune", "CCTV AMC at main campus", "Maharashtra"),
    ("14-Apr-2024 05:30 PM", "AIIMS Pune", "CCTV AMC at main campus", "Maharashtra"),
    ("16-Apr-2025 05:30 PM", "AIIMS Pune", "CCTV AMC at main campus", "Maharashtra"),
    # Annual housekeeping at a buyer in Kerala.
    ("01-May-2024 12:00 AM", "Kerala", "Housekeeping services O'Brien block", "Kerala"),
    ("02-May-2025 12:00 AM", "Kerala", "Housekeeping services O'Brien block", "Kerala"),
    # One-off award that only extends the data range (never forms a signal).
    ("01-Jun-2026 12:00 AM", "ASI Agra", "Garden upkeep at fort complex", "Uttar Pradesh"),
]


@pytest.fixture
def aoc_root(tmp_path):
    con = duckdb.connect()
    con.execute("CREATE TABLE t (aoc_date VARCHAR, org_name VARCHAR, title VARCHAR, state VARCHAR, "
                "tender_id VARCHAR, ref_no VARCHAR, portal_type VARCHAR, detail_url VARCHAR, year BIGINT)")
    for i, (d, org, title, state) in enumerate(ROWS):
        con.execute("INSERT INTO t VALUES (?, ?, ?, ?, ?, '', 'central', '', 2025)", [d, org, title, state, f"T{i}"])
    part = tmp_path / "aoc_tenders" / "year=2025"
    part.mkdir(parents=True)
    con.execute(f"COPY t TO '{(part / 'data.parquet').as_posix()}' (FORMAT PARQUET)")
    return tmp_path


def _signals(root, **kw):
    return historical_aoc_cycle_signals(root, as_of=dt.date(2026, 3, 1), limit=None, **kw)


def test_finds_both_recurring_requirements(aoc_root):
    assert sorted(_signals(aoc_root)["org_name"]) == ["AIIMS Pune", "Kerala"]


def test_keyword_filter_narrows_to_matching_service(aoc_root):
    df = _signals(aoc_root, keyword="cctv")
    assert list(df["org_name"]) == ["AIIMS Pune"]
    assert int(df.iloc[0]["award_count"]) == 3


def test_state_filter_uses_state_column(aoc_root):
    assert list(_signals(aoc_root, state="Maharashtra")["org_name"]) == ["AIIMS Pune"]


def test_filters_with_quotes_do_not_break_the_query(aoc_root):
    assert list(_signals(aoc_root, city="O'Brien")["org_name"]) == ["Kerala"]


def test_signals_use_only_awards_before_as_of(aoc_root):
    df = historical_aoc_cycle_signals(aoc_root, as_of=dt.date(2024, 6, 1), limit=None, keyword="cctv")
    assert int(df.iloc[0]["award_count"]) == 2  # the April 2025 award is not visible yet


def test_backtest_scores_hit_for_award_inside_window(aoc_root):
    result = backtest_aoc_signals(aoc_root, as_of=dt.date(2025, 1, 1), horizon_days=182)
    detail = result["detail"]
    cctv = detail[detail["org_name"] == "AIIMS Pune"].iloc[0]
    assert bool(cctv["scored"]) and bool(cctv["hit"])
    assert not bool(cctv["baseline_hit"])  # nothing was awarded half a cycle later
    assert cctv["next_actual_award"].date() == dt.date(2025, 4, 16)
    assert result["data_end"] == dt.date(2026, 6, 1)
    summary = result["summary"].set_index("evidence")
    assert summary.loc["All", "hits"] == 1 and summary.loc["All", "scored"] == 1


def test_backtest_does_not_score_windows_still_open(aoc_root):
    result = backtest_aoc_signals(aoc_root, as_of=dt.date(2026, 4, 1), horizon_days=365)
    assert not result["detail"]["scored"].any()
