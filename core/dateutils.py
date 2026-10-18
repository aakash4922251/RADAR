"""
Deterministic date parsing helpers.

Kept separate from extraction.py so both the extractor and the expiry
engine share exactly one notion of "how do we parse a date string" —
no drift between the two.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_ORDINAL_SUFFIX = re.compile(r"(\d+)(st|nd|rd|th)", re.I)

# 14-Mar-2025 / 14 Mar 2025 / 14th March, 2025
_DATE_PATTERNS = [
    # "25 day of October 2022" / "25th day of October, 2022" — UK-style / formal award letters
    re.compile(
        r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+day\s+of\s+"
        r"(?P<month>[A-Za-z]{3,9})[\s,]+(?P<year>\d{4})\b", re.I
    ),
    re.compile(
        r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?[\s\-/]+"
        r"(?P<month>[A-Za-z]{3,9})['’]?[\s\-/,]*(?P<year>\d{4})\b"
    ),
    # ISO: 2025-03-14
    re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})\b"),
    # 14/03/2025 or 14-03-2025 (DD/MM/YYYY, Indian convention)
    re.compile(r"\b(?P<day>\d{1,2})[/-](?P<month>\d{1,2})[/-](?P<year>\d{4})\b"),
]


def parse_date_any(text: str) -> Optional[date]:
    """Parse the first recognisable date in a short text span. Returns None,
    never guesses, if nothing unambiguous is found."""
    text = _ORDINAL_SUFFIX.sub(r"\1", text)
    for pattern in _DATE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        gd = m.groupdict()
        try:
            year = int(gd["year"])
            month_raw = gd["month"]
            month = _MONTHS.get(month_raw.lower()) if not month_raw.isdigit() else int(month_raw)
            day = int(gd["day"])
            if month is None or not (1 <= month <= 12) or not (1 <= day <= 31):
                continue
            return date(year, month, day)
        except (ValueError, KeyError):
            continue
    return None


def add_months(d: date, months: int) -> date:
    """Calendar-correct month addition (handles month-end clamping)."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    # clamp day to last valid day of target month
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    day = min(d.day, last_day)
    return date(year, month, day)


def add_days(d: date, days: int) -> date:
    from datetime import timedelta
    return d + timedelta(days=days)
