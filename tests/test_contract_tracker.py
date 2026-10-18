"""Contract tracker: automatic document intake on top of the evidence pipeline."""
import io
import zipfile
from datetime import date

from core.contract_tracker import contract_title_for, portfolio, track_documents
from db import database as db

AWARD = (b"LETTER OF AWARD\nSubject: Annual maintenance contract for fire fighting system at Terminal Building\n"
         b"M/s Suraksha Fire Services Pvt Ltd\nAward Date: 14-Mar-2025.\n"
         b"The contract shall be for a period of 2 years from the date of award.")
WORK_ORDER = (b"WORK ORDER\nSubject: Housekeeping services at hospital campus block A\n"
              b"Contract period from 01/04/2025 to 31/03/2026.")
NO_DATES = (b"LETTER OF AWARD\nSubject: Supply of office furniture to the main block\n"
            b"Award Date: 10-Jan-2025. Delivery schedule will be intimated separately.")
NO_CONTRACT_INFO = b"Bid opening committee members: Shri A (Member), Dr B (Bid Opener)."


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files:
            z.writestr(name, data)
    return buf.getvalue()


def test_each_file_becomes_a_contract_with_its_end_date(cur):
    results = track_documents(cur, [("award.txt", AWARD), ("wo.txt", WORK_ORDER)])
    assert [r.status for r in results] == ["ADDED", "ADDED"]
    by_file = {r.filename: r for r in results}
    assert by_file["award.txt"].doc_type == "loa"
    assert by_file["award.txt"].end_date == "2027-03-14"
    assert by_file["award.txt"].contract_title == "Annual maintenance contract for fire fighting system at Terminal Building"
    assert by_file["wo.txt"].doc_type == "work_order"
    assert by_file["wo.txt"].end_date == "2026-03-31"


def test_zip_is_unpacked_and_repeat_upload_is_recognised(cur):
    first = track_documents(cur, [("batch.zip", _zip([("a/award.txt", AWARD), ("notes.exe", b"x")]))])
    assert [(r.filename, r.status) for r in first] == [("batch.zip/a/award.txt", "ADDED")]
    again = track_documents(cur, [("award-copy.txt", AWARD)])
    assert again[0].status == "ALREADY_TRACKED"
    assert again[0].contract_id == first[0].contract_id
    assert len(db.list_contracts(cur)) == 1


def test_same_contract_option_attaches_all_files_to_one_contract(cur):
    results = track_documents(cur, [("award.txt", AWARD), ("wo.txt", WORK_ORDER)], same_contract=True)
    assert len({r.contract_id for r in results}) == 1
    assert len(db.list_documents_for_contract(cur, results[0].contract_id)) == 2


def test_bad_file_does_not_stop_the_batch(cur):
    results = track_documents(cur, [("empty.txt", b"   "), ("award.txt", AWARD), ("tool.exe", b"MZ"),
                                    ("broken.png", b"\x89PNG")])
    assert [r.status for r in results] == ["FAILED", "ADDED", "SKIPPED", "FAILED"]


def test_portfolio_orders_by_end_date_with_unknown_last(cur):
    track_documents(cur, [("none.txt", NO_DATES), ("award.txt", AWARD), ("wo.txt", WORK_ORDER)])
    df = portfolio(cur, today=date(2026, 1, 1))
    assert list(df["current_expiry_estimate"].fillna("UNKNOWN")) == ["2026-03-31", "2027-03-14", "UNKNOWN"]
    assert list(df["days_left"].iloc[:2]) == [89, 437]
    assert df.iloc[0]["documents"] == 1


def test_title_falls_back_to_file_name():
    assert contract_title_for("CCTV_AMC-Pune 2025.pdf", "No subject line here.") == "CCTV AMC Pune 2025"


def test_buyer_is_read_from_letterhead(cur):
    letter = b"HINDUSTAN PETROLEUM CORPORATION LIMITED\n" + AWARD
    result = track_documents(cur, [("hpcl.txt", letter)])[0]
    df = portfolio(cur)
    assert df.iloc[0]["buyer"] == "HINDUSTAN PETROLEUM CORPORATION LIMITED"
    assert df.iloc[0]["vendor"] == "Suraksha Fire Services Pvt Ltd"
    assert result.end_date == "2027-03-14"


def test_remove_contract_deletes_it_and_its_evidence(cur):
    from core import pipeline

    keep, gone = track_documents(cur, [("award.txt", AWARD), ("wo.txt", WORK_ORDER)])
    pipeline.remove_contract(cur, gone.contract_id)
    assert [c["id"] for c in db.list_contracts(cur)] == [keep.contract_id]
    for table in ("documents", "extracted_facts", "expiry_calculations", "confidence_assessment"):
        n = cur.execute(f"SELECT COUNT(*) FROM {table} WHERE contract_id = ?", (gone.contract_id,)).fetchone()[0]
        assert n == 0, table
    assert cur.execute("SELECT COUNT(*) FROM procurement_events WHERE contract_id = ?",
                       (gone.contract_id,)).fetchone()[0] == 0
    # the same file can be tracked again afterwards
    assert track_documents(cur, [("wo.txt", WORK_ORDER)])[0].status == "ADDED"


def test_document_without_contract_information_is_not_added(cur):
    result = track_documents(cur, [("committee.txt", NO_CONTRACT_INFO)])[0]
    assert result.status == "SKIPPED" and "No contract information" in result.message
    assert db.list_contracts(cur) == []


def test_letterhead_noise_and_address_are_trimmed():
    from core.extraction import _clean_letterhead
    assert _clean_letterhead("36% VISVESVARAYA NATIONAL INSTITUTE OF TECHNOLOGY, SOUTH AMBAZARI ROAD, NAGPUR — 440 010") \
        == "VISVESVARAYA NATIONAL INSTITUTE OF TECHNOLOGY"
    assert _clean_letterhead("eae HINDUSTAN PETROLEUM CORPORATION LIMITED") == "HINDUSTAN PETROLEUM CORPORATION LIMITED"
    assert _clean_letterhead("Indian Institute of Technology, Kanpur") == "Indian Institute of Technology, Kanpur"
