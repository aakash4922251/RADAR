from __future__ import annotations

import json
from datetime import date
from typing import Any, Optional

from db import database as db


def _event_date(event_id: int, cur):
    event = cur.execute("SELECT * FROM procurement_events WHERE id = ?", (event_id,)).fetchone()
    if not event:
        return None
    return event["event_date"]


def _title_similarity(title_a: Optional[str], title_b: Optional[str]) -> float:
    if not title_a or not title_b:
        return 0.0
    a = (title_a or '').lower().replace("-", " ").split()
    b = (title_b or '').lower().replace("-", " ").split()
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    if not common:
        return 0.0
    return min(1.0, len(common) / max(len(a), len(b)))


def match_prediction_event(cur, requirement_id: int, event_id: int, prediction_id: Optional[int] = None,
                          org_id: Optional[int] = None, title_text: Optional[str] = None,
                          event_date: Optional[str] = None):
    event = cur.execute("SELECT * FROM procurement_events WHERE id = ?", (event_id,)).fetchone()
    if not event:
        return {"status": "UNRELATED_TENDER", "score": 0.0, "factors": {"event": "event_id not found"}, "explanation": "UNRELATED_TENDER — no procurement event matched the requirement."}

    requirement = db.get_requirement(cur, requirement_id)
    if requirement is None:
        return {"status": "UNRELATED_TENDER", "score": 0.0, "factors": {"requirement": "missing requirement"}, "explanation": "UNRELATED_TENDER — requirement not found."}

    event_dt = event_date or event["event_date"]
    event_date_obj = date.fromisoformat(event_dt) if event_dt else None

    prediction = db.get_prediction(cur, prediction_id) if prediction_id else db.get_latest_prediction_for_requirement(cur, requirement_id)
    if prediction is not None:
        window_start = prediction["predicted_window_start"]
        window_end = prediction["predicted_window_end"]
        if event_date_obj is not None and window_start and window_end:
            inside_window = window_start <= event_dt <= window_end
            near_window = False
            try:
                start_d = date.fromisoformat(window_start)
                end_d = date.fromisoformat(window_end)
                if abs((event_date_obj - ((start_d + (end_d - start_d) / 2))).days) <= 90:
                    near_window = True
            except Exception:
                near_window = False
        else:
            inside_window = False
            near_window = False
    else:
        inside_window = False
        near_window = False

    required_factors = 0
    factors = {}
    if org_id is not None and requirement["org_id"] == org_id:
        factors["organisation"] = "same organisation"
        required_factors += 1
    else:
        factors["organisation"] = "organisation not confirmed"

    title = title_text or requirement["normalized_title"]
    if title:
        similarity = _title_similarity(title, requirement["normalized_title"])
        factors["title_similarity"] = f"{similarity:.2f}"
        if similarity >= 0.5:
            required_factors += 1

    if event["event_type"] in {"tender_published", "tender_awarded", "contract_started"}:
        factors["event_type"] = event["event_type"]
        required_factors += 1

    if inside_window:
        factors["timing"] = "inside predicted window"
        required_factors += 2
    elif near_window:
        factors["timing"] = "near predicted window"
        required_factors += 1
    else:
        factors["timing"] = "outside predicted window"

    score = min(1.0, required_factors / 5.0)

    if score >= 0.6 and (inside_window or near_window or required_factors >= 3):
        status = "CONFIRMED_MATCH"
        explanation = "CONFIRMED_MATCH — multiple supporting signals align: organisation, event type, title similarity, and timing are all coherent with the predicted procurement window."
    elif score >= 0.45 and required_factors >= 3:
        status = "PROBABLE_MATCH"
        explanation = "PROBABLE_MATCH — several supporting signals align, but the evidence remains below the threshold for a confirmed match."
    elif score < 0.35:
        status = "UNRELATED_TENDER"
        explanation = "UNRELATED_TENDER — the event lacks enough support to be treated as a matching procurement for this requirement."
    else:
        status = "REVIEW_REQUIRED"
        explanation = "REVIEW_REQUIRED — the event is not clearly unrelated, but does not yet satisfy the multi-signal threshold for a formal match."

    # Explicit guardrail: same org + same word alone is not enough.
    if org_id is not None and requirement["org_id"] == org_id and title_text:
        title_similarity = _title_similarity(title_text, requirement["normalized_title"])
        if title_similarity < 0.6 and required_factors <= 3:
            status = "REVIEW_REQUIRED" if score >= 0.35 else "UNRELATED_TENDER"
            explanation = "REVIEW_REQUIRED — same organisation and a weak title match are not sufficient by themselves; the system requires multiple supporting signals before treating this as a match."

    result = {
        "status": status,
        "score": round(score, 3),
        "factors": factors,
        "explanation": explanation,
        "event_id": event_id,
        "requirement_id": requirement_id,
        "prediction_id": prediction_id or (prediction["prediction_id"] if prediction else None),
    }

    if prediction_id is not None:
        db.insert_prediction_match(
            cur,
            prediction_id=prediction_id,
            event_id=event_id,
            match_status=status,
            match_score=result["score"],
            match_evidence=json.dumps(factors),
            matched_at="datetime('now')",
        )
    return result
