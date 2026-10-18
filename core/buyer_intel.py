"""
Buyer intelligence from public CPPP award details.

For one government buyer: how much it awards, for what, to whom, which of
its contracts end soon, and which requirements it buys again and again.
Every figure is an aggregate of published award records; nothing is
estimated.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core.contract_expiry import clean_text, contracts_ending
from core.dataset_import import _aoc_date_sql, _family_key_sql, _org_key_sql, duckdb_available


def _details_view(con, root: Path) -> None:
    pattern = str(root / "aoc_details" / "year=*" / "data.parquet").replace("\\", "/").replace("'", "''")
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW awards AS
        SELECT
            {_aoc_date_sql("contract_date")} AS contract_date,
            NULLIF(TRIM(organisation_name), '') AS buyer,
            NULLIF(TRIM(tender_description), '') AS description,
            NULLIF(TRIM(selected_bidders), '') AS incumbent,
            NULLIF(contract_value, 0) AS contract_value,
            num_bids_received, tender_id, tender_ref_no, tender_document_url,
            {_org_key_sql("organisation_name")} AS org_key,
            {_family_key_sql("tender_description")} AS family_key
        FROM read_parquet('{pattern}', union_by_name = true)
        WHERE NULLIF(TRIM(organisation_name), '') IS NOT NULL
          AND {_aoc_date_sql("contract_date")} IS NOT NULL
    """)


def _connect(root: Path):
    duckdb = duckdb_available()
    if duckdb is None:
        raise RuntimeError("DuckDB is not installed. Run: python -m pip install duckdb")
    con = duckdb.connect()
    _details_view(con, root)
    return con


def search_buyers(root: str | Path, text: str, limit: int = 25):
    """Buyers whose name contains ``text``, largest by number of awards."""
    con = _connect(Path(root))
    df = con.execute("""
        SELECT buyer, COUNT(*) AS awards, SUM(contract_value) AS total_value, MAX(contract_date) AS last_award
        FROM awards
        WHERE LOWER(buyer) LIKE LOWER(?)
        GROUP BY buyer
        ORDER BY awards DESC
        LIMIT ?
    """, [f"%{text.strip()}%", int(limit)]).fetchdf()
    df["label"] = df["buyer"].map(clean_text)
    return df


# Placeholders the source uses instead of a company name ("Multiple Bidders",
# its "MUTLIPLE BIDDERS AWARDED" misspelling, bare reference numbers).
_NOT_A_SUPPLIER = re.compile(r"^(mu[lt]{2}iple\b.*|na|n/a|nil|none|-+)$|^[^a-z]*$", re.I)


def _split_suppliers(value: str) -> list[str]:
    names = [clean_text(part) for part in (value or "").split(",")]
    return [n for n in names if n and not _NOT_A_SUPPLIER.match(n)]


def buyer_profile(root: str | Path, buyer: str, as_of=None, ending_within_days: int = 365) -> dict[str, Any]:
    """Everything the award records say about one exact buyer name."""
    import pandas as pd

    root = Path(root)
    con = _connect(root)
    s = con.execute("""
        SELECT COUNT(*), SUM(contract_value), COUNT(contract_value), MIN(contract_date), MAX(contract_date),
               MEDIAN(NULLIF(num_bids_received, 0))
        FROM awards WHERE buyer = ?
    """, [buyer]).fetchone()
    summary = {
        "buyer": clean_text(buyer), "awards": int(s[0]), "total_value": float(s[1] or 0),
        "awards_with_value": int(s[2]), "first_award": s[3], "last_award": s[4],
        "median_bids": float(s[5]) if s[5] is not None else None,
    }

    awards = con.execute("""
        SELECT contract_date, description, incumbent, contract_value, num_bids_received,
               tender_id, tender_ref_no, tender_document_url, family_key
        FROM awards WHERE buyer = ?
        ORDER BY contract_date DESC
    """, [buyer]).fetchdf()
    for col in ("description", "incumbent"):
        awards[col] = awards[col].map(clean_text)

    # Suppliers: a multi-winner award counts once for each named winner;
    # value is the contract value they shared in, not their individual share.
    rows = []
    for incumbent, value, date in zip(awards["incumbent"], awards["contract_value"], awards["contract_date"]):
        for name in _split_suppliers(incumbent):
            rows.append((name, value, date))
    suppliers = pd.DataFrame(rows, columns=["supplier", "contract_value", "contract_date"])
    if not suppliers.empty:
        suppliers = (suppliers.groupby(suppliers["supplier"].str.upper())
                     .agg(supplier=("supplier", "first"), contracts_won=("supplier", "size"),
                          value_involved=("contract_value", "sum"), last_win=("contract_date", "max"))
                     .sort_values(["contracts_won", "value_involved"], ascending=False)
                     .reset_index(drop=True))
    summary["distinct_suppliers"] = len(suppliers)

    # A requirement counts as repeated only when it was awarded on separate
    # occasions: lots of one tender share an award date and count once.
    families = awards[awards["family_key"].str.split().str.len() >= 2]
    repeat = (families.groupby("family_key")
              .agg(requirement=("description", "first"), awards=("contract_date", "nunique"),
                   value=("contract_value", "sum"), last_award=("contract_date", "max"),
                   first_award=("contract_date", "min"))
              .query("awards >= 2")
              .sort_values(["awards", "last_award"], ascending=False)
              .reset_index(drop=True))
    repeat = repeat[(repeat["last_award"] - repeat["first_award"]).dt.days >= 60].reset_index(drop=True)

    ending = contracts_ending(root, as_of=as_of, within_days=ending_within_days, buyer=buyer)
    return {
        "summary": summary,
        "ending": ending,
        "repeat_requirements": repeat,
        "suppliers": suppliers,
        "recent_awards": awards.drop(columns=["family_key"]).head(200),
    }
