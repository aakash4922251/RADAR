"""
Duration extraction.

Distinguishes contract duration from bid-validity, delivery period,
warranty/DLP, PBG/EMD validity, and experience/turnover criteria (see
reject_patterns.py), and represents composite clauses ("1 year,
extendable yearly up to 2 more years") as SEPARATE facts rather than a
single collapsed number, per spec §4.6 / example #4.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from core.reject_patterns import classify_context, RejectClass
from core.dateutils import parse_date_any

CONTEXT_RADIUS = 140  # chars each side, matches spec's "120 chars around the quote"

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}

_UNIT_TO_MONTHS = {"year": 12, "years": 12, "yr": 12, "yrs": 12,
                    "month": 1, "months": 1, "mo": 1}


@dataclass
class DurationCandidate:
    fact_type: str                # 'base_duration_months' | 'extension_option_months' | ...
    value: str                    # stringified value (months as int, or count, or ISO date)
    evidence_quote: str
    context_window: str
    char_offset_start: int
    char_offset_end: int
    extraction_rule_id: str
    accepted: bool
    reject_class: Optional[str] = None


def _window(text: str, start: int, end: int) -> str:
    lo = max(0, start - CONTEXT_RADIUS)
    hi = min(len(text), end + CONTEXT_RADIUS)
    return text[lo:hi]


def _number_from_match(num_str: str) -> Optional[int]:
    num_str = num_str.strip().lower()
    if num_str.isdigit():
        return int(num_str)
    return _NUMBER_WORDS.get(num_str)


def _days_to_months(days: int) -> float:
    return round(days / 30.4375, 2)  # average month length; used only for display/tolerance


# Build the number-word alternation once from _NUMBER_WORDS so every
# pattern below recognises the same word set — avoids the two lists
# silently drifting apart (e.g. one pattern knowing "twelve", another not).
_NUM_WORD_ALT = "|".join(sorted(_NUMBER_WORDS.keys(), key=len, reverse=True))
_NUM_ALT = rf"\d+|{_NUM_WORD_ALT}"
# A number optionally followed by a parenthetical numeral gloss, e.g.
# "Twelve (12)" or "01 (1)" — the gloss is consumed, not re-emitted.
_NUM_WITH_GLOSS = rf"(?:{_NUM_ALT})\s*(?:\(\s*\d+\s*\))?"
# A duration unit optionally followed by a parenthetical day-count gloss,
# e.g. "12 Months (365 days)" — consumed so it isn't double-counted.
_UNIT_WITH_DAY_GLOSS = r"(?:years?|months?|yrs?|mo)\b(?:\s*\(\s*\d+\s*days?\s*\))?"

# ---------------------------------------------------------------------
# Pattern 1: composite "N year(s)/month(s) ... extendable/renewable ...
#            up to M more year(s)/month(s) [/ N times]"
#            Covers examples #4, #5, #6, #7.
# ---------------------------------------------------------------------
_COMPOSITE_RE = re.compile(
    r"""
    \b(?:period\s*of\s*)?
    (?P<base_num>""" + _NUM_ALT + r""")\s*(?:\(\s*\d+\s*\))?\s*
    (?P<base_unit>years?|months?|yrs?|mo)\b
    (?:\s*\(\s*\d+\s*days?\s*\))?              # e.g. "(365 days)" gloss
    [^.]{0,80}?
    (?:extend(?:able|ed)?|renew(?:able)?)[^.]{0,40}?
    (?:up\s*to\s*(?:a\s*period\s*of\s*)?)?
    (?P<ext_num>""" + _NUM_ALT + r""")\s*(?:\(\s*\d+\s*\))?\s*
    (?:more\s*)?(?P<ext_unit>years?|months?|yrs?|mo)\b
    """,
    re.I | re.VERBOSE,
)

# ---------------------------------------------------------------------
# Pattern 2: "1+1 years" / "2+1+1 years" style shorthand
# ---------------------------------------------------------------------
_PLUS_SHORTHAND_RE = re.compile(
    r"\b(?P<parts>\d+(?:\s*\+\s*\d+){1,3})\s*(?P<unit>years?|months?)\b", re.I
)

# ---------------------------------------------------------------------
# Pattern 3: simple standalone duration ("12 months", "365 days", "1 year"),
#            including an optional trailing day-count gloss so
#            "12 Months (365 days)" is captured as ONE fact, not two.
# ---------------------------------------------------------------------
_SIMPLE_RE = re.compile(
    r"\b(?P<num>" + _NUM_ALT + r")\s*(?:\(\s*\d+\s*\))?\s*"
    r"(?P<unit>years?|months?|days?|yrs?|mo)\b"
    r"(?!\s+of\s+[A-Za-z])"   # exclude "25 day of October" — that's a date, not a duration
    r"(?:\s*\(\s*\d+\s*days?\s*\))?",
    re.I,
)

# ---------------------------------------------------------------------
# Pattern 4: "renewable annually" / "renewed every year" (open-ended)
# ---------------------------------------------------------------------
_RENEWABLE_ANNUAL_RE = re.compile(r"\brenew(?:able|ed)\s*(?:annually|every\s*year|yearly)\b", re.I)

# ---------------------------------------------------------------------
# Pattern 5: explicit calendar range "from <date> to <date>"
# ---------------------------------------------------------------------
_RANGE_RE = re.compile(
    r"\bfrom\s+(?P<start>\d{1,2}(?:st|nd|rd|th)?[\s\-/]+[A-Za-z]{3,9}['\u2019]?[\s\-/,]*\d{4}|\d{4}-\d{1,2}-\d{1,2})"
    r"\s+(?:to|till|until)\s+"
    r"(?P<end>\d{1,2}(?:st|nd|rd|th)?[\s\-/]+[A-Za-z]{3,9}['\u2019]?[\s\-/,]*\d{4}|\d{4}-\d{1,2}-\d{1,2})",
    re.I,
)


def _unit_months(unit: str) -> int:
    unit = unit.lower()
    if unit.startswith("day"):
        return 0  # handled specially by caller (days, not months)
    return _UNIT_TO_MONTHS.get(unit, 1)


def _make_candidate(text, fact_type, value, m, rule_id) -> DurationCandidate:
    quote = m.group(0).strip()
    ctx = _window(text, m.start(), m.end())
    cls = classify_context(ctx)
    return DurationCandidate(
        fact_type=fact_type,
        value=str(value),
        evidence_quote=quote,
        context_window=ctx,
        char_offset_start=m.start(),
        char_offset_end=m.end(),
        extraction_rule_id=rule_id,
        accepted=(cls.reject_class == RejectClass.NONE),
        reject_class=None if cls.reject_class == RejectClass.NONE else cls.reject_class.value,
    )


def extract_duration_candidates(text: str) -> list[DurationCandidate]:
    """
    Returns ALL candidates found (accepted and rejected) so callers /
    tests can inspect why something was rejected. Order of pattern
    application matters: composite and range patterns are tried first
    and their matched spans are excluded from the simple-pattern pass,
    to avoid double counting (e.g. "12 months" inside a composite
    clause should not ALSO fire as a bare simple duration).
    """
    candidates: list[DurationCandidate] = []
    consumed_spans: list[tuple[int, int]] = []

    def _overlaps(start: int, end: int) -> bool:
        return any(start < e and end > s for s, e in consumed_spans)

    # 1. Explicit calendar range — highest priority, spec §4.2 "alternative-form"
    for m in _RANGE_RE.finditer(text):
        start_d = parse_date_any(m.group("start"))
        end_d = parse_date_any(m.group("end"))
        if start_d and end_d and end_d > start_d:
            candidates.append(_make_candidate(
                text, "base_duration_explicit_end_date",
                f"{start_d.isoformat()}|{end_d.isoformat()}", m, "P_EXPLICIT_RANGE_v1"
            ))
            consumed_spans.append((m.start(), m.end()))

    # 2. Composite base + extension clause (examples #4-#7)
    for m in _COMPOSITE_RE.finditer(text):
        base_num = _number_from_match(m.group("base_num"))
        ext_num = _number_from_match(m.group("ext_num"))
        base_unit_months = _unit_months(m.group("base_unit"))
        ext_unit_months = _unit_months(m.group("ext_unit"))
        if base_num is None or ext_num is None:
            continue
        base_months = base_num * base_unit_months
        # composite pattern implies N separate annual/monthly extension
        # exercises unless the ext number itself is already a total, so we
        # store extension_option_count and extension_option_months
        # separately (per spec: "not a single 2-year block").
        ext_count = ext_num if ext_unit_months == 12 else 1
        ext_months_each = 12 if ext_unit_months == 12 else ext_num * ext_unit_months
        candidates.append(_make_candidate(text, "base_duration_months", base_months, m, "P_COMPOSITE_BASE_v3"))
        candidates.append(_make_candidate(text, "extension_option_months", ext_months_each, m, "P_COMPOSITE_EXT_MONTHS_v3"))
        candidates.append(_make_candidate(text, "extension_option_count", ext_count, m, "P_COMPOSITE_EXT_COUNT_v3"))
        consumed_spans.append((m.start(), m.end()))

    # 3. "1+1 years" shorthand
    for m in _PLUS_SHORTHAND_RE.finditer(text):
        if _overlaps(m.start(), m.end()):
            continue
        parts = [int(p.strip()) for p in m.group("parts").split("+")]
        unit_months = _unit_months(m.group("unit"))
        base_months = parts[0] * unit_months
        candidates.append(_make_candidate(text, "base_duration_months", base_months, m, "P_PLUS_SHORTHAND_BASE_v1"))
        if len(parts) > 1:
            for p in parts[1:]:
                candidates.append(_make_candidate(
                    text, "extension_option_months", p * unit_months, m, "P_PLUS_SHORTHAND_EXT_v1"
                ))
            candidates.append(_make_candidate(
                text, "extension_option_count", len(parts) - 1, m, "P_PLUS_SHORTHAND_EXT_COUNT_v1"
            ))
        consumed_spans.append((m.start(), m.end()))

    # 4. Renewable annually (open-ended extension, no fixed ceiling)
    for m in _RENEWABLE_ANNUAL_RE.finditer(text):
        candidates.append(_make_candidate(text, "extension_option_months", 12, m, "P_RENEWABLE_ANNUAL_v1"))
        candidates.append(_make_candidate(text, "extension_option_count", -1, m, "P_RENEWABLE_ANNUAL_OPEN_v1"))
        consumed_spans.append((m.start(), m.end()))

    # 5. Simple standalone duration, excluding anything already consumed
    for m in _SIMPLE_RE.finditer(text):
        if _overlaps(m.start(), m.end()):
            continue
        num = _number_from_match(m.group("num"))
        unit = m.group("unit").lower()
        if num is None:
            continue
        if unit.startswith("day"):
            months_equiv = _days_to_months(num)
            candidates.append(_make_candidate(text, "base_duration_months", months_equiv, m, "P_SIMPLE_DAYS_v2"))
        else:
            months = num * _unit_months(unit)
            candidates.append(_make_candidate(text, "base_duration_months", months, m, "P_SIMPLE_UNIT_v2"))
        consumed_spans.append((m.start(), m.end()))

    return candidates


def accepted_only(candidates: list[DurationCandidate]) -> list[DurationCandidate]:
    return [c for c in candidates if c.accepted]
