"""
Seeds the database with a handful of demo contracts built directly from
the real documents examined in the research (spec Part 1), so a first-
time user has something to explore immediately.

Run with:  python demo_data/demo_seed.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import database as db
from core import pipeline


def seed():
    db.init_db()
    conn = db.get_connection()
    cur = conn.cursor()

    # --- Demo 1: matches spec Part 7's exact worked example -------------
    r1 = pipeline.ingest_document(
        cur, doc_type="nit", contract_title="AMC of Fire Fighting System, Terminal Building",
        raw_text="AMC of Fire Fighting System, Terminal Building. AMC for a period of 2 years.",
        doc_title="NIT", source_url="https://eprocure.gov.in/example/nit.pdf",
    )
    conn.commit()
    pipeline.ingest_document(
        cur, doc_type="aoc", contract_id=r1.contract_id,
        raw_text="Award of Contract. Award Date: 14-Mar-2025. Awarded to M/s ABC Fire Services Pvt Ltd.",
        doc_title="AOC", source_url="https://eprocure.gov.in/example/aoc.pdf",
    )
    conn.commit()

    # --- Demo 2: example #4/#6 pattern — composite base+extension, and a
    #     start-date-UNKNOWN case (CPWD-style site-handover clause) -------
    r2 = pipeline.ingest_document(
        cur, doc_type="nit", contract_title="Civil Maintenance Works — IIT Kanpur",
        raw_text=(
            "Time allowed for completion of the work shall be Twelve (12) months "
            "(extendable yearly up to two more years) from the date of start as "
            "defined in Schedule 'F' or from the first date of handing over of the "
            "site, whichever is later."
        ),
        doc_title="NIT",
    )
    conn.commit()

    # --- Demo 3: explicit calendar date pair (example #8) ----------------
    r3 = pipeline.ingest_document(
        cur, doc_type="nit", contract_title="CCTV CAMC — CAG Kolkata",
        raw_text=(
            "The contract will be for periods of one year from 15th June'2022 to "
            "14th May'2023 or one year from the date of acceptance of award of "
            "contract, whichever is earlier, and may be extended on mutual consent "
            "for a further period of one more year."
        ),
        doc_title="NIT",
    )
    conn.commit()

    # --- Demo 4: full extension-exercised lifecycle (spec Part 5.2) ------
    r4 = pipeline.ingest_document(
        cur, doc_type="nit", contract_title="Vehicle Hiring Services — GeM AMC",
        raw_text="This NIT is for vehicle hiring services for a period of 2 years, renewable subject to performance.",
        doc_title="NIT",
    )
    conn.commit()
    pipeline.ingest_document(cur, doc_type="aoc", contract_id=r4.contract_id, raw_text="Award Date: 10-Jan-2026.")
    conn.commit()
    pipeline.ingest_document(cur, doc_type="work_order", contract_id=r4.contract_id,
                              raw_text="Work order. The work shall commence on 01-Feb-2026.")
    conn.commit()
    pipeline.ingest_document(
        cur, doc_type="work_order", contract_id=r4.contract_id,
        raw_text=("Work order. The contract period has been extended vide this work "
                   "order dated 15-Feb-2028; new end date shall be 31-Mar-2028."),
    )
    conn.commit()

    # --- Demo 5: a bid-deadline corrigendum that must NOT touch expiry ---
    r5 = pipeline.ingest_document(
        cur, doc_type="nit", contract_title="Security Services AMC — UIDAI Chandigarh",
        raw_text="AMC for a period of 1 year. Award Date: 01-Jan-2024.",
    )
    conn.commit()
    pipeline.ingest_document(
        cur, doc_type="corrigendum", contract_id=r5.contract_id,
        raw_text=("Corrigendum clarifies the applicable minimum-wage authority and "
                   "extends the last date for receipt of tender by two weeks."),
    )
    conn.commit()

    # --- Demo 6: conflicting evidence, for the Review Queue --------------
    r6 = pipeline.ingest_document(
        cur, doc_type="nit", contract_title="Housekeeping Services — Disputed Duration",
        raw_text="Housekeeping services for a period of 2 years. Award Date: 01-Jun-2024.",
    )
    conn.commit()
    pipeline.ingest_document(
        cur, doc_type="nit", contract_id=r6.contract_id,
        raw_text="Housekeeping services for a period of 3 years. Award Date: 01-Jun-2024.",
    )
    conn.commit()

    conn.close()
    print("Demo data seeded. Run: streamlit run app.py")


if __name__ == "__main__":
    seed()
