from core.duration import extract_duration_candidates


def test_example4_produces_three_separate_facts_not_collapsed(real_examples):
    """
    Spec §4.6 / example #4: "1 year, extendable yearly up to 2 more years"
    MUST produce base_duration_months=12, extension_option_months=12,
    extension_option_count=2 as THREE separate facts — never a single
    collapsed duration_months=36.
    """
    text = real_examples["ex4_iitk_housekeeping"]["text"]
    candidates = [c for c in extract_duration_candidates(text) if c.accepted]
    by_type = {c.fact_type: c for c in candidates}

    assert "base_duration_months" in by_type
    assert "extension_option_months" in by_type
    assert "extension_option_count" in by_type

    assert by_type["base_duration_months"].value == "12"
    assert by_type["extension_option_months"].value == "12"
    assert by_type["extension_option_count"].value == "2"

    # explicitly assert the collapsed (wrong) answer never appears
    assert not any(c.value == "36" for c in candidates)


def test_example6_site_handover_composite_also_separates(real_examples):
    text = real_examples["ex6_iitk_civil"]["text"]
    candidates = [c for c in extract_duration_candidates(text) if c.accepted]
    by_type = {c.fact_type: c for c in candidates}
    assert by_type["base_duration_months"].value == "12"
    assert by_type["extension_option_months"].value == "12"
    assert by_type["extension_option_count"].value == "2"


def test_plus_shorthand_separates_base_and_extension(real_examples):
    text = real_examples["synthetic_plus_shorthand"]["text"]
    candidates = [c for c in extract_duration_candidates(text) if c.accepted]
    by_type = {c.fact_type: c for c in candidates}
    assert by_type["base_duration_months"].value == "12"
    assert by_type["extension_option_months"].value == "12"
