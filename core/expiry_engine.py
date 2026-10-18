"""
Expiry engine.

Computes expiry ONLY from traceable evidence (extracted_facts rows).
Never guesses: if duration or start date cannot be determined, the
result is UNKNOWN, not a best-effort date.

Implements the exact representation required by spec §4.6:
  base_expiry             = start_date + base_duration
  extension_option_N      = kept separate, never folded into base
  current_expiry_estimate = base_expiry, UNLESS direct evidence of an
                             exercised extension exists
  current_expiry_ceiling  = base_expiry + all unexercised extension
                             options (the "if everything were exercised"
                             maximum), capped by any absolute_expiry_ceiling
                             fact if one exists
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from core.dateutils import add_months
from core.start_date import resolve_start_date, StartDateResolution


@dataclass
class ExpiryResult:
    start_resolution: StartDateResolution
    duration_months: Optional[float]
    duration_confidence: str            # HIGH / MEDIUM / LOW / UNKNOWN
    duration_source_fact_id: Optional[int]
    duration_conflict: bool             # True if multiple divergent duration facts found
    conflicting_fact_ids: list[int]
    base_expiry: Optional[date]
    base_formula: str
    current_expiry_estimate: Optional[date]
    current_expiry_ceiling: Optional[date]
    extension_option_months: list[int]
    extension_exercised: bool
    exercised_fact_id: Optional[int]
    input_fact_ids: list[int]
    calculation_type: str               # base / with_extension / revised_post_amendment


def _facts_by_type(facts: list) -> dict[str, list]:
    out: dict[str, list] = {}
    for f in facts:
        out.setdefault(f["fact_type"], []).append(f)
    return out


def compute_expiry(facts: list) -> ExpiryResult:
    by_type = _facts_by_type(facts)
    start_res = resolve_start_date(facts)

    # ---- explicit calendar end date takes total precedence -------------
    if "base_duration_explicit_end_date" in by_type:
        f = by_type["base_duration_explicit_end_date"][0]
        start_str, end_str = f["fact_value"].split("|")
        end_d = date.fromisoformat(end_str)
        result = ExpiryResult(
            start_resolution=start_res,
            duration_months=None,
            duration_confidence="HIGH",
            duration_source_fact_id=f["id"],
            duration_conflict=False,
            conflicting_fact_ids=[],
            base_expiry=end_d,
            base_formula=f"Explicit calendar dates stated in source: {start_str} to {end_str}",
            current_expiry_estimate=end_d,
            current_expiry_ceiling=end_d,
            extension_option_months=[],
            extension_exercised=False,
            exercised_fact_id=None,
            input_fact_ids=[f["id"]],
            calculation_type="base",
        )
        _apply_extensions_and_exercise(result, by_type)
        return result

    # ---- duration-based calculation -------------------------------------
    duration_facts = by_type.get("base_duration_months", [])
    duration_conflict = False
    conflicting_ids: list[int] = []
    duration_months = None
    duration_confidence = "UNKNOWN"
    duration_source_id = None

    if duration_facts:
        # prefer HIGH-confidence facts (composite/explicit rules) over MEDIUM (simple pattern)
        high = [f for f in duration_facts if f["fact_confidence"] == "HIGH"]
        pool = high if high else duration_facts
        distinct_values = {f["fact_value"] for f in pool}
        if len(distinct_values) > 1:
            duration_conflict = True
            conflicting_ids = [f["id"] for f in pool]
            # conservative: do not silently pick one — but still surface the
            # lowest (most conservative) value as a provisional estimate,
            # confidence capped at LOW, pending human review.
            chosen = min(pool, key=lambda f: float(f["fact_value"]))
            duration_confidence = "LOW"
        else:
            chosen = pool[0]
            duration_confidence = chosen["fact_confidence"]
        duration_months = float(chosen["fact_value"])
        duration_source_id = chosen["id"]

    if duration_months is None or start_res.start_date is None:
        result = ExpiryResult(
            start_resolution=start_res,
            duration_months=duration_months,
            duration_confidence=duration_confidence if duration_months is not None else "UNKNOWN",
            duration_source_fact_id=duration_source_id,
            duration_conflict=duration_conflict,
            conflicting_fact_ids=conflicting_ids,
            base_expiry=None,
            base_formula="UNKNOWN — " + (
                "no defensible start date found" if start_res.start_date is None
                else "no accepted contract-duration clause found"
            ),
            current_expiry_estimate=None,
            current_expiry_ceiling=None,
            extension_option_months=[],
            extension_exercised=False,
            exercised_fact_id=None,
            input_fact_ids=[fid for fid in [duration_source_id, start_res.source_fact_id] if fid],
            calculation_type="base",
        )
        return result

    base_expiry = add_months(start_res.start_date, round(duration_months))
    formula = f"{start_res.start_date.isoformat()} + {duration_months:g} months = {base_expiry.isoformat()}"

    result = ExpiryResult(
        start_resolution=start_res,
        duration_months=duration_months,
        duration_confidence=duration_confidence,
        duration_source_fact_id=duration_source_id,
        duration_conflict=duration_conflict,
        conflicting_fact_ids=conflicting_ids,
        base_expiry=base_expiry,
        base_formula=formula,
        current_expiry_estimate=base_expiry,
        current_expiry_ceiling=base_expiry,
        extension_option_months=[],
        extension_exercised=False,
        exercised_fact_id=None,
        input_fact_ids=[fid for fid in [duration_source_id, start_res.source_fact_id] if fid],
        calculation_type="base",
    )
    _apply_extensions_and_exercise(result, by_type)
    return result


def _apply_extensions_and_exercise(result: ExpiryResult, by_type: dict) -> None:
    """Mutates `result` in place to add extension ceiling + apply exercised
    extension evidence, per §4.6. current_expiry_estimate NEVER becomes the
    ceiling unless direct exercise evidence exists."""
    option_facts = by_type.get("extension_option_months", [])
    months_list = []
    for f in option_facts:
        try:
            months_list.append(int(float(f["fact_value"])))
        except ValueError:
            continue
    result.extension_option_months = months_list

    if result.base_expiry is not None and months_list:
        ceiling_months = sum(months_list)
        # open-ended ("renewable annually", count=-1) — ceiling is indefinite;
        # we still show a 1-cycle ceiling but this should be flagged in UI.
        ceiling = add_months(result.base_expiry, ceiling_months)
        result.current_expiry_ceiling = ceiling

    # absolute ceiling fact caps everything, if present
    abs_ceiling_facts = by_type.get("absolute_expiry_ceiling", [])
    if abs_ceiling_facts and result.current_expiry_ceiling:
        abs_date = date.fromisoformat(abs_ceiling_facts[0]["fact_value"])
        if abs_date < result.current_expiry_ceiling:
            result.current_expiry_ceiling = abs_date

    # exercised extension evidence overrides current_expiry_estimate only
    exercised_end = by_type.get("extension_exercised_new_end_date", [])
    if exercised_end:
        f = exercised_end[0]
        new_end = date.fromisoformat(f["fact_value"])
        result.current_expiry_estimate = new_end
        result.extension_exercised = True
        result.exercised_fact_id = f["id"]
        result.calculation_type = "with_extension"
        result.input_fact_ids.append(f["id"])
        if result.current_expiry_ceiling and new_end > result.current_expiry_ceiling:
            result.current_expiry_ceiling = new_end
