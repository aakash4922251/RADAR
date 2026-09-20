from core.duration import extract_duration_candidates


def test_experience_clause_rejected(real_examples):
    text = real_examples["ex3_reject_experience"]["text"]
    candidates = extract_duration_candidates(text)
    assert candidates, "expected at least one duration-shaped candidate to be found"
    assert all(not c.accepted for c in candidates), \
        "the 'last three years... Past Experience' clause must be rejected, not treated as a duration"
    assert any(c.reject_class == "experience_turnover" for c in candidates)


def test_delivery_period_rejected(real_examples):
    text = real_examples["ex18_reject_delivery"]["text"]
    candidates = extract_duration_candidates(text)
    assert candidates
    assert all(not c.accepted for c in candidates)
    assert any(c.reject_class == "delivery_period" for c in candidates)


def test_pbg_validity_rejected_and_distinct_from_contract_duration(real_examples):
    text = real_examples["ex19_reject_pbg"]["text"]
    candidates = extract_duration_candidates(text)
    assert candidates
    # the "1 year 6 months" PBG figure must be rejected
    assert all(not c.accepted for c in candidates)
    assert any(c.reject_class == "pbg_emd_validity" for c in candidates)


def test_bid_validity_rejected():
    text = "The bid validity period shall remain valid for 180 days from the date of bid opening."
    candidates = extract_duration_candidates(text)
    assert candidates
    assert all(not c.accepted for c in candidates)


def test_warranty_dlp_rejected():
    text = "The defect liability period (DLP) shall be 12 months from the date of completion."
    candidates = extract_duration_candidates(text)
    assert candidates
    assert all(not c.accepted for c in candidates)


def test_real_contract_duration_is_accepted(real_examples):
    text = real_examples["ex4_iitk_housekeeping"]["text"]
    candidates = extract_duration_candidates(text)
    accepted = [c for c in candidates if c.accepted]
    assert accepted, "a genuine contract-duration clause must be accepted"
