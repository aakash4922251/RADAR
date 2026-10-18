"""Explicit "from <date> to <date>" contract periods in every common date shape."""
import pytest

from core.duration import extract_duration_candidates


def _ranges(text):
    return [c.value for c in extract_duration_candidates(text)
            if c.fact_type == "base_duration_explicit_end_date"]


@pytest.mark.parametrize("text", [
    "Contract period from 01/04/2024 to 31/03/2025.",
    "Contract period from 01-04-2024 till 31-03-2025.",
    "Contract period from 01.04.2024 upto 31.03.2025.",
    "Contract period from 1st April 2024 to 31st March 2025.",
    "Contract period from 2024-04-01 until 2025-03-31.",
])
def test_range_formats_produce_explicit_end_date(text):
    assert _ranges(text) == ["2024-04-01|2025-03-31"]


def test_reversed_range_is_rejected():
    assert _ranges("Contract period from 31/03/2025 to 01/04/2024.") == []


def test_numeric_range_is_not_double_counted_as_simple_duration():
    cands = extract_duration_candidates("AMC from 01/04/2024 to 31/03/2025.")
    assert [c.fact_type for c in cands] == ["base_duration_explicit_end_date"]
