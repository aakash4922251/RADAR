"""Plain-language labels for the contract screen."""
from __future__ import annotations

from core.expiry_engine import AVERAGE_MONTH_DAYS

_STATUS = {
    "CONFLICTING_EVIDENCE": "Documents disagree",
    "INSUFFICIENT_EVIDENCE": "Not enough information yet",
    "HIGH_CONFIDENCE": "Strong supporting evidence",
    "VERIFIED": "Confirmed by the documents",
    "NEEDS_REVIEW": "Needs a closer look",
    "EXPIRED_UNVERIFIED": "May have ended",
    "EXPIRED": "End date has passed",
    "EXTENDED": "Extension recorded",
    "CANCELLED": "Cancellation recorded",
    "UNKNOWN": "Status not known yet",
}

_EVIDENCE = {"HIGH": "Strong", "MEDIUM": "Good", "LOW": "Weak"}


def status_label(status: str, stage: str = "AWARDED") -> str:
    if stage == "TENDER" and status == "INSUFFICIENT_EVIDENCE":
        return "Waiting for award"
    return _STATUS.get(status, "Status not known yet")


def evidence_label(confidence, stage: str = "AWARDED") -> str:
    return _EVIDENCE.get(confidence, "—" if stage == "TENDER" else "Not enough")


def buyer_label(value) -> str:
    """'Ministry of X||Division Y' -> 'Ministry of X · Division Y'. pandas hands
    missing values over as NaN (a float), not None."""
    if not isinstance(value, str) or not value.strip():
        return "Not stated"
    return " · ".join(part.strip() for part in value.split("||") if part.strip())


def period_label(months) -> str:
    """Contract term as the documents state it: day-based terms are stored as
    fractional months (see core.duration), whole months as integers."""
    if not isinstance(months, (int, float)) or months != months:
        return "—"
    if float(months).is_integer():
        m = int(months)
        if m and m % 12 == 0:
            return f"{m // 12} year{'s' if m > 12 else ''}"
        return f"{m} month{'s' if m != 1 else ''}"
    days = round(months * AVERAGE_MONTH_DAYS)
    return f"{days} day{'s' if days != 1 else ''}"
