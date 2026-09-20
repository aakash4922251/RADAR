from pathlib import Path

from acquisition.base import AcquisitionBlocked, SourceConnector
from acquisition.connectors.cppp import CPPPConnector
from acquisition.downloader import DocumentDownloadError, download_document
from acquisition.http import HttpResponse
from acquisition.models import DiscoveryResult, DocumentCandidate, TenderRecord
from acquisition.scanner import scan_source
from db import database as db


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


class FixtureConnector(SourceConnector):
    source_id = "fixture"
    source_name = "Fixture Procurement Portal"
    capabilities = frozenset({"discovery", "document_download"})

    def __init__(self, client, records):
        self.client = client
        self.records = records
        self.base_url = "https://fixture.example"

    def discover(self, since=None):
        return DiscoveryResult(records=self.records)


def test_cppp_listing_parser_extracts_records_and_documents():
    html = """
    <table>
      <tr><th>Tender ID</th><th>Tender Title</th><th>Organisation</th><th>Published Date</th><th>Closing Date</th><th>Detail</th></tr>
      <tr><td>CPPP/2026/ABC/001</td><td>CCTV AMC Services</td><td>XYZ Hospital</td><td>2026-09-20</td><td>2026-10-05</td><td><a href="docs/nit-001.pdf">NIT</a></td></tr>
      <tr><td>CPPP/2026/ABC/002</td><td>Housekeeping Services</td><td>City Council</td><td>2026-09-20</td><td>2026-10-06</td><td><a href="docs/nit-002.pdf">NIT</a></td></tr>
    </table>
    """
    records = CPPPConnector.parse_listing(html, "https://fixture.example/list")
    assert [record.external_id for record in records] == ["CPPP/2026/ABC/001", "CPPP/2026/ABC/002"]
    assert records[0].organisation == "XYZ Hospital"
    assert records[0].document_urls[0].document_url.endswith("/docs/nit-001.pdf")


def test_cppp_homepage_active_tenders_structure_is_parsed():
        html = """
        <table id="activeTenders">
            <tr class="even" id="informal">
                <td><a id="DirectLink" href="/eprocure/app?component=%24DirectLink&amp;page=Home&amp;service=direct&amp;session=T&amp;sp=abc">1. Maintenance of Various Sports Complexes.</a></td>
                <td>39/EE(E)/ELD-3/DDA/2026-27</td>
                <td>28-Sep-2026 03:00 PM</td>
                <td>29-Sep-2026 03:30 PM</td>
            </tr>
            <tr class="odd" id="informal_0">
                <td><a id="DirectLink_0" href="/eprocure/app?component=%24DirectLink&amp;page=Home&amp;service=direct&amp;session=T&amp;sp=def">2. CARRYING OUT SUPPLY AND APPLICATION OF INSULATION</a></td>
                <td>BANDR/HO/NIT/71148 / INSULATION/SC/NIT/04</td>
                <td>09-Oct-2026 04:30 PM</td>
                <td>12-Oct-2026 04:30 PM</td>
            </tr>
        </table>
        """
        records = CPPPConnector.parse_listing(html, "https://eprocure.gov.in/eprocure/app")
        assert len(records) == 2
        assert records[0].external_id == "39/EE(E)/ELD-3/DDA/2026-27"
        assert records[0].title == "Maintenance of Various Sports Complexes."
        assert records[0].organisation is None
        assert records[0].closing_at == "28-Sep-2026 03:00 PM"
        assert records[0].detail_url.startswith("https://eprocure.gov.in/eprocure/app?")
        assert records[0].raw_metadata["listing_route"] == "homepage#activeTenders"


def test_cppp_detail_extracts_verified_fields_and_download_links():
        detail_url = "https://fixture.example/detail"
        detail_html = """
        <table>
            <tr><td class="td_caption"><b>Organisation Chain</b></td><td class="td_field"><b>Delhi Development Authority||Electrical Division</b></td></tr>
            <tr><td class="td_caption"><b>Published Date</b></td><td class="td_field">19-Sep-2026 06:50 PM</td></tr>
            <tr><td class="td_caption"><b>Tender Value in &#8377;</b></td><td class="td_field">18,22,935</td></tr>
            <tr><td class="td_caption"><b>Location</b></td><td class="td_field">Jasola</td></tr>
        </table>
        <a href="/eprocure/app?component=docDownoad&amp;page=FrontEndTenderDetails">Tendernotice_1.pdf</a>
        <a href="/eprocure/app?component=nav&amp;page=WebAwards">Bid Awards</a>
        <a href="/eprocure/app?component=zip&amp;page=FrontEndTenderDetails">Download as zip file</a>
        """
        client = FakeClient({detail_url: HttpResponse(detail_url, 200, {"Content-Type": "text/html"}, detail_html.encode())})
        connector = CPPPConnector(client)
        record = TenderRecord(source_id="cppp", external_id="REF-1", title="Tender", detail_url=detail_url)
        enriched = connector.fetch_details(record)
        assert enriched.organisation == "Delhi Development Authority||Electrical Division"
        assert enriched.published_at == "19-Sep-2026 06:50 PM"
        assert enriched.estimated_value == "18,22,935"
        assert enriched.location == "Jasola"
        assert len(enriched.document_urls) == 2
        assert all("WebAwards" not in item.document_url for item in enriched.document_urls)


def test_scanner_ingests_new_document_into_layer1_and_layer2_once(cur, tmp_path):
    document_url = "https://fixture.example/docs/cctv.txt"
    body = b"CCTV AMC for XYZ Hospital. Tendering authority: XYZ Hospital, Pune. AMC for a period of 1 year."
    client = FakeClient({document_url: HttpResponse(document_url, 200, {"Content-Type": "text/plain"}, body)})
    record = TenderRecord(
        source_id="fixture", external_id="T-001", title="CCTV AMC Services",
        organisation="XYZ Hospital", published_at="2026-09-20",
        source_page_url="https://fixture.example/list",
        document_urls=[DocumentCandidate(document_url, "tender.txt")],
    )
    connector = FixtureConnector(client, [record])

    first = scan_source(cur, connector, client=client, storage_root=str(tmp_path))
    second = scan_source(cur, connector, client=client, storage_root=str(tmp_path))

    assert first.records_new == 1
    assert first.documents_downloaded == 1
    assert first.documents_processed == 1
    assert second.records_new == 0
    assert second.documents_skipped == 1
    assert client.calls == [document_url]
    assert cur.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    assert cur.execute("SELECT COUNT(*) FROM procurement_events WHERE event_type = 'tender_published'").fetchone()[0] == 1
    assert cur.execute("SELECT COUNT(*) FROM requirements").fetchone()[0] == 1
    acquired = cur.execute("SELECT * FROM acquired_documents").fetchone()
    assert acquired["status"] == "PROCESSED"
    assert acquired["sha256"]
    assert Path(acquired["local_path"]).exists()


def test_documentless_discovery_is_marked_metadata_only(cur):
    record = TenderRecord(
        source_id="fixture", external_id="T-003", title="Metadata Only Tender",
        published_at="2026-09-20", source_page_url="https://fixture.example/list",
    )
    summary = scan_source(cur, FixtureConnector(FakeClient({}), [record]))
    assert summary.records_seen == 1
    stored = cur.execute(
        "SELECT status, acquisition_status FROM discovered_records WHERE external_id = 'T-003'"
    ).fetchone()
    assert stored["status"] == "DISCOVERED"
    assert stored["acquisition_status"] == "DISCOVERED_METADATA_ONLY"


def test_init_db_backfills_old_pending_documentless_discoveries(cur):
    source_id = db.upsert_acquisition_source(
        cur, source_key="fixture", name="Fixture Procurement Portal", base_url="https://fixture.example"
    )
    cur.execute(
        """INSERT INTO discovered_records (source_id, external_id, title)
           VALUES (?, 'OLD-PENDING', 'Old metadata record')""",
        (source_id,),
    )
    cur.execute("UPDATE discovered_records SET acquisition_status = 'PENDING' WHERE external_id = 'OLD-PENDING'")
    cur.connection.commit()
    db.init_db(cur.connection.execute("PRAGMA database_list").fetchone()[2])
    stored = cur.execute(
        "SELECT status, acquisition_status FROM discovered_records WHERE external_id = 'OLD-PENDING'"
    ).fetchone()
    assert stored["acquisition_status"] == "DISCOVERED_METADATA_ONLY"


def test_captcha_discovery_is_persisted_as_manual_action(cur):
    class CaptchaConnector(FixtureConnector):
        def discover(self, since=None):
            return DiscoveryResult(manual_action_required="CAPTCHA_REQUIRED")

    connector = CaptchaConnector(FakeClient({}), [])
    summary = scan_source(cur, connector, storage_root="data/test-acquired")
    assert summary.status == "MANUAL_ACTION_REQUIRED"
    assert summary.manual_action_required == 1
    source = cur.execute("SELECT * FROM acquisition_sources WHERE source_key = 'fixture'").fetchone()
    run = cur.execute("SELECT * FROM acquisition_runs WHERE id = ?", (summary.run_id,)).fetchone()
    assert source["status"] == "MANUAL_ACTION_REQUIRED"
    assert run["error"] == "CAPTCHA_REQUIRED"


def test_discovery_timeout_is_recorded_without_crashing_scan(cur):
    class TimeoutConnector(FixtureConnector):
        def discover(self, since=None):
            raise TimeoutError("The read operation timed out")

    summary = scan_source(cur, TimeoutConnector(FakeClient({}), []), storage_root="data/test-acquired")
    assert summary.status == "FAILED"
    assert summary.error == "The read operation timed out"
    run = cur.execute("SELECT * FROM acquisition_runs WHERE id = ?", (summary.run_id,)).fetchone()
    assert run["status"] == "FAILED"
    assert run["error"] == "The read operation timed out"


def test_records_are_processed_even_when_discovery_also_reports_a_block(cur, tmp_path):
    document_url = "https://fixture.example/docs/partial.txt"
    body = b"CCTV AMC for XYZ Hospital. AMC for a period of 1 year."
    client = FakeClient({document_url: HttpResponse(document_url, 200, {"Content-Type": "text/plain"}, body)})
    record = TenderRecord(
        source_id="fixture", external_id="T-002", title="CCTV AMC Services",
        published_at="2026-09-20", source_page_url="https://fixture.example/list",
        document_urls=[DocumentCandidate(document_url, "partial.txt")],
    )

    class PartiallyBlockedConnector(FixtureConnector):
        def discover(self, since=None):
            return DiscoveryResult(records=[record], manual_action_required="CAPTCHA_REQUIRED")

    summary = scan_source(cur, PartiallyBlockedConnector(client, [record]), client=client, storage_root=str(tmp_path))
    assert summary.status == "PARTIAL"
    assert summary.records_seen == 1
    assert summary.documents_processed == 1
    assert summary.manual_action_required == 1


def test_download_rejects_html_error_page():
    url = "https://fixture.example/blocked.pdf"
    client = FakeClient({url: HttpResponse(url, 200, {"Content-Type": "text/html"}, b"<html>Access denied</html>")})
    try:
        download_document(client, DocumentCandidate(url, "blocked.pdf"), storage_root="data/test-acquired", source_id="fixture")
    except DocumentDownloadError as exc:
        assert exc.code == "HTML_ERROR_PAGE"
    else:
        raise AssertionError("HTML error pages must never be treated as downloaded documents")


def test_download_infers_pdf_type_when_cppp_omits_content_type(tmp_path):
    url = "https://fixture.example/document-download"
    client = FakeClient({url: HttpResponse(url, 200, {}, b"%PDF-1.7\nfixture")})
    outcome = download_document(
        client, DocumentCandidate(url, "Tendernotice_1.pdf"),
        storage_root=str(tmp_path), source_id="fixture",
    )
    assert outcome["filename"] == "Tendernotice_1.pdf"
    assert outcome["path"].endswith(".pdf")


def test_download_infers_zip_type_from_cppp_bytes(tmp_path):
    url = "https://fixture.example/download-zip"
    client = FakeClient({url: HttpResponse(url, 200, {}, b"PK\x03\x04fixture")})
    outcome = download_document(
        client, DocumentCandidate(url, "Download as zip file"),
        storage_root=str(tmp_path), source_id="fixture",
    )
    assert outcome["filename"].endswith(".zip")
    assert outcome["path"].endswith(".zip")
