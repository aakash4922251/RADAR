"""
Confidence engine (spec §4.5 / §3.6).

Deliberately NOT a weighted additive score. A missing or LOW dimension
must be able to veto the overall result — that's the whole point of the
research finding in the spec ("a weighted sum could report a confident
date built on an unknown start"). Only a rule-based floor guarantees a
veto; a sum cannot.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from core.expiry_engine import ExpiryResult

_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}

# doc_type -> source reliability, per the revised evidence hierarchy (§4.3)
_DOC_TYPE_RELIABILITY = {
    "signed_agreement": "HIGH",
    "work_order": "HIGH",
    "aoc": "HIGH",
    "loa": "HIGH",
    "nit": "HIGH",
    "bid_document": "HIGH",
    "sla_stc": "MEDIUM",
    "corrigendum": "MEDIUM",
    "platform_amendment": "MEDIUM",
    "site_handover_letter": "HIGH",
    "termination_notice": "HIGH",
    "department_page": "LOW",
    "other": "LOW",
}


@dataclass
class ConfidenceAssessment:
    award_date_confidence: str
    duration_confidence: str
    start_date_confidence: str
    source_reliability: str
    cross_source_verified: bool
    cross_source_detail: str
    expiry_confidence: str
    limiting_factor: str


def _source_reliability(facts: list, documents_by_id: dict) -> str:
    doc_types = set()
    for f in facts:
        doc = documents_by_id.get(f["document_id"])
        if doc:
            doc_types.add(doc["doc_type"])
    if not doc_types:
        return "UNKNOWN"
    reliabilities = [_DOC_TYPE_RELIABILITY.get(dt, "LOW") for dt in doc_types]
    # source reliability of the *set* is the weakest link that mattered,
    # i.e. the lowest reliability among documents actually feeding facts
    return min(reliabilities, key=lambda r: _RANK[r])


def _cross_source_check(facts: list) -> tuple[bool, str]:
    by_type: dict[str, set] = {}
    for f in facts:
        by_type.setdefault(f["fact_type"], set()).add(f["document_id"])
    corroborated_types = [ft for ft, docs in by_type.items() if len(docs) > 1]
    if corroborated_types:
        return True, f"Corroborated across multiple documents for: {', '.join(corroborated_types)}."
    distinct_docs = {f["document_id"] for f in facts}
    if len(distinct_docs) > 1:
        return False, f"{len(distinct_docs)} documents reviewed but no single fact type independently corroborated across them."
    return False, "Only one source document reviewed; no cross-source verification possible."


def assess_confidence(expiry_result: ExpiryResult, facts: list, documents_by_id: dict,
                       award_date_fact: Optional[dict]) -> ConfidenceAssessment:
    # An explicit calendar start-and-end date pair (spec §4.3 rank 1) fully
    # determines the expiry on its own — it does not depend on an award
    # date at all. Gating on a missing award_date in that case would wrongly
    # force UNKNOWN on a result that is actually the MOST reliable evidence
    # type available. Detect this case: explicit-range facts leave
    # duration_months as None while still reporting HIGH duration confidence.
    explicit_range_used = expiry_result.duration_months is None and expiry_result.duration_confidence == "HIGH"

    award_conf = award_date_fact["fact_confidence"] if award_date_fact else "UNKNOWN"
    duration_conf = expiry_result.duration_confidence
    start_conf = expiry_result.start_resolution.confidence
    source_rel = _source_reliability(facts, documents_by_id)
    cross_verified, cross_detail = _cross_source_check(facts)
    has_conflict = expiry_result.duration_conflict

    # For the floor logic only (not for display): when an explicit calendar
    # range determined the expiry, award_date is not applicable and must
    # not gate the result down to UNKNOWN. award_date_confidence as
    # displayed/persisted stays whatever it genuinely is (often UNKNOWN,
    # meaning "not found" — which is simply true and shown honestly).
    award_gate = "HIGH" if explicit_range_used else award_conf

    dims = {
        "award_date": award_gate,
        "duration": duration_conf,
        "start_date": start_conf,
        "source_reliability": source_rel,
    }

    # --- rule-based floor, per §4.5 -------------------------------------
    if duration_conf == "UNKNOWN" or award_gate == "UNKNOWN":
        expiry_conf = "UNKNOWN"
        limiting = "duration" if duration_conf == "UNKNOWN" else "award_date"
    elif has_conflict:
        expiry_conf = "LOW"
        limiting = "duration (conflicting values across sources, unresolved)"
    elif any(v == "LOW" for v in dims.values()) or start_conf == "UNKNOWN":
        low_dims = [k for k, v in dims.items() if v == "LOW"]
        limiting = low_dims[0] if low_dims else "start_date"
        expiry_conf = "LOW"
    elif (award_gate == "HIGH" and duration_conf == "HIGH"
          and start_conf in ("HIGH", "MEDIUM") and source_rel == "HIGH"
          and not has_conflict):
        expiry_conf = "HIGH"
        limiting = "none — all dimensions at or above threshold"
        if start_conf == "MEDIUM":
            expiry_conf = "MEDIUM"
            limiting = "start_date (award date used as proxy)"
    elif all(_RANK[v] >= _RANK["MEDIUM"] for v in dims.values()) and not has_conflict:
        expiry_conf = "MEDIUM"
        med_dims = [k for k, v in dims.items() if v == "MEDIUM"]
        limiting = med_dims[0] if med_dims else "start_date"
    else:
        expiry_conf = "LOW"
        limiting = "unresolved combination of factors"

    return ConfidenceAssessment(
        award_date_confidence=award_conf,
        duration_confidence=duration_conf,
        start_date_confidence=start_conf,
        source_reliability=source_rel,
        cross_source_verified=cross_verified,
        cross_source_detail=cross_detail,
        expiry_confidence=expiry_conf,
        limiting_factor=limiting,
    )


def derive_status(assessment: ConfidenceAssessment, duration_conflict: bool,
                   has_open_conflict: bool) -> str:
    """Maps confidence + conflict state to the customer-facing contracts.status enum."""
    if has_open_conflict or duration_conflict:
        return "CONFLICTING_EVIDENCE"
    if assessment.expiry_confidence == "HIGH":
        return "HIGH_CONFIDENCE"
    if assessment.expiry_confidence == "MEDIUM":
        return "NEEDS_REVIEW" if assessment.start_date_confidence == "UNKNOWN" else "VERIFIED"
    if assessment.expiry_confidence == "LOW":
        return "NEEDS_REVIEW"
    return "INSUFFICIENT_EVIDENCE"
