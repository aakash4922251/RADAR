"""
Context classifier for numbers that *look* like a contract duration but
are not one.

Every pattern here is backed by a real observed document from the
research (Trust & Evidence Architecture v2.0, Part 1, examples #3, #18,
#19). This module is deliberately conservative: when a duration-shaped
number sits inside one of these contexts, we REJECT it rather than
report a false duration.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class RejectClass(str, Enum):
    BID_VALIDITY = "bid_validity"
    DELIVERY_PERIOD = "delivery_period"
    WARRANTY_DLP = "warranty_dlp"
    EXPERIENCE_TURNOVER = "experience_turnover"
    PBG_EMD_VALIDITY = "pbg_emd_validity"
    MOBILISATION_PAYMENT = "mobilisation_payment"
    NONE = "none"


# Each entry: (RejectClass, compiled regex). Matched against a window of
# text *around* a candidate duration span (context_window), not the whole
# document, so a rejected clause elsewhere in the doc doesn't blind us to
# a real duration clause a few paragraphs later.
_REJECT_PATTERNS: list[tuple[RejectClass, re.Pattern]] = [
    (
        RejectClass.BID_VALIDITY,
        re.compile(r"\bbid\s*(validity|shall\s*remain\s*valid)\b", re.I),
    ),
    (
        RejectClass.DELIVERY_PERIOD,
        re.compile(
            r"\b(delivery\s*period|standard\s*delivery|DP\b.{0,15}(180|365)\s*days?)\b",
            re.I,
        ),
    ),
    (
        RejectClass.WARRANTY_DLP,
        re.compile(
            r"\b(warrant(y|ies)|defect\s*liability\s*period|DLP)\b", re.I
        ),
    ),
    (
        RejectClass.EXPERIENCE_TURNOVER,
        re.compile(
            r"\b(past\s*experience|similar\s*(services|works)|last\s*(three|3|five|5)\s*"
            r"(years|financial\s*years)|annual\s*turnover|audited\s*financial\s*statement)\b",
            re.I,
        ),
    ),
    (
        RejectClass.PBG_EMD_VALIDITY,
        re.compile(
            r"\b(performance\s*(bank\s*)?guarantee|PBG|earnest\s*money|EMD)\b.{0,60}"
            r"\bvalid",
            re.I,
        ),
    ),
    (
        RejectClass.MOBILISATION_PAYMENT,
        re.compile(r"\b(mobilisation|mobilization)\s*period\b", re.I),
    ),
]


@dataclass
class ContextClassification:
    reject_class: RejectClass
    matched_pattern: str | None


def classify_context(context_window: str) -> ContextClassification:
    """
    Given the text immediately surrounding a candidate duration number,
    decide whether it belongs to a REJECT class. Order matters only in
    that the first match wins; patterns are written to be mutually
    exclusive in practice.
    """
    for reject_class, pattern in _REJECT_PATTERNS:
        m = pattern.search(context_window)
        if m:
            return ContextClassification(reject_class=reject_class, matched_pattern=m.group(0))
    return ContextClassification(reject_class=RejectClass.NONE, matched_pattern=None)


def is_accept_context(context_window: str) -> bool:
    return classify_context(context_window).reject_class == RejectClass.NONE
