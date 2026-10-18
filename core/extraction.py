"""
Top-level extraction: turns raw document text into a list of atomic
extracted-fact dicts (fact_type, value, evidence_quote, context_window,
offsets, extraction_rule_id, fact_confidence), ready for
db.database.insert_fact().

This module does NOT decide start-date resolution or expiry — it only
extracts atomic, evidence-backed claims. Resolution lives in
core/start_date.py and core/expiry_engine.py, per the separation the
spec's schema (§3.2) is built around.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from core.dateutils import parse_date_any
from core.duration import extract_duration_candidates, CONTEXT_RADIUS

_WINDOW = CONTEXT_RADIUS


@dataclass
class Fact:
    fact_type: str
    value: str
    evidence_quote: str
    context_window: str
    char_offset_start: int
    char_offset_end: int
    extraction_rule_id: str
    fact_confidence: str  # HIGH / MEDIUM / LOW


def _win(text: str, start: int, end: int) -> str:
    lo = max(0, start - _WINDOW)
    hi = min(len(text), end + _WINDOW)
    return text[lo:hi]


# ---------------------------------------------------------------------
# Date-bearing fact patterns.
# Each: (fact_type, keyword regex whose match captures a nearby date, rule_id, confidence)
# The keyword pattern must contain a date somewhere within ~40 chars after it;
# we search the local window with parse_date_any.
# ---------------------------------------------------------------------
_DATE_FIELD_PATTERNS: list[tuple[str, re.Pattern, str, str]] = [
    ("award_date", re.compile(r"(?:award(?:ed)?|AOC|acceptance\s*of\s*(?:tender|offer))[^.\d]{0,25}", re.I),
     "P_AWARD_DATE_v2", "HIGH"),
    ("sanction_date", re.compile(r"sanction(?:ed)?[^.\d]{0,25}", re.I), "P_SANCTION_DATE_v1", "HIGH"),
    ("loa_date", re.compile(r"letter\s*of\s*award[^.\d]{0,25}|LOA[^.\d]{0,25}", re.I), "P_LOA_DATE_v1", "HIGH"),
    ("work_order_date", re.compile(r"work\s*order[^.\d]{0,25}dated[^.\d]{0,15}", re.I),
     "P_WORK_ORDER_DATE_v1", "HIGH"),
    ("agreement_signed_date", re.compile(r"agreement\s*(?:signed|executed)[^.\d]{0,25}", re.I),
     "P_AGREEMENT_SIGNED_v1", "HIGH"),
    ("site_handover_date", re.compile(r"(?:hand(?:ing|ed)?\s*over|handover)\s*of\s*(?:the\s*)?site[^.\d]{0,25}", re.I),
     "P_SITE_HANDOVER_v1", "HIGH"),
    ("commencement_date_explicit",
     re.compile(r"(?:commence(?:ment|s|d)?|start\s*of\s*(?:work|contract|services))[^.\d]{0,25}", re.I),
     "P_COMMENCEMENT_EXPLICIT_v2", "HIGH"),
    ("termination_date", re.compile(r"termination[^.\d]{0,25}|contract\s*(?:completed|closed)[^.\d]{0,25}", re.I),
     "P_TERMINATION_v1", "HIGH"),
]

# Contract value: ₹ / Rs. / INR figures
_VALUE_RE = re.compile(
    r"(?:Rs\.?|₹|INR)\s*([\d,]+(?:\.\d+)?)\s*(?:/-|lakh|lakhs|crore|crores)?", re.I
)

# Vendor / organisation cues (very lightweight — real NER is out of MVP
# scope; these patterns catch the common "M/s <Name>" and "issued by <Org>"
# constructions actually seen in NIT/AOC boilerplate).
_VENDOR_RE = re.compile(r"M/s\.?\s+([A-Z][A-Za-z0-9&.,\-\s]{2,60}?)(?:,|\.|\n|$)")
_ORG_RE = re.compile(
    r"(?:tendering\s*authority|issued\s*by|department\s*of|office\s*of)[:\s]+"
    r"([A-Z][A-Za-z0-9&.,\-\s]{3,80}?)(?:\n|\.|,\s*(?:invites|hereby))",
    re.I,
)

# Extension actually exercised (not merely an option) — requires an
# explicit verb of exercise, not just the word "extend".
_EXTENSION_EXERCISED_RE = re.compile(
    r"(?:extension\s*(?:has\s*been|is\s*hereby)?\s*(?:granted|sanctioned|exercised|approved)|"
    r"contract\s*(?:period\s*)?(?:has\s*been\s*)?extended\s*(?:vide|as\s*per|per)\s*(?:this\s*)?"
    r"(?:work\s*order|letter|order))",
    re.I,
)

_ABSOLUTE_CEILING_RE = re.compile(
    r"shall\s*not\s*(?:surpass|exceed)\s*([^.]{0,40})", re.I
)

# The specific new end-date phrase, searched anywhere after the exercise
# trigger — NOT just "the first date within N chars", since the trigger
# clause itself often contains an unrelated date (e.g. the work order's
# own issue date: "...extended vide this work order dated 15-Feb-2028;
# new end date shall be 31-Mar-2028." — the 31-Mar-2028 is the fact we
# want, not the work order's own dated-15-Feb-2028).
_NEW_END_DATE_PHRASE_RE = re.compile(
    r"(?:new\s*end\s*date|revised\s*(?:expiry|end\s*date)|new\s*expiry(?:\s*date)?)"
    r"\s*(?:shall\s*be|is|:)?\s*", re.I
)


def _find_date_facts(text: str) -> list[Fact]:
    facts: list[Fact] = []
    for fact_type, keyword_re, rule_id, confidence in _DATE_FIELD_PATTERNS:
        for km in keyword_re.finditer(text):
            local_end = min(len(text), km.end() + 40)
            local_text = text[km.end():local_end]
            # don't let the date search cross a sentence boundary the
            # keyword match itself didn't already cross (avoids grabbing
            # an unrelated date from the *next* sentence, e.g. "Work
            # order. The work shall commence on 01-Feb-2026." incorrectly
            # yielding a work_order_date from the commencement clause).
            sentence_break = local_text.find(". ")
            if sentence_break != -1:
                local_text = local_text[:sentence_break]
            d = parse_date_any(local_text)
            if d is None:
                continue
            evidence_start = km.start()
            evidence_end = km.end() + len(local_text)
            quote = text[evidence_start:evidence_end].strip()
            facts.append(Fact(
                fact_type=fact_type,
                value=d.isoformat(),
                evidence_quote=quote,
                context_window=_win(text, evidence_start, evidence_end),
                char_offset_start=evidence_start,
                char_offset_end=evidence_end,
                extraction_rule_id=rule_id,
                fact_confidence=confidence,
            ))
    return facts


_DURATION_KEYWORD_RE = re.compile(
    r"\b(period|duration|tenure|term|AMC|CMC|contract|agreement|time\s*allowed|valid\s*for)\b", re.I
)


def _duration_fact_confidence(cand) -> str:
    if cand.fact_type == "base_duration_explicit_end_date":
        return "HIGH"
    if not cand.extraction_rule_id.startswith("P_SIMPLE"):
        return "HIGH"  # composite / plus-shorthand clauses already read as genuine duration clauses
    # bare "N years/months" needs a nearby duration keyword to earn HIGH;
    # otherwise it's a MEDIUM-confidence, context-only inference.
    return "HIGH" if _DURATION_KEYWORD_RE.search(cand.context_window) else "MEDIUM"


def _find_duration_facts(text: str) -> list[Fact]:
    facts: list[Fact] = []
    for cand in extract_duration_candidates(text):
        if not cand.accepted:
            continue  # rejected candidates are not persisted as facts (but see extract_all(debug=True))
        facts.append(Fact(
            fact_type=cand.fact_type,
            value=cand.value,
            evidence_quote=cand.evidence_quote,
            context_window=cand.context_window,
            char_offset_start=cand.char_offset_start,
            char_offset_end=cand.char_offset_end,
            extraction_rule_id=cand.extraction_rule_id,
            fact_confidence=_duration_fact_confidence(cand),
        ))
    return facts


def _find_value_facts(text: str) -> list[Fact]:
    facts = []
    for m in _VALUE_RE.finditer(text):
        raw = m.group(1).replace(",", "")
        try:
            amount = float(raw)
        except ValueError:
            continue
        unit = m.group(0).lower()
        if "crore" in unit:
            amount *= 1e7
        elif "lakh" in unit:
            amount *= 1e5
        facts.append(Fact(
            fact_type="contract_value",
            value=str(amount),
            evidence_quote=m.group(0).strip(),
            context_window=_win(text, m.start(), m.end()),
            char_offset_start=m.start(),
            char_offset_end=m.end(),
            extraction_rule_id="P_VALUE_INR_v1",
            fact_confidence="MEDIUM",
        ))
    return facts


def _find_entity_facts(text: str) -> list[Fact]:
    facts = []
    for m in _VENDOR_RE.finditer(text):
        facts.append(Fact(
            fact_type="vendor_name",
            value=m.group(1).strip(),
            evidence_quote=m.group(0).strip(),
            context_window=_win(text, m.start(), m.end()),
            char_offset_start=m.start(),
            char_offset_end=m.end(),
            extraction_rule_id="P_VENDOR_MS_v1",
            fact_confidence="MEDIUM",
        ))
    for m in _ORG_RE.finditer(text):
        facts.append(Fact(
            fact_type="organisation_name",
            value=m.group(1).strip(),
            evidence_quote=m.group(0).strip(),
            context_window=_win(text, m.start(), m.end()),
            char_offset_start=m.start(),
            char_offset_end=m.end(),
            extraction_rule_id="P_ORG_ISSUEDBY_v1",
            fact_confidence="MEDIUM",
        ))
    return facts


def _find_extension_exercise_facts(text: str) -> list[Fact]:
    facts = []
    for m in _EXTENSION_EXERCISED_RE.finditer(text):
        local_end = min(len(text), m.end() + 60)
        window_text = text[m.start():local_end]

        # 1. Prefer an explicit "new end date" / "revised expiry" phrase,
        #    searched over the whole document (not just the trigger's own
        #    local window), since that phrase is the actual fact — the
        #    trigger clause itself may contain an unrelated date (the
        #    work order's own issue date).
        d = None
        phrase_span_end = None
        search_from = m.start()
        phrase_m = _NEW_END_DATE_PHRASE_RE.search(text, search_from)
        if phrase_m:
            phrase_local_end = min(len(text), phrase_m.end() + 40)
            phrase_local_text = text[phrase_m.end():phrase_local_end]
            d = parse_date_any(phrase_local_text)
            if d is not None:
                phrase_span_end = phrase_m.end() + len(phrase_local_text)

        if d is not None:
            evidence_end = max(local_end, phrase_span_end)
            evidence_quote = text[m.start():evidence_end].strip()
            offset_end = evidence_end
        else:
            # 2. Fallback: no explicit new-end-date phrase found at all —
            #    do NOT guess by grabbing the nearest unrelated date. Only
            #    record that an extension was exercised (fact_type
            #    'extension_exercised', value 'true'); the new end date
            #    stays unrepresented rather than being inferred wrongly.
            evidence_quote = window_text.strip()
            offset_end = local_end

        facts.append(Fact(
            fact_type="extension_exercised",
            value="true" if d is None else d.isoformat(),
            evidence_quote=evidence_quote,
            context_window=_win(text, m.start(), offset_end),
            char_offset_start=m.start(),
            char_offset_end=offset_end,
            extraction_rule_id="P_EXTENSION_EXERCISED_v1",
            fact_confidence="HIGH",
        ))
        if d is not None:
            facts.append(Fact(
                fact_type="extension_exercised_new_end_date",
                value=d.isoformat(),
                evidence_quote=evidence_quote,
                context_window=_win(text, m.start(), offset_end),
                char_offset_start=m.start(),
                char_offset_end=offset_end,
                extraction_rule_id="P_EXTENSION_NEW_END_v1",
                fact_confidence="HIGH",
            ))
    return facts


def _find_absolute_ceiling_facts(text: str) -> list[Fact]:
    facts = []
    for m in _ABSOLUTE_CEILING_RE.finditer(text):
        d = parse_date_any(m.group(1))
        if not d:
            continue
        facts.append(Fact(
            fact_type="absolute_expiry_ceiling",
            value=d.isoformat(),
            evidence_quote=m.group(0).strip(),
            context_window=_win(text, m.start(), m.end()),
            char_offset_start=m.start(),
            char_offset_end=m.end(),
            extraction_rule_id="P_ABSOLUTE_CEILING_v1",
            fact_confidence="HIGH",
        ))
    return facts


def extract_all_facts(text: str) -> list[Fact]:
    """Run every extractor and return the combined, evidence-backed fact list."""
    facts: list[Fact] = []
    facts += _find_date_facts(text)
    facts += _find_duration_facts(text)
    facts += _find_value_facts(text)
    facts += _find_entity_facts(text)
    facts += _find_extension_exercise_facts(text)
    facts += _find_absolute_ceiling_facts(text)
    return facts
