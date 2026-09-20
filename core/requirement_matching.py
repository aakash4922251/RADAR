"""
Requirement matching (spec §7/§8).

Scores a candidate requirement signature against an existing requirement
row and returns an explainable result — every match carries the factors
that produced it, never a bare boolean.

Hard gates:
  - asset_keyword must match exactly, always.
  - organisation: a CONFIRMED conflict (both sides resolved to a
    different org_id) is a hard gate failure — merging two contracts
    known to belong to different organisations is exactly the false
    merge the spec warns against. But organisation being UNRESOLVED on
    one or both sides is NOT treated the same as a conflict: it simply
    earns no organisation-match bonus, and the decision falls to the
    remaining signals (title similarity, location). Treating "unknown"
    as a hard failure would make Layer 2 unusable whenever organisation
    extraction hasn't fired — which is common — while still being far
    weaker evidence than a genuine same-org confirmation.

Vendor identity is intentionally NOT a factor here at all — per spec §7,
a vendor changing must never by itself create or block a requirement
match.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional

STRONG_MATCH_THRESHOLD = 0.85
PROBABLE_MATCH_THRESHOLD = 0.65
REVIEW_REQUIRED_THRESHOLD = 0.45  # below this (but past the hard gates), still surfaced, not silently linked


@dataclass
class MatchResult:
    status: str                    # STRONG_MATCH / PROBABLE_MATCH / REVIEW_REQUIRED / NOT_MATCHED
    score: float
    factors: dict = field(default_factory=dict)   # factor_name -> human-readable detail
    explanation: str = ""


def match_requirement(
    *,
    candidate_org_id: Optional[int],
    candidate_asset_keyword: Optional[str],
    candidate_location: Optional[str],
    candidate_normalized_title: str,
    existing_org_id: Optional[int],
    existing_asset_keyword: str,
    existing_location: Optional[str],
    existing_normalized_title: str,
) -> MatchResult:
    factors: dict[str, str] = {}

    # --- hard gate: asset/service keyword -------------------------------
    if candidate_asset_keyword is None:
        factors["asset"] = "no recognizable asset/service keyword in candidate text"
        asset_ok = False
    elif candidate_asset_keyword == existing_asset_keyword:
        factors["asset"] = f"exact match ('{candidate_asset_keyword}')"
        asset_ok = True
    else:
        factors["asset"] = f"different ('{candidate_asset_keyword}' vs '{existing_asset_keyword}')"
        asset_ok = False

    if not asset_ok:
        explanation = f"NOT_MATCHED — asset: {factors['asset']}."
        return MatchResult(status="NOT_MATCHED", score=0.0, factors=factors, explanation=explanation)

    # --- organisation: confirmed conflict is a hard gate; unresolved is not
    if candidate_org_id is not None and existing_org_id is not None:
        if candidate_org_id == existing_org_id:
            factors["organisation"] = "exact match (confirmed same organisation)"
            org_bonus = 0.35
        else:
            factors["organisation"] = "CONFIRMED different organisation"
            explanation = (
                f"NOT_MATCHED — organisation: {factors['organisation']}. "
                "Two procurements confirmed to belong to different organisations are never "
                "treated as the same requirement, regardless of other similarity."
            )
            return MatchResult(status="NOT_MATCHED", score=0.0, factors=factors, explanation=explanation)
    else:
        factors["organisation"] = "unresolved on one or both sides — no bonus, not treated as a conflict"
        org_bonus = 0.0

    score = 0.30 + org_bonus  # asset base (always confirmed above) + organisation bonus if confirmed

    # --- soft factor: location -------------------------------------------
    if candidate_location and existing_location:
        if candidate_location == existing_location:
            factors["location"] = f"exact match ('{candidate_location}')"
            score += 0.15
        else:
            factors["location"] = f"different ('{candidate_location}' vs '{existing_location}') — not a hard gate"
            score -= 0.10
    else:
        factors["location"] = "not determinable on one or both sides — treated as neutral, not penalized"

    # --- soft factor: normalized title similarity -------------------------
    similarity = SequenceMatcher(None, candidate_normalized_title, existing_normalized_title).ratio()
    factors["title_similarity"] = f"{similarity:.2f} (difflib ratio of normalized titles)"
    score += 0.35 * similarity

    score = max(0.0, min(1.0, round(score, 3)))

    if score >= STRONG_MATCH_THRESHOLD:
        status = "STRONG_MATCH"
    elif score >= PROBABLE_MATCH_THRESHOLD:
        status = "PROBABLE_MATCH"
    elif score >= REVIEW_REQUIRED_THRESHOLD:
        status = "REVIEW_REQUIRED"
    else:
        status = "NOT_MATCHED"

    explanation = (
        f"{status} (score={score}). Organisation: {factors['organisation']}. "
        f"Asset: {factors['asset']}. Location: {factors['location']}. "
        f"Title similarity: {factors['title_similarity']}."
    )
    return MatchResult(status=status, score=score, factors=factors, explanation=explanation)
