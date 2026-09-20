"""Conservative, explainable radar windows derived from Layer 2 cycles."""
from __future__ import annotations

from datetime import date, timedelta

from db import database as db


def refresh_prediction(cur, requirement_id: int):
    cycle = db.get_latest_cycle(cur, requirement_id)
    if not cycle or cycle["median_interval_days"] is None or not cycle["last_event_date"]:
        return None
    try:
        last_event = date.fromisoformat(cycle["last_event_date"])
    except ValueError:
        return None
    interval = float(cycle["median_interval_days"])
    dispersion = float(cycle["stddev_interval_days"] or 0.0)
    tolerance = max(30.0, dispersion)
    predicted = last_event + timedelta(days=round(interval))
    confidence = "HIGH" if cycle["n_cycles"] >= 4 and dispersion <= interval * 0.15 else "MEDIUM" if cycle["n_cycles"] >= 3 else "LOW"
    return db.upsert_prediction(
        cur, requirement_id=requirement_id, anchor_event_type=cycle["anchor_event_type"],
        predicted_date=predicted.isoformat(),
        window_start=(predicted - timedelta(days=round(tolerance))).isoformat(),
        window_end=(predicted + timedelta(days=round(tolerance))).isoformat(),
        interval_days=interval, dispersion_days=dispersion, confidence=confidence,
    )


def match_discovered_record(cur, record_id: int):
    record = cur.execute("SELECT * FROM discovered_records WHERE id = ?", (record_id,)).fetchone()
    if not record or not record["published_at"]:
        return None
    try:
        published = date.fromisoformat(record["published_at"][:10])
    except ValueError:
        return None
    candidates = db.list_predictions(cur, status="ACTIVE")
    for prediction in candidates:
        if not (prediction["window_start"] <= published.isoformat() <= prediction["window_end"]):
            continue
        if prediction["asset_keyword"].lower() not in (record["title"] or "").lower():
            continue
        db.mark_prediction_matched(cur, prediction["id"], record_id)
        return prediction["id"]
    return None