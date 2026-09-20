"""
Start-date resolution (spec §4.1).

CRITICAL RULE: award_date is never silently treated as commencement_date.
If it is used as a proxy, that fact is stated explicitly in the evidence
trail and confidence is capped at MEDIUM. If nothing usable exists,
resolution returns UNKNOWN rather than guessing (examples #6, #7).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from core.dateutils import parse_date_any


@dataclass
class StartDateResolution:
    start_date: Optional[date]
    confidence: str          # HIGH / MEDIUM / UNKNOWN
    source_fact_id: Optional[int]
    note: str                # human-readable rationale, goes straight into the Proof Packet


def resolve_start_date(facts: list) -> StartDateResolution:
    """
    `facts` is a list of sqlite3.Row (or dict-like) extracted_facts rows
    for one contract, already filtered to non-superseded.

    Resolution order (most to least authoritative, per §4.1):
      1. Explicit calendar start date (base_duration_explicit_end_date's
         start component) — HIGH
      2. commencement_date_explicit — HIGH
      3. site_handover_date — HIGH
      4. award_date used as proxy (no commencement clause found) — MEDIUM
      5. No usable signal — UNKNOWN
    """
    by_type: dict[str, list] = {}
    for f in facts:
        by_type.setdefault(f["fact_type"], []).append(f)

    # 1. Explicit calendar range
    if "base_duration_explicit_end_date" in by_type:
        f = by_type["base_duration_explicit_end_date"][0]
        start_str = f["fact_value"].split("|")[0]
        d = date.fromisoformat(start_str)
        return StartDateResolution(
            start_date=d, confidence="HIGH", source_fact_id=f["id"],
            note="Explicit calendar start date stated directly in the source document "
                 "(outranks arithmetic inference).",
        )

    # 2. Explicit commencement date
    if "commencement_date_explicit" in by_type:
        f = by_type["commencement_date_explicit"][0]
        d = date.fromisoformat(f["fact_value"])
        return StartDateResolution(
            start_date=d, confidence="HIGH", source_fact_id=f["id"],
            note="Explicit commencement date stated in a signed agreement / work order.",
        )

    # 3. Site handover date
    if "site_handover_date" in by_type:
        f = by_type["site_handover_date"][0]
        d = date.fromisoformat(f["fact_value"])
        return StartDateResolution(
            start_date=d, confidence="HIGH", source_fact_id=f["id"],
            note="Site-handover date separately published.",
        )

    # 4. Award date as proxy — explicitly labelled, confidence capped at MEDIUM
    if "award_date" in by_type:
        f = by_type["award_date"][0]
        d = date.fromisoformat(f["fact_value"])
        return StartDateResolution(
            start_date=d, confidence="MEDIUM", source_fact_id=f["id"],
            note="No commencement/site-handover clause found in any linked document; "
                 "award date is being used as a PROXY for start date. This is an "
                 "assumption, not observed evidence — treat the resulting expiry date "
                 "as provisional.",
        )

    # 5. Nothing usable
    return StartDateResolution(
        start_date=None, confidence="UNKNOWN", source_fact_id=None,
        note="No award date, commencement date, or site-handover date found in any "
             "linked document. Start date cannot be determined from available evidence.",
    )
