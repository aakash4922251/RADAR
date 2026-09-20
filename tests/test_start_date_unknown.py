from core.extraction import extract_all_facts
from core.start_date import resolve_start_date


class FakeRow(dict):
    """sqlite3.Row-like access via both attribute and __getitem__."""
    def __getitem__(self, key):
        return dict.__getitem__(self, key)


def _facts_from_text(text, document_id=1, start_id=1):
    facts = extract_all_facts(text)
    rows = []
    for i, f in enumerate(facts, start=start_id):
        rows.append(FakeRow(
            id=i, document_id=document_id, fact_type=f.fact_type,
            fact_value=f.value, fact_confidence=f.fact_confidence,
        ))
    return rows


def test_site_handover_clause_without_award_date_is_unknown(real_examples):
    """
    Examples #6/#7: start date is defined as 'the later of Schedule F or
    site handover' — a date that frequently never appears in any published
    document. If no award_date, commencement_date_explicit, or
    site_handover_date fact exists, resolution MUST be UNKNOWN, not a
    silent fallback to anything else.
    """
    text = real_examples["ex6_iitk_civil"]["text"]
    facts = _facts_from_text(text)
    # sanity: this text contains no extractable award/commencement/handover date
    fact_types = {f["fact_type"] for f in facts}
    assert "award_date" not in fact_types
    assert "commencement_date_explicit" not in fact_types
    assert "site_handover_date" not in fact_types

    resolution = resolve_start_date(facts)
    assert resolution.start_date is None
    assert resolution.confidence == "UNKNOWN"


def test_award_date_alone_is_medium_proxy_not_high():
    facts = [FakeRow(id=1, document_id=1, fact_type="award_date",
                      fact_value="2025-03-14", fact_confidence="HIGH")]
    resolution = resolve_start_date(facts)
    assert resolution.start_date is not None
    assert resolution.confidence == "MEDIUM"
    assert "proxy" in resolution.note.lower()


def test_explicit_commencement_date_is_high_and_preferred_over_award_date():
    facts = [
        FakeRow(id=1, document_id=1, fact_type="award_date", fact_value="2025-03-14", fact_confidence="HIGH"),
        FakeRow(id=2, document_id=2, fact_type="commencement_date_explicit", fact_value="2025-04-01", fact_confidence="HIGH"),
    ]
    resolution = resolve_start_date(facts)
    assert resolution.start_date.isoformat() == "2025-04-01"
    assert resolution.confidence == "HIGH"


def test_no_facts_at_all_is_unknown():
    resolution = resolve_start_date([])
    assert resolution.start_date is None
    assert resolution.confidence == "UNKNOWN"
