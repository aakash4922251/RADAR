from core.corrigendum import classify_corrigendum, CorrigendumSubtype, is_duration_relevant


def test_all_real_corrigenda_classify_as_non_duration(real_examples):
    """
    Spec §4.4: 7 of 7 real corrigenda examined touched a bid deadline or an
    administrative point; ZERO touched contract duration. Assert both real
    examples in our fixture set classify accordingly.
    """
    for ex_id in ("ex9_corrigendum_bid_deadline", "ex10_corrigendum_admin"):
        text = real_examples[ex_id]["text"]
        subtype = classify_corrigendum(text)
        assert subtype == CorrigendumSubtype.BID_DEADLINE_EXTENSION
        assert not is_duration_relevant(subtype)


def test_substantive_duration_corrigendum_detected(real_examples):
    text = real_examples["synthetic_substantive_corrigendum"]["text"]
    subtype = classify_corrigendum(text)
    assert subtype == CorrigendumSubtype.SUBSTANTIVE_DURATION_CHANGE
    assert is_duration_relevant(subtype)


def test_administrative_only_corrigendum_not_duration_relevant():
    text = ("This corrigendum revises the minimum wage rate applicable under the "
            "labour laws for this tender. No other changes.")
    subtype = classify_corrigendum(text)
    assert subtype == CorrigendumSubtype.ADMINISTRATIVE_CLARIFICATION
    assert not is_duration_relevant(subtype)


def test_zero_of_real_corrigenda_are_duration_relevant(real_examples):
    """Base-rate check: across every real corrigendum fixture, none should
    trigger a duration-relevant classification (this is the 7/7 finding)."""
    real_corrigenda = [v for k, v in real_examples.items() if k.startswith("ex") and v["doc_type"] == "corrigendum"]
    assert real_corrigenda, "expected at least one real corrigendum fixture"
    for ex in real_corrigenda:
        subtype = classify_corrigendum(ex["text"])
        assert not is_duration_relevant(subtype), f"{ex['id']} should not be duration-relevant"
