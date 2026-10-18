"""
Cross-document reconciliation (spec §5.1).

Runs whenever a new document is linked to a contract. Never silently
overwrites: contradictory facts are retained side by side and flagged
as a conflict for the review queue. "Later document wins" only applies
within the same fact_type AND only when the new document's rank is >=
the existing fact's document rank — recency alone is not authority.
"""
from __future__ import annotations

from db import database as db

# Hierarchy rank, higher = more authoritative (spec §4.3). Ties are broken
# by NOT superseding (safer default — surface as corroboration/conflict).
_DOC_RANK = {
    "signed_agreement": 7,
    "work_order": 7,
    "aoc": 6,
    "loa": 6,
    "nit": 5,
    "bid_document": 5,
    "sla_stc": 4,
    "corrigendum": 3,
    "department_page": 2,
    "platform_amendment": 6,   # explicit on-platform duration amendment, ranks with AOC
    "site_handover_letter": 7,
    "termination_notice": 7,
    "other": 1,
}

# Fact types where a new, higher-or-equal-ranked document legitimately
# supersedes an older one (vs. types where multiple facts are expected
# to coexist, e.g. extension_option_months can have several rows).
_SUPERSEDABLE_TYPES = {
    "award_date", "sanction_date", "loa_date", "work_order_date",
    "agreement_signed_date", "site_handover_date", "commencement_date_explicit",
    "base_duration_months", "base_duration_explicit_end_date",
    "contract_value", "vendor_name", "organisation_name",
}

_NAME_TYPES = {"vendor_name", "organisation_name"}
_CONFIDENCE_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNKNOWN": 0}

# Numeric fact types where small differences are tolerated as
# "compatible" (e.g. "2 years" vs "730 days" within a few days).
_NUMERIC_TOLERANCE_DAYS = 3


def _doc_rank(doc_type: str) -> int:
    return _DOC_RANK.get(doc_type, 1)


def _values_compatible(fact_type: str, value_a: str, value_b: str) -> bool:
    if value_a == value_b:
        return True
    if fact_type == "base_duration_months":
        try:
            months_a, months_b = float(value_a), float(value_b)
            return abs(months_a - months_b) * 30.4375 <= _NUMERIC_TOLERANCE_DAYS
        except ValueError:
            return False
    return False


def reconcile_new_document(cur, contract_id: int, new_document_id: int) -> dict:
    """
    Compares every fact just extracted from `new_document_id` against the
    contract's existing non-superseded facts. Returns a summary dict for
    logging/testing: {'conflicts': [...], 'superseded': [...], 'corroborated': [...]}.
    """
    new_facts = db.get_facts_for_document(cur, new_document_id)
    new_doc = db.get_document(cur, new_document_id)
    new_rank = _doc_rank(new_doc["doc_type"])

    summary = {"conflicts": [], "superseded": [], "corroborated": []}

    for new_fact in new_facts:
        # Cross-document reconciliation only: several values of one type inside
        # a single document (a fee, a deposit and the work value on one tender
        # page) are not one source contradicting another. Duration disagreements
        # within a document are still caught by the expiry engine.
        existing = [
            f for f in db.get_facts_for_contract(cur, contract_id, fact_type=new_fact["fact_type"])
            if f["id"] != new_fact["id"] and f["document_id"] != new_fact["document_id"]
        ]
        for old_fact in existing:
            if _values_compatible(new_fact["fact_type"], old_fact["fact_value"], new_fact["fact_value"]):
                summary["corroborated"].append((old_fact["id"], new_fact["id"]))
                continue

            # Names read with different strength of evidence (a letterhead guess
            # vs the portal's own "Organisation Chain") are not a contradiction:
            # the stronger reading replaces the weaker one.
            if new_fact["fact_type"] in _NAME_TYPES:
                old_strength = _CONFIDENCE_RANK.get(old_fact["fact_confidence"], 0)
                new_strength = _CONFIDENCE_RANK.get(new_fact["fact_confidence"], 0)
                if new_strength > old_strength:
                    db.supersede_fact(cur, old_fact["id"], new_fact["id"])
                    summary["superseded"].append((old_fact["id"], new_fact["id"]))
                    continue
                if old_strength > new_strength:
                    db.supersede_fact(cur, new_fact["id"], old_fact["id"])
                    summary["superseded"].append((new_fact["id"], old_fact["id"]))
                    continue

            # contradictory
            if new_fact["fact_type"] not in _SUPERSEDABLE_TYPES:
                # types that legitimately coexist (e.g. multiple extension
                # options) are never flagged as conflicts merely for differing
                continue

            old_doc = db.get_document(cur, old_fact["document_id"])
            old_rank = _doc_rank(old_doc["doc_type"]) if old_doc else 0

            if new_rank > old_rank:
                db.supersede_fact(cur, old_fact["id"], new_fact["id"])
                summary["superseded"].append((old_fact["id"], new_fact["id"]))
            elif new_rank == old_rank:
                # equal rank, contradictory, do NOT auto-resolve
                db.insert_conflict(cur, contract_id=contract_id, fact_type=new_fact["fact_type"],
                                    fact_id_a=old_fact["id"], fact_id_b=new_fact["id"])
                summary["conflicts"].append((old_fact["id"], new_fact["id"]))
            else:
                # new document is LOWER ranked than existing — recency alone
                # does not override authority; retain both, flag as conflict
                # for visibility but do not supersede.
                db.insert_conflict(cur, contract_id=contract_id, fact_type=new_fact["fact_type"],
                                    fact_id_a=old_fact["id"], fact_id_b=new_fact["id"])
                summary["conflicts"].append((old_fact["id"], new_fact["id"]))

    if summary["conflicts"]:
        db.insert_timeline_event(
            cur, contract_id=contract_id, event_type="conflict_flagged",
            document_id=new_document_id,
            narrative=f"{len(summary['conflicts'])} conflicting fact(s) detected against "
                      f"existing evidence upon ingesting document {new_document_id}.",
        )

    return summary
