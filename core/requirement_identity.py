"""
Requirement identity (spec §4/§7).

Deterministic, transparent, no LLM. A "requirement" is the underlying
recurring need (e.g. "CCTV AMC — XYZ Hospital") that must survive
changes in tender title, incumbent vendor, tender number, and document
wording across multiple contracts over time — without silently merging
genuinely unrelated procurements.

Identity is built from two hard, auditable signals:
  1. asset_keyword — which canonical taxonomy bucket the requirement's
     text falls into (CCTV, housekeeping, security, etc.), detected via
     a fixed synonym list, not free-text NLP.
  2. location — a normalized token from a small gazetteer of common
     Indian administrative/city names, when present in the text at all.

Everything here is a lookup table + string normalization. No statistical
or learned model is used to decide identity — that keeps every match
auditable: "asset_keyword matched because the text contained the
substring 'cctv'" is always the actual, inspectable reason.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------
# Asset / service taxonomy — canonical key -> synonym substrings.
# Deliberately small and explicit; extend this list as real documents
# reveal new recurring requirement categories, per spec §4.
# ---------------------------------------------------------------------
ASSET_TAXONOMY: dict[str, list[str]] = {
    "cctv": ["cctv", "video surveillance", "camera surveillance", "surveillance system", "surveillance camera"],
    "housekeeping": ["housekeeping", "house keeping", "cleaning services", "sanitation services", "janitorial"],
    "security": ["security services", "security guard", "watch and ward", "security personnel"],
    "fire_safety": ["fire alarm", "fire fighting", "fire-fighting", "fire detection", "fire safety", "fire suppression"],
    "vehicle_hiring": ["vehicle hiring", "vehicle hire", "car hiring", "transport services", "taxi services"],
    "facility_management": ["facility management", "facilities management", "comprehensive facility"],
    "generator_amc": ["generator", "dg set", "diesel generator", "genset"],
    "it_support": ["it support", "information technology support", "computer maintenance", "hardware maintenance"],
    "electrical_maintenance": ["electrical maintenance", "electrical works", "electrical amc"],
    "elevator_amc": ["elevator", "lift maintenance", "lift amc"],
    "pest_control": ["pest control", "rodent control", "fumigation"],
    "canteen_catering": ["canteen services", "catering services", "food services"],
    "horticulture": ["horticulture", "gardening services", "landscaping"],
    "water_treatment": ["water treatment", "stp maintenance", "effluent treatment", "sewage treatment"],
    "air_conditioning": ["air conditioning", "hvac", "ac maintenance", "air-conditioning"],
}

# A small gazetteer of common Indian city / administrative-area tokens.
# Location is a "when present" bonus signal, never a hard gate — most
# procurement text does not spell out a city at all, and that must not
# be treated as a mismatch (see requirement_matching.py).
LOCATION_GAZETTEER: list[str] = [
    "mumbai", "delhi", "new delhi", "bengaluru", "bangalore", "chennai", "kolkata",
    "hyderabad", "pune", "ahmedabad", "jaipur", "lucknow", "kanpur", "nagpur",
    "indore", "bhopal", "patna", "chandigarh", "kochi", "coimbatore", "visakhapatnam",
    "surat", "vadodara", "nashik", "rajkot", "varanasi", "amritsar", "prayagraj",
    "guwahati", "ranchi", "raipur", "bhubaneswar", "dehradun", "shimla", "jammu",
    "thiruvananthapuram", "mysuru", "mysore", "gurugram", "gurgaon", "noida",
    "faridabad", "ghaziabad", "auckland", "colombo", "algiers",
]

_PUNCT_RE = re.compile(r"[^\w\s]")
_WS_RE = re.compile(r"\s+")


@dataclass
class RequirementSignature:
    asset_keyword: Optional[str]
    asset_evidence: Optional[str]        # the synonym substring that matched
    location: Optional[str]
    normalized_title: str


def normalize_text(text: str) -> str:
    text = text.lower()
    text = _PUNCT_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def detect_asset_keyword(text: str) -> tuple[Optional[str], Optional[str]]:
    """Returns (canonical_key, matched_synonym) — the FIRST taxonomy entry
    whose synonym appears in the normalized text, checked in a fixed,
    documented order (dict insertion order), so the result is
    deterministic and reproducible."""
    norm = normalize_text(text)
    for canonical_key, synonyms in ASSET_TAXONOMY.items():
        for syn in synonyms:
            if syn in norm:
                return canonical_key, syn
    return None, None


def detect_location(text: str) -> Optional[str]:
    norm = normalize_text(text)
    for city in LOCATION_GAZETTEER:
        # word-boundary match to avoid e.g. "pune" inside an unrelated longer token
        if re.search(rf"\b{re.escape(city)}\b", norm):
            return city
    return None


def build_requirement_signature(*, contract_title: str, organisation_name: Optional[str] = None) -> RequirementSignature:
    """
    Combines the contract title (and organisation name, if known — city
    names sometimes appear there, e.g. "CAG Kolkata") into one text blob
    for asset/location detection, and produces a normalized_title for
    display and title-similarity scoring.
    """
    combined = " ".join(t for t in [contract_title, organisation_name or ""] if t)
    asset_keyword, asset_evidence = detect_asset_keyword(combined)
    location = detect_location(combined)
    return RequirementSignature(
        asset_keyword=asset_keyword,
        asset_evidence=asset_evidence,
        location=location,
        normalized_title=normalize_text(contract_title),
    )
