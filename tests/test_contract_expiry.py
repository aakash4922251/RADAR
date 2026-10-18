"""Contract-expiry radar, buyer profiles and the PDF brief over CPPP award details."""
import datetime as dt
import json

import pytest

duckdb = pytest.importorskip("duckdb")

from core.brief_pdf import build_buyer_brief, format_inr
from core.buyer_intel import buyer_profile, search_buyers
from core.contract_expiry import (
    backtest_contract_expiry,
    clean_text,
    contracts_ending,
    details_root,
    expiry_coverage,
)

FIELD = "Date of Completion/Completion Period in Days"

# (contract_date, completion, buyer, description, winners, value)
ROWS = [
    # Annual security contract at HPCL Mumbai: awarded 2023, 2024, 2025; each runs one year.
    ("01-Apr-2023 12:00 AM", "31-Mar-2024 12:00 AM", "HPCL", "Security services at Mumbai refinery", "Alpha Guards", 2.5e7),
    ("01-Apr-2024 12:00 AM", "31-Mar-2025 12:00 AM", "HPCL", "Security services at Mumbai refinery", "Alpha Guards", 2.6e7),
    ("02-Apr-2025 12:00 AM", "01-Apr-2026 12:00 AM", "HPCL", "Security services at Mumbai refinery", "Beta Security,Alpha Guards,", 2.8e7),
    # Two-year pipeline works at BPCL ending mid-2026, never awarded before.
    ("15-Jun-2024 12:00 AM", "14-Jun-2026 12:00 AM", "BPCL", "Laying &amp; construction of pipeline", "Gamma Infra", 1.2e9),
    # Short supply job (20 days): not a re-tenderable contract, must be ignored.
    ("01-Jan-2026 12:00 AM", "21-Jan-2026 12:00 AM", "BPCL", "Supply of valves for terminal", "Delta Valves", 5e5),
    # "Not stated" placeholder date used by the source.
    ("01-Feb-2026 12:00 AM", "01-Jan-1900 12:00 AM", "BPCL", "Painting of storage tanks", "Epsilon Paints", 3e6),
    # Buyer with no completion dates at all, used for profile-only checks.
    ("10-Mar-2025 12:00 AM", "", "AIIMS Raipur", "Housekeeping services hospital campus", "Clean Co", 4e6),
    ("12-Mar-2026 12:00 AM", "", "AIIMS Raipur", "Housekeeping services hospital campus", "Clean Co", 4.2e6),
    # One tender split into lots on a single day, with placeholder winner names.
    ("05-May-2025 12:00 AM", "", "AIIMS Raipur", "Supply of hospital beds ward", "Multiple Bidders", 1e6),
    ("05-May-2025 12:00 AM", "", "AIIMS Raipur", "Supply of hospital beds ward", "MUTLIPLE BIDDERS AWARDED", 1e6),
    ("05-May-2025 12:00 AM", "", "AIIMS Raipur", "Supply of hospital beds ward", "11698", 1e6),
]


@pytest.fixture
def root(tmp_path):
    con = duckdb.connect()
    con.execute("""CREATE TABLE t (contract_date VARCHAR, organisation_name VARCHAR, tender_description VARCHAR,
                   selected_bidders VARCHAR, contract_value DOUBLE, num_bids_received INTEGER, tender_id VARCHAR,
                   tender_ref_no VARCHAR, tender_document_url VARCHAR, state VARCHAR, tender_type VARCHAR,
                   raw_json VARCHAR, year BIGINT)""")
    for i, (cd, comp, buyer, desc, win, val) in enumerate(ROWS):
        con.execute("INSERT INTO t VALUES (?, ?, ?, ?, ?, 3, ?, ?, '', 'Maharashtra', 'Works', ?, 2025)",
                    [cd, buyer, desc, win, val, f"T{i}", f"REF{i}", json.dumps({FIELD: comp})])
    part = tmp_path / "ds" / "aoc_details" / "year=2025"
    part.mkdir(parents=True)
    con.execute(f"COPY t TO '{(part / 'data.parquet').as_posix()}' (FORMAT PARQUET)")
    return details_root(tmp_path)


def test_clean_text_decodes_cppp_entities_and_separators():
    assert clean_text("Laying &amp; construction &amp2 of\n pipeline,") == "Laying & construction &2 of pipeline"
    assert clean_text(None) == ""


def test_lists_contracts_ending_in_window_soonest_first(root):
    df = contracts_ending(root, as_of=dt.date(2026, 1, 1), within_days=182)
    assert list(df["buyer"]) == ["HPCL", "BPCL"]
    assert list(df["description"]) == ["Security services at Mumbai refinery", "Laying & construction of pipeline"]
    assert df.iloc[0]["incumbent"] == "Beta Security,Alpha Guards"


def test_prior_awards_separate_repeat_work_from_one_offs(root):
    df = contracts_ending(root, as_of=dt.date(2026, 1, 1), within_days=182).set_index("buyer")
    assert df.loc["HPCL", "prior_awards"] == 2
    assert df.loc["BPCL", "prior_awards"] == 0


def test_short_jobs_and_placeholder_dates_are_excluded(root):
    df = contracts_ending(root, as_of=dt.date(2025, 6, 1), within_days=3650)
    assert "Supply of valves for terminal" not in set(df["description"])
    assert "Painting of storage tanks" not in set(df["description"])


def test_contracts_not_yet_awarded_on_as_of_are_hidden(root):
    df = contracts_ending(root, as_of=dt.date(2025, 3, 1), within_days=60)
    assert list(df["end_date"].dt.date) == [dt.date(2025, 3, 31)]


def test_filters_bind_user_text_safely(root):
    assert list(contracts_ending(root, as_of=dt.date(2026, 1, 1), keyword="pipeline")["buyer"]) == ["BPCL"]
    assert contracts_ending(root, as_of=dt.date(2026, 1, 1), city="O'Brien").empty
    assert list(contracts_ending(root, as_of=dt.date(2026, 1, 1), min_value_inr=1e8)["buyer"]) == ["BPCL"]


def test_coverage_counts_only_usable_end_dates(root):
    cov = expiry_coverage(root)
    assert cov["contracts_with_end_date"] == 4
    assert cov["buyer_names"] == ["HPCL", "BPCL"]


def test_backtest_scores_reaward_near_stated_end(root):
    result = backtest_contract_expiry(root, as_of=dt.date(2024, 3, 1), horizon_days=60, tolerance_days=30)
    detail = result["detail"]
    assert len(detail) == 1 and bool(detail.iloc[0]["hit"]) and not bool(detail.iloc[0]["baseline_hit"])


def test_buyer_search_and_profile(root):
    assert list(search_buyers(root, "aiims")["buyer"]) == ["AIIMS Raipur"]
    profile = buyer_profile(root, "HPCL", as_of=dt.date(2026, 1, 1))
    s = profile["summary"]
    assert s["awards"] == 3 and s["total_value"] == pytest.approx(7.9e7)
    suppliers = profile["suppliers"].set_index("supplier")
    assert suppliers.loc["Alpha Guards", "contracts_won"] == 3
    assert suppliers.loc["Beta Security", "contracts_won"] == 1
    assert profile["repeat_requirements"].iloc[0]["awards"] == 3
    assert len(profile["ending"]) == 1


def test_profile_for_buyer_without_end_dates(root):
    profile = buyer_profile(root, "AIIMS Raipur", as_of=dt.date(2026, 6, 1))
    assert profile["ending"].empty
    assert profile["summary"]["awards"] == 5
    # Lots awarded on one day are not a repeat requirement; placeholders are not suppliers.
    assert list(profile["repeat_requirements"]["requirement"]) == ["Housekeeping services hospital campus"]
    assert list(profile["suppliers"]["supplier"]) == ["Clean Co"]


def test_brief_pdf_renders(root):
    pdf = build_buyer_brief(buyer_profile(root, "HPCL", as_of=dt.date(2026, 1, 1)), prepared_for="Test Co",
                            generated=dt.date(2026, 1, 2))
    assert pdf.startswith(b"%PDF") and len(pdf) > 2000


@pytest.mark.parametrize("value,expected", [
    (3.2e9, "₹320.00 Cr"), (4.5e5, "₹4.50 L"), (9999, "₹9,999"), (0, "—"), (None, "—"), (float("nan"), "—"),
])
def test_format_inr(value, expected):
    assert format_inr(value) == expected
