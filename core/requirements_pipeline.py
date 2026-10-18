"""
Layer 2 orchestrator — wires together requirement identity, matching,
event derivation, and cycle statistics on top of Layer 1's evidence.

Call `link_contract_to_requirement(cur, contract_id)` any time a
contract's facts have changed (same trigger point as
core.pipeline.recalculate_contract). It is idempotent: re-running it on
an unchanged contract produces the same requirement linkage and
refreshed (but consistent) cycle statistics.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from core.requirement_identity import build_requirement_signature
from core.requirement_matching import match_requirement, MatchResult
from core.procurement_events import derive_events_for_contract
from core.procurement_cycles import recompute_and_store, CycleStats
from db import database as db


@dataclass
class RequirementLinkResult:
    requirement_id: Optional[int]
    created_new_requirement: bool
    match_result: Optional[MatchResult]
    event_ids: list
    cycle_stats: Optional[CycleStats]
    reason_unlinked: Optional[str] = None


def link_contract_to_requirement(cur, contract_id: int) -> RequirementLinkResult:
    contract = db.get_contract(cur, contract_id)
    if contract is None:
        return RequirementLinkResult(None, False, None, [], None, reason_unlinked="contract not found")

    org_row = None
    org_name = None
    if contract["org_id"] is not None:
        org_row = cur.execute("SELECT * FROM organisations WHERE id = ?", (contract["org_id"],)).fetchone()
        org_name = org_row["name"] if org_row else None

    signature = build_requirement_signature(contract_title=contract["title"], organisation_name=org_name)

    # Events are derived from Layer 1 evidence regardless of whether this
    # contract's requirement can be identified — an event not yet linked
    # to a requirement is still valid data, not something to discard.
    event_ids = derive_events_for_contract(cur, contract_id)

    if signature.asset_keyword is None:
        # No recognizable requirement category — this contract's EVENTS
        # are still recorded (above), but it doesn't participate in
        # requirement-level cycle tracking yet. Not an error: many
        # procurement documents won't match a known taxonomy bucket, and
        # guessing one would be exactly the kind of unexplained inference
        # this system avoids.
        return RequirementLinkResult(
            None, False, None, event_ids, None,
            reason_unlinked="no recognizable asset/service keyword found in contract title/organisation",
        )

    candidates = db.list_requirements_for_org_asset(cur, contract["org_id"], signature.asset_keyword)

    best_requirement_id = None
    best_match: Optional[MatchResult] = None
    for cand in candidates:
        result = match_requirement(
            candidate_org_id=contract["org_id"],
            candidate_asset_keyword=signature.asset_keyword,
            candidate_location=signature.location,
            candidate_normalized_title=signature.normalized_title,
            existing_org_id=cand["org_id"],
            existing_asset_keyword=cand["asset_keyword"],
            existing_location=cand["location"],
            existing_normalized_title=cand["normalized_title"],
        )
        if best_match is None or result.score > best_match.score:
            best_match = result
            best_requirement_id = cand["id"]

    created_new = False
    if best_requirement_id is not None and best_match is not None and best_match.status != "NOT_MATCHED":
        requirement_id = best_requirement_id
        db.touch_requirement(cur, requirement_id)
    else:
        requirement_id = db.create_requirement(
            cur, org_id=contract["org_id"], asset_keyword=signature.asset_keyword,
            location=signature.location, normalized_title=signature.normalized_title,
        )
        created_new = True
        best_match = MatchResult(
            status="STRONG_MATCH", score=1.0,
            factors={"reason": "first observation of this organisation+asset combination"},
            explanation="New requirement created — no prior requirement existed for this "
                        "organisation and asset/service combination.",
        )

    db.add_requirement_alias(cur, requirement_id=requirement_id, alias_text=contract["title"],
                              source_contract_id=contract_id)

    for eid in event_ids:
        db.link_event_to_requirement(
            cur, requirement_id=requirement_id, event_id=eid,
            match_status=best_match.status, match_score=best_match.score,
            match_evidence_json=json.dumps(best_match.factors),
        )

    cycle_stats = recompute_and_store(cur, requirement_id)

    return RequirementLinkResult(
        requirement_id=requirement_id, created_new_requirement=created_new,
        match_result=best_match, event_ids=event_ids, cycle_stats=cycle_stats,
    )
