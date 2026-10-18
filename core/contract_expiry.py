"""
Contract expiry intelligence from public CPPP award details.

Every CPPP award-of-contract (AOC) detail record names the buyer, the
winning bidder(s), the contract value and the contract date; a subset also
states the "Date of Completion". Together that is an evidence-backed answer
to "which government contracts end soon, who holds them, and how big are
they" -- known months before the replacement tender is published.

Only stated completion dates are used. Nothing here estimates an end date
that the source did not publish; records without one are simply not listed.
Results come straight from the read-only Parquet partitions via DuckDB.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

from core.dataset_import import (
    _aoc_date_sql,
    _as_date,
    _family_key_sql,
    _org_key_sql,
    duckdb_available,
)

COMPLETION_FIELD = "Date of Completion/Completion Period in Days"
MIN_CONTRACT_DAYS = 90  # shorter jobs are one-off supplies, not re-tenderable contracts


def details_root(base_dir: str | Path) -> Path | None:
    """First imported dataset that has AOC detail partitions."""
    base = Path(base_dir)
    candidates = sorted(base.glob("*/aoc_details/year=*/data.parquet")) if base.exists() else []
    return candidates[0].parents[2] if candidates else None


def clean_text(value) -> str:
    """Display form of a CPPP text field: HTML entities decoded (including the
    unterminated "&amp2" style), whitespace collapsed, trailing separators cut."""
    import html
    import re

    if value is None or (isinstance(value, float) and value != value):
        return ""
    text = html.unescape(str(value)).replace("&amp", "&")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ,;")


def _filters(state: str = "", city: str = "", keyword: str = "", min_value_inr: float = 0,
             buyer: str = "") -> tuple[str, list]:
    """AND-clauses over the ``contracts`` CTE; user values are bound, never interpolated."""
    clauses, params = [], []
    if buyer:
        clauses.append("buyer = ?")
        params.append(buyer)
    if state.strip():
        clauses.append("LOWER(COALESCE(state, '')) LIKE LOWER(?) OR LOWER(buyer) LIKE LOWER(?)")
        params.extend([f"%{state.strip()}%"] * 2)
    if city.strip():
        clauses.append("LOWER(buyer) LIKE LOWER(?) OR LOWER(description) LIKE LOWER(?)")
        params.extend([f"%{city.strip()}%"] * 2)
    for word in keyword.split():
        clauses.append("LOWER(description) LIKE LOWER(?)")
        params.append(f"%{word}%")
    if min_value_inr:
        clauses.append("contract_value >= ?")
        params.append(float(min_value_inr))
    return "".join(f"\n      AND ({c})" for c in clauses), params


def _contracts_cte(root: Path) -> str:
    """CTE ``contracts``: one row per distinct awarded contract with a usable
    stated completion date, plus ``awards``: every dated award (with or
    without completion date), used to see what the buyer did next."""
    pattern = str(root / "aoc_details" / "year=*" / "data.parquet").replace("\\", "/").replace("'", "''")
    comp = f"json_extract_string(raw_json, '$.\"{COMPLETION_FIELD}\"')"
    return f"""
    src AS (
        SELECT
            {_aoc_date_sql("contract_date")} AS contract_date,
            {_aoc_date_sql(comp)} AS end_date,
            NULLIF(TRIM(organisation_name), '') AS buyer,
            NULLIF(TRIM(tender_description), '') AS description,
            NULLIF(TRIM(selected_bidders), '') AS incumbent,
            NULLIF(contract_value, 0) AS contract_value,
            num_bids_received, tender_id, tender_ref_no, tender_document_url, state, tender_type,
            {_org_key_sql("organisation_name")} AS org_key,
            {_family_key_sql("tender_description")} AS family_key
        FROM read_parquet('{pattern}', union_by_name = true)
    ),
    awards AS (
        SELECT DISTINCT org_key, family_key, contract_date
        FROM src
        WHERE contract_date IS NOT NULL AND buyer IS NOT NULL
          AND len(string_split(family_key, ' ')) >= 2
    ),
    contracts AS (
        SELECT * EXCLUDE (rn) FROM (
            SELECT *, DATE_DIFF('day', contract_date, end_date) AS duration_days,
                   ROW_NUMBER() OVER (
                       PARTITION BY org_key, family_key, contract_date, end_date, COALESCE(tender_id, '')
                       ORDER BY contract_value DESC NULLS LAST
                   ) AS rn
            FROM src
            WHERE contract_date IS NOT NULL AND end_date IS NOT NULL
              AND buyer IS NOT NULL AND description IS NOT NULL
              AND end_date > DATE '1901-01-01'   -- source uses 01-Jan-1900 for "not stated"
              AND DATE_DIFF('day', contract_date, end_date) BETWEEN {MIN_CONTRACT_DAYS} AND 3650
              AND len(string_split(family_key, ' ')) >= 2
        ) WHERE rn = 1
    )"""


def _connect():
    duckdb = duckdb_available()
    if duckdb is None:
        raise RuntimeError("DuckDB is not installed. Run: python -m pip install duckdb")
    return duckdb.connect()


def contracts_ending(root: str | Path, as_of=None, within_days: int = 182, recently_ended_days: int = 0,
                     state: str = "", city: str = "", keyword: str = "", min_value_inr: float = 0,
                     buyer: str = "", limit: int | None = None):
    """Contracts whose stated completion date falls between ``as_of -
    recently_ended_days`` and ``as_of + within_days``, soonest first.

    ``prior_awards`` counts earlier awards by the same buyer for the same
    requirement family, so a contract that has been re-awarded before can be
    told apart from a one-off. ``buyer`` restricts to one exact buyer name.
    """
    as_of = _as_date(as_of)
    root = Path(root)
    filter_sql, params = _filters(state, city, keyword, min_value_inr, buyer)
    start = as_of - _dt.timedelta(days=max(0, int(recently_ended_days)))
    end = as_of + _dt.timedelta(days=max(1, int(within_days)))
    sql = f"""
    WITH {_contracts_cte(root)}
    SELECT c.buyer, c.description, c.incumbent, c.contract_value, c.contract_date, c.end_date,
           c.duration_days, c.num_bids_received, c.state, c.tender_type, c.tender_id, c.tender_ref_no,
           c.tender_document_url,
           (SELECT COUNT(*) FROM awards a
             WHERE a.org_key = c.org_key AND a.family_key = c.family_key
               AND a.contract_date < c.contract_date - INTERVAL 30 DAY) AS prior_awards
    FROM contracts c
    WHERE c.end_date BETWEEN DATE '{start.isoformat()}' AND DATE '{end.isoformat()}'
      AND c.contract_date < DATE '{as_of.isoformat()}'{filter_sql}
    ORDER BY c.end_date, c.contract_value DESC NULLS LAST
    {f'LIMIT {int(limit)}' if limit else ''}
    """
    df = _connect().execute(sql, params).fetchdf()
    for col in ("buyer", "description", "incumbent"):
        df[col] = df[col].map(clean_text)
    return df


def expiry_coverage(root: str | Path) -> dict[str, Any]:
    """How much of the award data carries a usable stated end date."""
    root = Path(root)
    row = _connect().execute(f"""
        WITH {_contracts_cte(root)}
        SELECT (SELECT COUNT(*) FROM contracts),
               (SELECT COUNT(DISTINCT org_key) FROM contracts),
               (SELECT MIN(contract_date) FROM contracts),
               (SELECT MAX(contract_date) FROM contracts),
               (SELECT LIST(buyer ORDER BY n DESC) FROM (
                    SELECT MAX(buyer) AS buyer, COUNT(*) AS n FROM contracts GROUP BY org_key))
    """).fetchone()
    return {"contracts_with_end_date": int(row[0]), "buyers": int(row[1]),
            "first_contract": row[2], "last_contract": row[3],
            "buyer_names": [clean_text(b) for b in (row[4] or [])]}


def backtest_contract_expiry(root: str | Path, as_of, horizon_days: int = 182, tolerance_days: int = 90) -> dict[str, Any]:
    """Does a stated end date predict when the buyer awards the next contract?

    Takes contracts awarded before ``as_of`` whose stated end falls in the
    next ``horizon_days``, then checks the same buyer's later awards for the
    same requirement family. HIT: a new award within ``tolerance_days`` of the
    stated end. BASELINE: an equally wide window placed half the contract
    length (at least ``2 * tolerance_days``) after the end, i.e. at a time the
    end date says is wrong. Only contracts whose baseline window has closed
    by the end of the data are scored.
    """
    import pandas as pd

    as_of = _as_date(as_of)
    root = Path(root)
    horizon = as_of + _dt.timedelta(days=max(1, int(horizon_days)))
    tol = int(tolerance_days)
    df = _connect().execute(f"""
    WITH {_contracts_cte(root)},
    cutoff AS (SELECT quantile_disc(contract_date, 0.999) AS data_end FROM awards),
    cohort AS (
        SELECT org_key, family_key, contract_date, end_date, duration_days,
               end_date + CAST(GREATEST(2 * {tol}, duration_days // 2) AS INTEGER) AS baseline_center
        FROM contracts
        WHERE contract_date < DATE '{as_of.isoformat()}'
          AND end_date BETWEEN DATE '{as_of.isoformat()}' AND DATE '{horizon.isoformat()}'
    )
    SELECT c.*, cutoff.data_end,
           EXISTS (SELECT 1 FROM awards a WHERE a.org_key = c.org_key AND a.family_key = c.family_key
                     AND a.contract_date >= DATE '{as_of.isoformat()}'
                     AND a.contract_date BETWEEN c.end_date - {tol} AND c.end_date + {tol}) AS hit,
           EXISTS (SELECT 1 FROM awards a WHERE a.org_key = c.org_key AND a.family_key = c.family_key
                     AND a.contract_date >= DATE '{as_of.isoformat()}'
                     AND a.contract_date BETWEEN c.baseline_center - {tol} AND c.baseline_center + {tol}) AS baseline_hit,
           EXISTS (SELECT 1 FROM awards a WHERE a.org_key = c.org_key AND a.family_key = c.family_key
                     AND a.contract_date >= DATE '{as_of.isoformat()}'
                     AND a.contract_date <= cutoff.data_end) AS reawarded
    FROM cohort c, cutoff
    """).fetchdf()
    if df.empty:
        return {"as_of": as_of, "data_end": None, "scored": 0, "summary": pd.DataFrame(), "detail": df}
    data_end = df["data_end"].iloc[0]
    data_end = data_end.date() if hasattr(data_end, "date") else data_end
    df["scored"] = (df["baseline_center"] + pd.to_timedelta(tol, unit="D")).dt.date <= data_end
    scored = df[df["scored"]]
    summary = pd.DataFrame([{
        "contracts_tested": len(scored),
        "pending": int((~df["scored"]).sum()),
        "reawarded_by_data_end": round(scored["reawarded"].mean(), 3) if len(scored) else None,
        "hit_rate_near_end_date": round(scored["hit"].mean(), 3) if len(scored) else None,
        "baseline_hit_rate": round(scored["baseline_hit"].mean(), 3) if len(scored) else None,
        "hit_rate_when_reawarded": round(scored[scored["reawarded"]]["hit"].mean(), 3) if scored["reawarded"].any() else None,
    }])
    return {"as_of": as_of, "data_end": data_end, "tolerance_days": tol, "summary": summary,
            "detail": df.drop(columns=["org_key", "family_key", "data_end"])}
