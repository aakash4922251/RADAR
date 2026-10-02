from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from core.prediction_engine import build_prediction_for_requirement
from core.procurement_cycles import compute_cycle_stats
from db import database as db


def backtest_requirement_as_of(cur, requirement_id: int, as_of_date):
    cutoff = as_of_date if isinstance(as_of_date, str) else as_of_date.isoformat()
    stats = compute_cycle_stats(cur, requirement_id, cutoff_date=cutoff)
    prediction = build_prediction_for_requirement(cur, requirement_id, as_of_date=cutoff)

    future_events = cur.execute(
        """
        SELECT pe.event_date
        FROM procurement_events pe
        JOIN requirement_event_links rel ON rel.event_id = pe.id
        WHERE rel.requirement_id = ? AND pe.event_date IS NOT NULL AND pe.event_date > ?
        ORDER BY pe.event_date ASC
        LIMIT 1
        """,
        (requirement_id, cutoff),
    ).fetchone()
    actual_event_date = future_events["event_date"] if future_events else None

    if prediction["predicted_window_start"] and prediction["predicted_window_end"] and actual_event_date:
        start = date.fromisoformat(prediction["predicted_window_start"])
        end = date.fromisoformat(prediction["predicted_window_end"])
        matched = start <= date.fromisoformat(actual_event_date) <= end
        timing_error_days = (date.fromisoformat(actual_event_date) - ((start + (end - start) / 2))).days
    else:
        matched = False
        timing_error_days = None

    return {
        "requirement_id": requirement_id,
        "as_of_date": cutoff,
        "used_events": stats.n_cycles,
        "cycle_stats": stats,
        "prediction": prediction,
        "actual_event_date": actual_event_date,
        "match_status": "MATCH" if matched else "NO_MATCH",
        "timing_error_days": timing_error_days,
    }


def evaluate_backtest_results(cur, results: Iterable[dict], minimum_dataset_size: int = 10):
    rows = list(results)
    dataset_size = len(rows)
    confirmed = sum(1 for r in rows if (r.get("match_status") or "").upper() in {"CONFIRMED","MATCH"})
    probable = sum(1 for r in rows if (r.get("match_status") or "").upper() == "PROBABLE")
    unmatched = sum(1 for r in rows if (r.get("match_status") or "").upper() in {"NO_MATCH", "UNMATCHED"})

    if dataset_size < minimum_dataset_size:
        return {
            "dataset_size": dataset_size,
            "predictions_evaluated": dataset_size,
            "confirmed_matches": confirmed,
            "probable_matches": probable,
            "unmatched": unmatched,
            "precision": None,
            "recall": None,
            "average_lead_time_days": None,
            "status": "NOT_ENOUGH_DATA_FOR_RELIABLE_PERFORMANCE_ESTIMATE",
            "notes": f"Dataset size is {dataset_size}, below the minimum threshold of {minimum_dataset_size}; no reliable performance estimate is reported.",
        }

    precision = (confirmed + probable) / max(1, dataset_size)
    recall = confirmed / max(1, dataset_size)
    lead_times = [float(r.get("timing_error_days") or 0) for r in rows if r.get("timing_error_days") is not None]
    avg_lead = sum(lead_times) / max(1, len(lead_times)) if lead_times else 0.0

    return {
        "dataset_size": dataset_size,
        "predictions_evaluated": dataset_size,
        "confirmed_matches": confirmed,
        "probable_matches": probable,
        "unmatched": unmatched,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "average_lead_time_days": round(avg_lead, 2),
        "status": "EVALUATED",
        "notes": f"Evaluated {dataset_size} backtested predictions; dataset size meets the minimum threshold of {minimum_dataset_size}.",
    }
