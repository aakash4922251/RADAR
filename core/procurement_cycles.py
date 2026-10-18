"""
Procurement cycle statistics (spec §6).

Computes measurable historical features for a requirement from its
linked procurement_events — not a single average presented as
"prediction" (that's explicitly disallowed by the spec; Layer 3, not
built here, is where prediction happens). This module only produces the
descriptive statistics Layer 3 will eventually consume.

Anchor event selection: intervals are measured between consecutive
occurrences of ONE event_type (the "anchor"), chosen by a fixed priority
order (tender_published > tender_awarded > contract_started), picking
the first type in that order with at least 2 occurrences. This keeps the
choice deterministic and stated, rather than silently mixing event types
with different real-world timing semantics into one interval series.
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from db import database as db

ANCHOR_PRIORITY = ["tender_published", "tender_awarded", "contract_started"]


@dataclass
class CycleStats:
    requirement_id: int
    anchor_event_type: Optional[str]
    cycle_dates: list[str]           # chronological ISO dates used
    interval_days: list[int]         # gaps between consecutive cycle_dates
    n_cycles: int                    # number of cycle_dates (not intervals)
    median_interval_days: Optional[float]
    mean_interval_days: Optional[float]
    min_interval_days: Optional[float]
    max_interval_days: Optional[float]
    stddev_interval_days: Optional[float]
    last_event_date: Optional[str]
    last_event_type: Optional[str]
    insufficient_data: bool
    note: str = ""


def _pick_anchor(events_by_type: dict[str, list[str]]) -> Optional[str]:
    for candidate in ANCHOR_PRIORITY:
        dates = events_by_type.get(candidate, [])
        if len(dates) >= 2:
            return candidate
    return None


def compute_cycle_stats(cur, requirement_id: int, cutoff_date: Optional[str | date] = None) -> CycleStats:
    events = db.list_events_for_requirement(cur, requirement_id)
    if cutoff_date is not None:
        cutoff_iso = cutoff_date.isoformat() if isinstance(cutoff_date, date) else str(cutoff_date)
        events = [e for e in events if not e["event_date"] or e["event_date"] <= cutoff_iso]

    events_by_type: dict[str, list[str]] = {}
    for e in events:
        if not e["event_date"]:
            continue
        events_by_type.setdefault(e["event_type"], []).append(e["event_date"])
    for k in events_by_type:
        events_by_type[k] = sorted(set(events_by_type[k]))

    last_event_date, last_event_type = None, None
    if events:
        dated = [e for e in events if e["event_date"]]
        if dated:
            latest = max(dated, key=lambda e: e["event_date"])
            last_event_date, last_event_type = latest["event_date"], latest["event_type"]

    anchor = _pick_anchor(events_by_type)
    if anchor is None:
        n = max((len(v) for v in events_by_type.values()), default=0)
        return CycleStats(
            requirement_id=requirement_id, anchor_event_type=None, cycle_dates=[], interval_days=[],
            n_cycles=n, median_interval_days=None, mean_interval_days=None, min_interval_days=None,
            max_interval_days=None, stddev_interval_days=None, last_event_date=last_event_date,
            last_event_type=last_event_type, insufficient_data=True,
            note=(
                "INSUFFICIENT_DATA — fewer than 2 occurrences of any single anchorable event type "
                f"({', '.join(ANCHOR_PRIORITY)}). Historical cycle statistics require at least two "
                "dated occurrences of the same event type to measure an interval at all."
            ),
        )

    cycle_dates = events_by_type[anchor]
    date_objs = [date.fromisoformat(d) for d in cycle_dates]
    intervals = [(date_objs[i + 1] - date_objs[i]).days for i in range(len(date_objs) - 1)]

    if len(intervals) < 1:
        insufficient = True
        median = mean = mn = mx = sd = None
        note = "INSUFFICIENT_DATA — only one dated anchor event; no interval can be measured yet."
    else:
        insufficient = False
        median = statistics.median(intervals)
        mean = statistics.mean(intervals)
        mn = min(intervals)
        mx = max(intervals)
        sd = statistics.stdev(intervals) if len(intervals) >= 2 else 0.0
        note = (
            f"Computed from {len(cycle_dates)} '{anchor}' event(s) across {len(intervals)} interval(s): "
            f"{intervals} days."
        )

    return CycleStats(
        requirement_id=requirement_id, anchor_event_type=anchor, cycle_dates=cycle_dates,
        interval_days=intervals, n_cycles=len(cycle_dates), median_interval_days=median,
        mean_interval_days=mean, min_interval_days=mn, max_interval_days=mx,
        stddev_interval_days=sd, last_event_date=last_event_date, last_event_type=last_event_type,
        insufficient_data=insufficient, note=note,
    )


def recompute_and_store(cur, requirement_id: int) -> CycleStats:
    stats = compute_cycle_stats(cur, requirement_id)
    db.insert_procurement_cycle(
        cur, requirement_id=requirement_id, anchor_event_type=stats.anchor_event_type,
        cycle_dates_json=json.dumps(stats.cycle_dates), interval_days_json=json.dumps(stats.interval_days),
        n_cycles=stats.n_cycles, median_interval_days=stats.median_interval_days,
        mean_interval_days=stats.mean_interval_days, min_interval_days=stats.min_interval_days,
        max_interval_days=stats.max_interval_days, stddev_interval_days=stats.stddev_interval_days,
        last_event_date=stats.last_event_date, last_event_type=stats.last_event_type,
    )
    return stats
