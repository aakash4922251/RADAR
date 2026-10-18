from ui_labels import buyer_label, evidence_label, period_label, status_label


def test_buyer_label_joins_organisation_chain_and_handles_missing_values():
    assert buyer_label("Ministry||Surveillance Division") == "Ministry · Surveillance Division"
    assert buyer_label(float("nan")) == "Not stated"
    assert buyer_label(None) == "Not stated"


def test_period_label_reads_days_months_and_years():
    assert period_label(0.49) == "15 days"
    assert period_label(0.66) == "20 days"
    assert period_label(6) == "6 months"
    assert period_label(12) == "1 year"
    assert period_label(24.0) == "2 years"
    assert period_label(float("nan")) == "—"
    assert period_label(None) == "—"


def test_tender_stage_labels():
    assert status_label("INSUFFICIENT_EVIDENCE", "TENDER") == "Waiting for award"
    assert status_label("INSUFFICIENT_EVIDENCE") == "Not enough information yet"
    assert status_label("VERIFIED") == "Confirmed by the documents"
    assert evidence_label(None, "TENDER") == "—"
    assert evidence_label("HIGH") == "Strong"
