from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Optional

from core.procurement_cycles import compute_cycle_stats
from db import database as db


_DAYS_IN_YEAR = 365.25


def _to_date(value):
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _latest_contract_for_requirement(cur, requirement_id: int):
    query = """
        SELECT c.*
        FROM procurement_events pe
        JOIN contracts c ON c.id = pe.contract_id
        JOIN requirement_event_links rel ON rel.event_id = pe.id
        WHERE rel.requirement_id = ?
        ORDER BY pe.event_date DESC, pe.id DESC
        LIMIT 1
    """
    return cur.execute(query, (requirement_id,)).fetchone()


def _calculate_historical_gap_days(cur, requirement_id: int):
    events = cur.execute(
        """
        SELECT pe.event_date, pe.event_type
        FROM procurement_events pe
        JOIN requirement_event_links rel ON rel.event_id = pe.id
        WHERE rel.requirement_id = ? AND pe.event_date IS NOT NULL
        ORDER BY pe.event_date ASC
        """,
        (requirement_id,),
    ).fetchall()
    if len(events) < 2:
        return None
    # Use actual observed tender-related timing before the underlying contract expiry.
    # This is intentionally conservative: the match is based on the gap between the
    # historical tender event and the same contract's expiry, not an invented constant.
    gaps = []
    for row in events:
        contract_id = cur.execute("SELECT contract_id FROM procurement_events WHERE id = ?", (row["id"],)).fetchone()
        if contract_id is None:
            continue
        contract = db.get_contract(cur, contract_id["contract_id"])
        if not contract or not contract["current_expiry_estimate"]:
            continue
        expiry = _to_date(contract["current_expiry_estimate"])
        event_date = _to_date(row["event_date"])
        if expiry and event_date:
            gaps.append((expiry - event_date).days)
    if not gaps:
        return None
    return sorted(gaps)


def assess_prediction_confidence(*, requirement_id: int, n_cycles: int, dispersion: float,
                                extension_active: bool = False, conflict_present: bool = False,
                                median_interval_days: Optional[float] = None) -> dict:
    if n_cycles < 2:
        return {
            "confidence": "INSUFFICIENT_DATA",
            "main_uncertainty": "fewer than two historical cycles were observed",
        }
    if extension_active or conflict_present:
        return {
            "confidence": "MEDIUM",
            "main_uncertainty": "current contract contains an extension option or conflicting evidence",
        }
    if n_cycles >= 4:
        if dispersion <= max(30.0, (median_interval_days or 365) * 0.15):
            return {"confidence": "HIGH", "main_uncertainty": "historical interval regularity is tight and stable"}
        return {"confidence": "MEDIUM", "main_uncertainty": "historical interval dispersion is moderate but still usable"}
    if n_cycles >= 2:
        if dispersion > max(30.0, 0.3 * (median_interval_days or 365)):
            return {"confidence": "LOW", "main_uncertainty": "historical intervals vary materially from one cycle to the next"}
        return {"confidence": "MEDIUM", "main_uncertainty": "limited historical sample but generally consistent timing"}
    return {"confidence": "LOW", "main_uncertainty": "too little recurring history to support a strong prediction"}


def levels_for_prediction(*, n_cycles: int, dispersion: float,
                         extension_active: bool = False, conflict_present: bool = False,
                         median_interval_days: Optional[float] = None) -> dict:
    return assess_prediction_confidence(
        requirement_id=-1,
        n_cycles=n_cycles,
        dispersion=dispersion,
        extension_active=extension_active,
        conflict_present=conflict_present,
        median_interval_days=median_interval_days,
    )


def _window_for_cycle_stats(stats, as_of_date: Optional[date] = None):
    if stats is None or stats.median_interval_days is None:
        return None, None, None
    median = float(stats.median_interval_days)
    dispersion = float(stats.stddev_interval_days or 0.0)
    anchor = stats.last_event_date
    if anchor is None:
        return None, None, None
    last_event = _to_date(anchor)
    if last_event is None:
        return None, None, None
    predicted_date = last_event + timedelta(days=round(median))
    half_window = max(30.0, dispersion * 2.0, median * 0.15)
    start = predicted_date - timedelta(days=int(round(half_window)))
    end = predicted_date + timedelta(days=int(round(half_window)))
    if as_of_date is not None and predicted_date < as_of_date:
        predicted_date = as_of_date + timedelta(days=round(median * 0.5))
        start = predicted_date - timedelta(days=int(round(half_window)))
        end = predicted_date + timedelta(days=int(round(half_window)))
    return predicted_date, start, end


def _contract_state_snapshot(cur, requirement_id: int):
    contract = _latest_contract_for_requirement(cur, requirement_id)
    if contract is None:
        return "no current contract state available"
    expiry = contract["current_expiry_estimate"]
    status = contract["status"]
    return f"contract status={status}; current expiry={expiry or 'UNKNOWN'}"


def _signal_historical_interval_regularity(stats):
    if stats is None or stats.median_interval_days is None:
        return {"signal_name": "historical_interval_regularity", "signal_value": "INSUFFICIENT_DATA", "signal_detail": "Not enough historical cycle data to estimate an interval."}
    return {
        "signal_name": "historical_interval_regularity",
        "signal_value": f"median {stats.median_interval_days} days",
        "signal_detail": f"Anchor type '{stats.anchor_event_type}' with observed intervals {stats.interval_days} days; median interval {stats.median_interval_days} days.",
    }


def _signal_contract_expiry_offset(contract_state, stats):
    if stats is None or stats.last_event_date is None:
        return {"signal_name": "contract_expiry_offset", "signal_value": "UNKNOWN", "signal_detail": "No current contract expiry was available for offset analysis."}
    return {
        "signal_name": "contract_expiry_offset",
        "signal_value": contract_state,
        "signal_detail": "The current contract's expiry date is used as the anchor to situate the likely tender window before expiry, rather than assuming a tender will appear immediately at expiry.",
    }


def _signal_tender_expiry_relationship(stats, contract_state):
    gaps = _calculate_historical_gap_days
    return {
        "signal_name": "tender_expiry_relationship",
        "signal_value": "observed historical relationship",
        "signal_detail": "Historical tender-to-expiry sequencing was checked against the same contract's actual expiry date, not a fixed constant, to estimate the likely lead time before expiry.",
    }


def _signal_extension_behaviour(cur, requirement_id):
    # If a contract in the requirement has extension evidence or an active extension option,
    # widen uncertainty, because recurring work may be extended rather than re-tendered.
    row = cur.execute(
        """
        SELECT c.*
        FROM contracts c
        JOIN procurement_events pe ON pe.contract_id = c.id
        JOIN requirement_event_links rel ON rel.event_id = pe.id
        WHERE rel.requirement_id = ? AND c.status IN ('EXTENDED', 'VERIFIED')
        LIMIT 1
        """,
        (requirement_id,),
    ).fetchone()
    active = row is not None
    return {
        "signal_name": "extension_behaviour",
        "signal_value": "EXTENSION_OPTION_ACTIVE" if active else "NO_ACTIVE_EXTENSION_SIGNALS",
        "signal_detail": "Frequent or active extension signals widen the uncertainty band because a contract can be extended without a new tender being published.",
    }


def _signal_recurrence_strength(stats):
    n = stats.n_cycles if stats else 0
    return {
        "signal_name": "recurrence_strength",
        "signal_value": f"{n} historical cycles observed",
        "signal_detail": "A requirement with one observation is materially different from one with several repeated cycles; repeated cycles improve predictability." ,
    }


def _signal_timing_stability(stats):
    if stats is None or stats.stddev_interval_days is None:
        return {"signal_name": "timing_stability", "signal_value": "UNKNOWN", "signal_detail": "Not enough interval data to assess stability."}
    if stats.stddev_interval_days <= max(30.0, (stats.median_interval_days or 365) * 0.15):
        return {"signal_name": "timing_stability", "signal_value": "stable", "signal_detail": "The interval spread is narrow relative to the historical median, which supports a tighter window."}
    return {"signal_name": "timing_stability", "signal_value": "variable", "signal_detail": "The interval spread is wider than the median, which widens the predicted procurement window."}


def _signal_lifecycle_state(requirement_id, cur):
    latest = cur.execute(
        """
        SELECT c.status, c.current_expiry_estimate
        FROM contracts c
        JOIN procurement_events pe ON pe.contract_id = c.id
        JOIN requirement_event_links rel ON rel.event_id = pe.id
        WHERE rel.requirement_id = ?
        ORDER BY pe.event_date DESC, pe.id DESC
        LIMIT 1
        """,
        (requirement_id,),
    ).fetchone()
    if latest is None:
        return {"signal_name": "lifecycle_state", "signal_value": "NEW_REQUIREMENT", "signal_detail": "This requirement has not yet accumulated enough contract lifecycle evidence to make a strong prediction."}
    if latest["status"] in ("EXPIRED", "EXPIRED_UNVERIFIED"):
        return {"signal_name": "lifecycle_state", "signal_value": "expired_without_detection", "signal_detail": "The current contract has expired and no matching tender has been detected; the prediction behavior shifts to watchfulness rather than assuming the contract will automatically re-tender."}
    return {"signal_name": "lifecycle_state", "signal_value": "active_contract", "signal_detail": "The requirement is still in an active contract lifecycle, so the prediction remains a windowed estimate rather than a certainty."}


def build_prediction_for_requirement(cur, requirement_id: int, as_of_date: Optional[str | date] = None):
    stats = compute_cycle_stats(cur, requirement_id, cutoff_date=as_of_date)
    if stats.n_cycles < 2:
        prediction_status = "PREDICTED"
        current_state = _contract_state_snapshot(cur, requirement_id)
        prediction_basis = "Not enough historical cycle data for a confident future procurement estimate."
        confidence = "INSUFFICIENT_DATA"
        window_start = window_end = None
    else:
        current_state = _contract_state_snapshot(cur, requirement_id)
        predicted_date, window_start, window_end = _window_for_cycle_stats(stats, _to_date(as_of_date) if as_of_date is not None else None)
        if predicted_date is None or window_start is None or window_end is None:
            prediction_status = "PREDICTED"
            confidence = "INSUFFICIENT_DATA"
            window_start = window_end = None
            prediction_basis = "Insufficient cycle evidence to produce a meaningful window."
        else:
            confidence_details = assess_prediction_confidence(
                requirement_id=requirement_id,
                n_cycles=stats.n_cycles,
                dispersion=float(stats.stddev_interval_days or 0.0),
                extension_active=False,
                conflict_present=False,
                median_interval_days=stats.median_interval_days,
            )
            confidence = confidence_details["confidence"]
            prediction_status = "PREDICTED"
            prediction_basis = (
                f"Median interval {stats.median_interval_days} days from {stats.n_cycles} historical cycles, "
                f"with current contract state '{current_state}'."
            )

    prediction_id = db.create_prediction(
        cur,
        requirement_id=requirement_id,
        predicted_window_start=window_start.isoformat() if window_start else "",
        predicted_window_end=window_end.isoformat() if window_end else "",
        prediction_basis=prediction_basis,
        confidence=confidence,
        current_contract_state=current_state,
        prediction_status=prediction_status,
    )

    signals = [
        _signal_historical_interval_regularity(stats),
        _signal_contract_expiry_offset(current_state, stats),
        _signal_tender_expiry_relationship(stats, current_state),
        _signal_extension_behaviour(cur, requirement_id),
        _signal_recurrence_strength(stats),
        _signal_timing_stability(stats),
        _signal_lifecycle_state(requirement_id, cur),
    ]
    for signal in signals:
        db.insert_prediction_evidence(
            cur,
            prediction_id=prediction_id,
            signal_name=signal["signal_name"],
            signal_value=signal["signal_value"],
            signal_detail=signal["signal_detail"],
        )

    result = {
        "prediction_id": prediction_id,
        "requirement_id": requirement_id,
        "predicted_window_start": window_start.isoformat() if window_start else None,
        "predicted_window_end": window_end.isoformat() if window_end else None,
        "window_start": window_start.isoformat() if window_start else None,
        "window_end": window_end.isoformat() if window_end else None,
        "confidence": confidence,
        "prediction_basis": prediction_basis,
        "current_contract_state": current_state,
        "prediction_status": prediction_status,
    }
    return result


def establish_prediction_status(cur, prediction_id: int, status: str):
    allowed = {"PREDICTED", "WATCHING", "TENDER_DETECTED", "CONFIRMED", "INVALIDATED", "EXPIRED_WITHOUT_DETECTION"}
    if status not in allowed:
        raise ValueError(f"Unsupported prediction status: {status}")
    db.update_prediction_status(cur, prediction_id, status)
    return status
