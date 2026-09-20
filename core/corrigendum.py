"""
Corrigendum classifier (spec §4.4).

Evidence base: 7 real corrigenda examined in the research (examples
#9-#12), 7 of 7 extended a bid deadline or clarified an administrative
point; zero touched contract duration. A corrigendum is therefore never
assumed duration-relevant by default — it must positively match a
substantive-duration pattern to be treated as one.
"""
from __future__ import annotations

import re
from enum import Enum


class CorrigendumSubtype(str, Enum):
    BID_DEADLINE_EXTENSION = "bid_deadline_extension"
    SUBSTANTIVE_DURATION_CHANGE = "substantive_duration_change"
    ADMINISTRATIVE_CLARIFICATION = "administrative_clarification"


_LAST_DATE_RE = re.compile(r"last\s*date", re.I)
_SUBMISSION_RECEIPT_RE = re.compile(r"submission|receipt\s*of\s*(?:tender|bid)|receipt\b", re.I)
_EXTEND_REVISE_RE = re.compile(r"extend|revis", re.I)

_SUBSTANTIVE_DURATION_RE = re.compile(
    r"(?:contract|agreement|AMC)\s*(?:period\s*)?(?:of\s*the\s*(?:contract|agreement|AMC)\s*)?"
    r".{0,10}\bperiod\b.{0,40}(?:extend|revis|modif)"
    r"|\bduration\s*(?:is\s*hereby\s*)?(?:extend|revis|modif)",
    re.I,
)

_WINDOW = 100


def _near(text: str, re_a: re.Pattern, re_b: re.Pattern, window: int = _WINDOW) -> bool:
    """True if a match of re_a and a match of re_b occur within `window`
    characters of each other, in either order."""
    matches_a = list(re_a.finditer(text))
    matches_b = list(re_b.finditer(text))
    for ma in matches_a:
        for mb in matches_b:
            if abs(ma.start() - mb.start()) <= window:
                return True
    return False


def classify_corrigendum(text: str) -> CorrigendumSubtype:
    if _SUBSTANTIVE_DURATION_RE.search(text):
        return CorrigendumSubtype.SUBSTANTIVE_DURATION_CHANGE
    if _near(text, _LAST_DATE_RE, _SUBMISSION_RECEIPT_RE) and _near(text, _LAST_DATE_RE, _EXTEND_REVISE_RE):
        return CorrigendumSubtype.BID_DEADLINE_EXTENSION
    return CorrigendumSubtype.ADMINISTRATIVE_CLARIFICATION


def is_duration_relevant(subtype: CorrigendumSubtype) -> bool:
    """Only a substantive duration change may feed contract_timeline_events
    and alter current_expiry_estimate. Everything else is stored for audit
    but is a structural non-event for duration purposes."""
    return subtype == CorrigendumSubtype.SUBSTANTIVE_DURATION_CHANGE
