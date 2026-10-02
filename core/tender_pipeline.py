"""Link acquired tender records to stable requirements without creating contracts."""
from __future__ import annotations

import json

from core.dateutils import parse_date_any
from core.procurement_cycles import recompute_and_store
from core.radar import refresh_prediction
from core.requirement_identity import build_requirement_signature, detect_asset_keyword, detect_location
from core.requirement_matching import MatchResult, match_requirement
from db import database as db


def link_tender_to_requirement(cur, record_id: int) -> dict:
    record = cur.execute(
        "SELECT * FROM discovered_records WHERE id = ?", (record_id,)
    ).fetchone()
    if record is None:
        return {"requirement_id": None, "event_id": None, "reason": "tender record not found"}

    title = (record["title"] or record["external_id"] or "").strip()
    organization_chain = (record["organisation"] or "").strip()
    organization = organization_chain.split("||", 1)[0].strip() or None
    department = organization_chain.split("||", 1)[1].strip() if "||" in organization_chain else None
    org_id = db.get_or_create(cur, "organisations", organization) if organization else None

    signature = build_requirement_signature(
        contract_title=title,
        organisation_name=" ".join(part for part in (organization, department) if part),
    )
    category, category_evidence = detect_asset_keyword(
        " ".join(part for part in (title, record["classification"] or "") if part)
    )
    asset_keyword = category
    location = detect_location(record["location"] or "") or signature.location
    if asset_keyword is None:
        return {
            "requirement_id": None,
            "event_id": None,
            "reason": "no recognizable service/category in tender title or classification",
        }

    candidates = db.list_requirements_for_org_asset(cur, org_id, asset_keyword)
    best_requirement_id = None
    best_match = None
    for candidate in candidates:
        result = match_requirement(
            candidate_org_id=org_id,
            candidate_asset_keyword=asset_keyword,
            candidate_location=location,
            candidate_normalized_title=signature.normalized_title,
            existing_org_id=candidate["org_id"],
            existing_asset_keyword=candidate["asset_keyword"],
            existing_location=candidate["location"],
            existing_normalized_title=candidate["normalized_title"],
        )
        if best_match is None or result.score > best_match.score:
            best_requirement_id = candidate["id"]
            best_match = result

    if best_match is None or best_match.status == "NOT_MATCHED":
        requirement_id = db.create_requirement(
            cur, org_id=org_id, asset_keyword=asset_keyword, location=location,
            normalized_title=signature.normalized_title,
        )
        best_match = MatchResult(
            status="STRONG_MATCH", score=1.0,
            factors={"reason": "first observed tender for this organisation and service category"},
            explanation="New requirement created from a tender observation; no existing identity was merged.",
        )
    else:
        requirement_id = best_requirement_id
        db.touch_requirement(cur, requirement_id)

    evidence = dict(best_match.factors)
    evidence.update({
        "source": "discovered tender record",
        "tender_reference": record["external_id"],
        "department": department or "not exposed",
        "classification": record["classification"] or "not exposed",
        "location_observed": record["location"] or "not exposed",
        "category_evidence": category_evidence or "title taxonomy match",
    })
    db.add_requirement_alias(
        cur, requirement_id=requirement_id, alias_text=title,
        source_discovered_record_id=record_id,
    )

    published = parse_date_any(record["published_at"] or "")
    event_id = None
    if published:
        event_id = db.insert_procurement_event(
            cur,
            contract_id=None,
            discovered_record_id=record_id,
            event_type="tender_published",
            event_date=published.isoformat(),
            notes=json.dumps({
                "source": "discovered tender record",
                "external_id": record["external_id"],
                "title": title,
                "detail_url": record["detail_url"],
                "source_page_url": record["source_page_url"],
            }, sort_keys=True),
        )
        db.link_event_to_requirement(
            cur,
            requirement_id=requirement_id,
            event_id=event_id,
            match_status=best_match.status,
            match_score=best_match.score,
            match_evidence_json=json.dumps(evidence, sort_keys=True),
        )
        recompute_and_store(cur, requirement_id)
        refresh_prediction(cur, requirement_id)

    return {"requirement_id": requirement_id, "event_id": event_id, "match": best_match}