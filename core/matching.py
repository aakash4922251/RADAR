"""
Contract-identity similarity check.

This does NOT auto-merge contracts — per the evidence-first philosophy,
merging two contract records is a judgement call that should be made by
a human, not inferred silently from string similarity. What this module
does is surface a warning when a newly-titled contract looks like it
might already exist under a slightly different title, so a reviewer (or
the uploader) can choose to link the document to the existing contract
instead of creating a duplicate.

Uses the standard library's difflib rather than rapidfuzz: rapidfuzz is
listed in requirements.txt for future use, but a stdlib-only
implementation means this feature works with zero extra dependencies,
which matters most for exactly the kind of offline/locked-down
environment this MVP is meant to run in.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from db import database as db

DEFAULT_SIMILARITY_THRESHOLD = 0.82


@dataclass
class SimilarContract:
    contract_id: int
    title: str
    similarity: float


def _normalize(title: str) -> str:
    return " ".join(title.lower().split())


def find_similar_contracts(cur, title: str, threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
                            exclude_contract_id: int | None = None) -> list[SimilarContract]:
    """
    Returns existing contracts whose title is similar enough to `title`
    that they might be the same underlying contract. Ordered by
    similarity, highest first. Never returns an exact-title match (that
    case is handled separately as a true duplicate, not a "similar"
    candidate needing review).
    """
    target = _normalize(title)
    matches: list[SimilarContract] = []
    for row in db.list_contracts(cur):
        if exclude_contract_id is not None and row["id"] == exclude_contract_id:
            continue
        candidate = _normalize(row["title"])
        if candidate == target:
            continue  # exact match is not "similar", it's the same contract
        score = SequenceMatcher(None, target, candidate).ratio()
        if score >= threshold:
            matches.append(SimilarContract(contract_id=row["id"], title=row["title"], similarity=round(score, 3)))
    matches.sort(key=lambda m: m.similarity, reverse=True)
    return matches
