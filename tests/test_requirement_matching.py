from core.requirement_matching import match_requirement


def _match(**overrides):
    defaults = dict(
        candidate_org_id=1, candidate_asset_keyword="cctv", candidate_location="pune",
        candidate_normalized_title="cctv surveillance system comprehensive amc",
        existing_org_id=1, existing_asset_keyword="cctv", existing_location="pune",
        existing_normalized_title="annual maintenance contract for cctv system",
    )
    defaults.update(overrides)
    return match_requirement(**defaults)


def test_same_org_same_asset_same_location_is_strong_match():
    result = _match()
    assert result.status == "STRONG_MATCH"
    assert result.score >= 0.85
    assert "exact match" in result.factors["organisation"]


def test_different_asset_is_never_matched_regardless_of_everything_else():
    """This is the critical anti-overmerge case: housekeeping must never
    match cctv, even with identical org and location."""
    result = _match(candidate_asset_keyword="housekeeping", existing_asset_keyword="cctv")
    assert result.status == "NOT_MATCHED"
    assert result.score == 0.0
    assert "different" in result.factors["asset"]


def test_confirmed_different_organisation_is_a_hard_gate():
    result = _match(candidate_org_id=1, existing_org_id=2)
    assert result.status == "NOT_MATCHED"
    assert "CONFIRMED different" in result.factors["organisation"]


def test_unresolved_organisation_is_not_treated_as_a_conflict():
    """Organisation being unknown on one/both sides must not hard-block a
    match — only a CONFIRMED conflict does. Here asset+location+decent
    title similarity should still be enough to surface a match."""
    result = _match(
        candidate_org_id=None, existing_org_id=None,
        candidate_normalized_title="annual maintenance contract for cctv system",
        existing_normalized_title="annual maintenance contract for cctv system",
    )
    assert result.status != "NOT_MATCHED"
    assert "unresolved" in result.factors["organisation"]


def test_no_asset_keyword_at_all_is_not_matched():
    result = _match(candidate_asset_keyword=None)
    assert result.status == "NOT_MATCHED"


def test_match_result_always_carries_explanation_and_factors():
    """Every result, matched or not, must be explainable — never a bare
    MATCH=TRUE with no reasoning."""
    for result in [_match(), _match(candidate_asset_keyword="housekeeping")]:
        assert result.explanation
        assert result.factors
        assert "asset" in result.factors


def test_conflicting_location_lowers_score_but_is_not_a_hard_gate():
    matched = _match()
    conflicting_location = _match(candidate_location="mumbai", existing_location="pune")
    assert conflicting_location.score < matched.score
    assert conflicting_location.status != "NOT_MATCHED" or conflicting_location.score == 0.0
    assert "not a hard gate" in conflicting_location.factors["location"]


def test_low_title_similarity_with_no_org_or_location_can_fail_review_threshold():
    """With organisation and location both unresolved, and very different
    titles, the score should be low enough to require review or fail
    outright — asset match alone must not be sufficient for a strong claim."""
    result = _match(
        candidate_org_id=None, existing_org_id=None,
        candidate_location=None, existing_location=None,
        candidate_normalized_title="completely different wording about cameras",
        existing_normalized_title="totally unrelated phrasing for monitoring",
    )
    assert result.status in ("NOT_MATCHED", "REVIEW_REQUIRED")
