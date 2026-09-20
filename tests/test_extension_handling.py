from datetime import date
from core.expiry_engine import compute_expiry


class FakeRow(dict):
    def __getitem__(self, key):
        return dict.__getitem__(self, key)


def test_unexercised_extension_never_becomes_current_estimate():
    """
    Spec §4.6: current_expiry_estimate is ALWAYS the conservative base
    term unless direct evidence of an exercised extension exists.
    potential_max_expiry (the ceiling) must never leak into
    current_expiry_estimate just because an option exists.
    """
    facts = [
        FakeRow(id=1, document_id=1, fact_type="commencement_date_explicit",
                fact_value="2024-01-01", fact_confidence="HIGH"),
        FakeRow(id=2, document_id=1, fact_type="base_duration_months",
                fact_value="12", fact_confidence="HIGH"),
        FakeRow(id=3, document_id=1, fact_type="extension_option_months",
                fact_value="12", fact_confidence="HIGH"),
        FakeRow(id=4, document_id=1, fact_type="extension_option_count",
                fact_value="2", fact_confidence="HIGH"),
    ]
    result = compute_expiry(facts)
    assert result.current_expiry_estimate == date(2025, 1, 1)  # base term only
    assert result.current_expiry_ceiling == date(2026, 1, 1)   # +12 months ceiling
    assert result.extension_exercised is False


def test_exercised_extension_overrides_current_estimate():
    facts = [
        FakeRow(id=1, document_id=1, fact_type="commencement_date_explicit",
                fact_value="2024-01-01", fact_confidence="HIGH"),
        FakeRow(id=2, document_id=1, fact_type="base_duration_months",
                fact_value="12", fact_confidence="HIGH"),
        FakeRow(id=3, document_id=2, fact_type="extension_exercised_new_end_date",
                fact_value="2025-06-30", fact_confidence="HIGH"),
    ]
    result = compute_expiry(facts)
    assert result.current_expiry_estimate == date(2025, 6, 30)
    assert result.extension_exercised is True
    assert result.calculation_type == "with_extension"


def test_absolute_ceiling_caps_the_extension_ceiling():
    facts = [
        FakeRow(id=1, document_id=1, fact_type="commencement_date_explicit",
                fact_value="2022-10-25", fact_confidence="HIGH"),
        FakeRow(id=2, document_id=1, fact_type="base_duration_months",
                fact_value="18", fact_confidence="HIGH"),
        FakeRow(id=3, document_id=1, fact_type="extension_option_months",
                fact_value="12", fact_confidence="HIGH"),
        FakeRow(id=4, document_id=1, fact_type="absolute_expiry_ceiling",
                fact_value="2025-04-23", fact_confidence="HIGH"),
    ]
    result = compute_expiry(facts)
    # base + option would be 2022-10-25 + 30 months = 2025-04-25, but the
    # absolute ceiling of 2025-04-23 must cap it.
    assert result.current_expiry_ceiling == date(2025, 4, 23)
