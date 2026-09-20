from core.requirement_identity import (
    build_requirement_signature, detect_asset_keyword, detect_location, normalize_text,
)


def test_asset_keyword_detected_across_worded_variants():
    variants = [
        "Annual Maintenance Contract for CCTV System",
        "CCTV Surveillance System Comprehensive AMC",
        "Comprehensive Maintenance of CCTV Installation",
        "CCTV CAMC Services",
    ]
    for v in variants:
        key, evidence = detect_asset_keyword(v)
        assert key == "cctv", f"expected 'cctv' for {v!r}, got {key}"
        assert evidence is not None


def test_unrelated_categories_detected_distinctly():
    assert detect_asset_keyword("Housekeeping Services at IIT Kanpur")[0] == "housekeeping"
    assert detect_asset_keyword("Security Guard Services for the campus")[0] == "security"
    assert detect_asset_keyword("AMC of Fire Fighting System")[0] == "fire_safety"
    assert detect_asset_keyword("Vehicle Hiring Services")[0] == "vehicle_hiring"


def test_no_recognizable_asset_returns_none():
    key, evidence = detect_asset_keyword("This is a generic notice about something unrelated.")
    assert key is None
    assert evidence is None


def test_location_detected_from_gazetteer():
    assert detect_location("CAG Kolkata, CCTV AMC") == "kolkata"
    assert detect_location("IIT Kanpur housekeeping NIT") == "kanpur"
    assert detect_location("No city mentioned here") is None


def test_normalize_text_strips_punctuation_and_case():
    assert normalize_text("CCTV-AMC, Terminal Building!") == "cctv amc terminal building"


def test_build_requirement_signature_combines_title_and_org():
    sig = build_requirement_signature(
        contract_title="Comprehensive CCTV AMC", organisation_name="Airports Authority of India, Pune",
    )
    assert sig.asset_keyword == "cctv"
    assert sig.location == "pune"
    assert sig.normalized_title == "comprehensive cctv amc"


def test_signature_asset_keyword_none_when_org_name_missing_and_title_generic():
    sig = build_requirement_signature(contract_title="Miscellaneous Notice", organisation_name=None)
    assert sig.asset_keyword is None
