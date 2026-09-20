from core import pipeline
from db import database as db


def test_repeated_layer2_cycles_create_transparent_prediction_window(cur):
    titles = [
        ("CCTV AMC for XYZ Hospital", "2022-01-15"),
        ("CCTV Surveillance AMC XYZ Hospital", "2023-01-20"),
        ("Annual CCTV Maintenance XYZ Hospital", "2024-01-10"),
    ]
    requirement_id = None
    for title, published in titles:
        result = pipeline.ingest_document(
            cur,
            raw_text=f"{title}. Tendering authority: XYZ Hospital, Pune. AMC for a period of 1 year.",
            doc_type="nit",
            contract_title=title,
            doc_date=published,
        )
        requirement_id = result.requirement_link.requirement_id

    prediction = db.get_prediction_for_requirement(cur, requirement_id)
    assert prediction is not None
    assert prediction["anchor_event_type"] == "tender_published"
    assert prediction["predicted_date"] == "2025-01-06"
    assert prediction["window_start"] < prediction["predicted_date"] < prediction["window_end"]
    assert prediction["confidence"] in ("LOW", "MEDIUM", "HIGH")