"""
Contract Expiry Radar — Streamlit application.

Run with:  streamlit run app.py
"""
from __future__ import annotations

import csv
import io
import os
import sys

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import database as db
from core import pipeline
from core.proof_packet import build_proof_packet_text
from acquisition.config import settings as acquisition_settings
from acquisition.http import PublicHttpClient
from acquisition.registry import get_connector
from acquisition.scanner import scan_source

st.set_page_config(page_title="Contract Expiry Radar", layout="wide")

db.init_db()  # idempotent — creates tables if they don't exist


def get_cursor():
    conn = db.get_connection()
    return conn.cursor(), conn


STATUS_COLORS = {
    "HIGH_CONFIDENCE": "🟢",
    "VERIFIED": "🟢",
    "EXTENDED": "🟢",
    "NEEDS_REVIEW": "🟡",
    "CONFLICTING_EVIDENCE": "🔴",
    "INSUFFICIENT_EVIDENCE": "⚪",
    "UNKNOWN": "⚪",
    "EXPIRED": "🔴",
    "EXPIRED_UNVERIFIED": "🟠",
    "CANCELLED": "⚫",
}

DOC_TYPES = [
    "nit", "bid_document", "corrigendum", "aoc", "loa", "work_order",
    "signed_agreement", "site_handover_letter", "sla_stc",
    "platform_amendment", "termination_notice", "department_page", "other",
]


# ---------------------------------------------------------------------
# Sidebar navigation
# ---------------------------------------------------------------------
st.sidebar.title("📡 Contract Expiry Radar")
page = st.sidebar.radio(
    "Navigate",
    ["Dashboard", "Automated Monitoring", "Upload Document", "Contract Search", "Review Queue", "Requirements & Cycles"],
    label_visibility="collapsed",
)

if "selected_contract_id" not in st.session_state:
    st.session_state.selected_contract_id = None


def select_contract(contract_id: int):
    st.session_state.selected_contract_id = contract_id


# ---------------------------------------------------------------------
# Contract detail (shared across pages once a contract is selected)
# ---------------------------------------------------------------------
def render_contract_detail(cur, contract_id: int):
    contract = db.get_contract(cur, contract_id)
    if not contract:
        st.error("Contract not found.")
        return

    org = cur.execute("SELECT name FROM organisations WHERE id = ?", (contract["org_id"],)).fetchone()
    vendor = cur.execute("SELECT name FROM vendors WHERE id = ?", (contract["vendor_id"],)).fetchone()

    st.markdown(f"### {STATUS_COLORS.get(contract['status'], '⚪')} {contract['title']}")
    cols = st.columns(4)
    cols[0].metric("Status", contract["status"])
    cols[1].metric("Current Expiry Estimate", contract["current_expiry_estimate"] or "UNKNOWN")
    cols[2].metric("Expiry Ceiling", contract["current_expiry_ceiling"] or "UNKNOWN")
    conf = db.get_latest_confidence(cur, contract_id)
    cols[3].metric("Expiry Confidence", conf["expiry_confidence"] if conf else "not assessed")

    st.caption(f"Organisation: {org['name'] if org else 'Unknown'}  |  Vendor: {vendor['name'] if vendor else 'Unknown'}")

    tabs = st.tabs(["Proof Packet", "Evidence", "Timeline", "Documents", "Confidence"])

    with tabs[0]:
        packet_text = build_proof_packet_text(cur, contract_id)
        st.code(packet_text, language=None)
        st.download_button(
            "⬇ Download Proof Packet (.txt)", data=packet_text,
            file_name=f"proof_packet_contract_{contract_id}.txt",
        )

    with tabs[1]:
        facts = db.get_facts_for_contract(cur, contract_id, include_superseded=True)
        if not facts:
            st.info("No facts extracted yet.")
        for f in facts:
            superseded_tag = " (SUPERSEDED)" if f["superseded_by_fact_id"] else ""
            with st.expander(f"{f['fact_type']}{superseded_tag}: {f['fact_value']}  [{f['fact_confidence']}]"):
                st.write(f"**Evidence quote:** \"{f['evidence_quote']}\"")
                doc = db.get_document(cur, f["document_id"])
                if doc:
                    st.write(f"**Source:** {doc['doc_type']} — {doc['doc_title'] or doc['source_url'] or 'uploaded document'}")
                    if doc["page_number"]:
                        st.write(f"**Page:** {doc['page_number']}")
                st.write(f"**Extraction rule:** {f['extraction_rule_id']}")
        open_conflicts = db.list_open_conflicts(cur, contract_id)
        if open_conflicts:
            st.warning(f"{len(open_conflicts)} open conflict(s) — see Review Queue.")

    with tabs[2]:
        events = db.list_timeline_for_contract(cur, contract_id)
        if not events:
            st.info("No timeline events yet.")
        for e in events:
            st.write(f"**{e['event_type']}** — {e['event_date'] or e['observed_at']}")
            if e["narrative"]:
                st.caption(e["narrative"])

    with tabs[3]:
        documents = db.list_documents_for_contract(cur, contract_id)
        for d in documents:
            with st.expander(f"{d['doc_type']} — {d['doc_title'] or d['source_url'] or 'uploaded document'} ({d['doc_date'] or 'no date'})"):
                st.write(f"Subtype: {d['doc_subtype'] or '—'}")
                st.write(f"Source URL: {d['source_url'] or '—'}")
                st.text_area("Raw text", d["raw_text"], height=150, key=f"rawtext_{d['id']}")

    with tabs[4]:
        if conf:
            st.write(f"Award-date confidence: **{conf['award_date_confidence']}**")
            st.write(f"Duration confidence: **{conf['duration_confidence']}**")
            st.write(f"Start-date confidence: **{conf['start_date_confidence']}**")
            st.write(f"Source reliability: **{conf['source_reliability']}**")
            st.write(f"Cross-source verified: **{'YES' if conf['cross_source_verified'] else 'NO'}** — {conf['cross_source_detail']}")
            st.write(f"→ Expiry confidence: **{conf['expiry_confidence']}**")
            st.write(f"Limiting factor: {conf['limiting_factor']}")
        else:
            st.info("Not yet assessed.")

    st.divider()
    if st.button("✅ Mark as human-verified"):
        cur.execute("UPDATE contracts SET last_human_verified_at = datetime('now') WHERE id = ?", (contract_id,))
        cur.connection.commit()
        st.success("Marked as human-verified.")
        st.rerun()


# ---------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------
if page == "Dashboard":
    cur, conn = get_cursor()
    contracts = db.list_contracts(cur)

    st.title("Dashboard")
    status_counts = {}
    for c in contracts:
        status_counts[c["status"]] = status_counts.get(c["status"], 0) + 1

    cols = st.columns(max(len(status_counts), 1))
    for i, (status, count) in enumerate(sorted(status_counts.items())):
        cols[i % len(cols)].metric(f"{STATUS_COLORS.get(status,'')} {status}", count)

    st.subheader(f"All contracts ({len(contracts)})")
    if not contracts:
        st.info("No contracts ingested yet. Go to 'Upload Document' to add your first document.")
    for c in contracts:
        col1, col2, col3, col4 = st.columns([4, 2, 2, 1])
        col1.write(f"{STATUS_COLORS.get(c['status'],'')} **{c['title']}**")
        col2.write(f"Expiry: {c['current_expiry_estimate'] or 'UNKNOWN'}")
        col3.write(c["status"])
        if col4.button("View", key=f"view_{c['id']}"):
            select_contract(c["id"])
            st.rerun()

    st.subheader("Discovered tenders")
    st.caption(
        "Metadata discovered from monitored public sources. These are not counted as contracts until a document is acquired and analyzed."
    )
    discovered_tenders = db.list_discovered_records(cur, limit=50)
    if discovered_tenders:
        st.dataframe([
            {
                "Tender": tender["title"] or tender["external_id"],
                "Reference": tender["external_id"],
                "Organisation": tender["organisation"] or "not exposed",
                "Location": tender["location"] or "not exposed",
                "Published": tender["published_at"] or "not exposed",
                "Closing": tender["closing_at"] or "unknown",
                "Tender value": tender["estimated_value"] or "not exposed",
                "Source": tender["source_name"],
                "Status": tender["acquisition_status"],
                "Detail URL": tender["detail_url"] or "not exposed",
            }
            for tender in discovered_tenders
        ], use_container_width=True)
    else:
        st.info("No automated tender discoveries yet. Run a source scan from Automated Monitoring.")

    if st.session_state.selected_contract_id:
        st.divider()
        render_contract_detail(cur, st.session_state.selected_contract_id)


# ---------------------------------------------------------------------
# Automated Monitoring
# ---------------------------------------------------------------------
elif page == "Automated Monitoring":
    cur, conn = get_cursor()
    st.title("Automated Monitoring")
    config = acquisition_settings()
    st.caption("Public-source monitoring persists discoveries and provenance. CAPTCHA, authentication, and blocked documents are shown for manual action.")

    col1, col2 = st.columns([3, 1])
    col1.metric("Monitoring", "ON" if config["enabled"] else "OFF")
    if col2.button("Run CPPP scan now", type="primary", disabled=not config["enabled"]):
        client = PublicHttpClient(timeout=config["request_timeout"], max_retries=config["max_retries"], rate_limit_delay=config["rate_limit_delay"])
        try:
            summary = scan_source(cur, get_connector("cppp", client=client), client=client, storage_root=config["storage_root"])
            conn.commit()
            message = (
                f"Scan {summary.status}: {summary.records_seen} seen, "
                f"{summary.records_new} new, {summary.documents_processed} processed."
            )
            if summary.status == "FAILED":
                st.error(f"{message} Run {summary.run_id}. Error: {summary.error or 'unknown error'}")
            elif summary.status == "PARTIAL":
                st.warning(message)
            else:
                st.success(message)
            if summary.manual_action_required:
                st.warning(f"{summary.manual_action_required} item(s) require manual action.")
        except Exception as exc:
            conn.commit()
            st.error(f"Scan failed: {exc}")

    sources = db.list_acquisition_sources(cur)
    st.subheader("Sources")
    if sources:
        st.dataframe([
            {"Source": source["name"], "Status": source["status"], "Last scan": source["last_scan_at"] or "never", "Last success": source["last_success_at"] or "never"}
            for source in sources
        ], use_container_width=True)
    else:
        st.info("No source has been scanned yet. The background worker can populate this view.")

    st.subheader("Manual action required")
    manual_records = db.list_discovered_records(cur, acquisition_status="MANUAL_ACTION_REQUIRED")
    if manual_records:
        st.dataframe([
            {"Tender": record["title"] or record["external_id"], "Organisation": record["organisation"] or "unknown", "Reason": record["last_error"] or "manual retrieval required", "Detail": record["detail_url"] or "not exposed"}
            for record in manual_records
        ], use_container_width=True)
    else:
        st.info("No manual-action records.")

    st.subheader("Recent discoveries")
    records = db.list_discovered_records(cur, limit=50)
    if records:
        st.dataframe([
            {"Tender": record["title"] or record["external_id"], "Organisation": record["organisation"] or "unknown", "Location": record["location"] or "not exposed", "Published": record["published_at"] or "unknown", "Closing": record["closing_at"] or "unknown", "Tender value": record["estimated_value"] or "not exposed", "Source": record["source_name"], "Status": record["acquisition_status"]}
            for record in records
        ], use_container_width=True)
    else:
        st.info("No discoveries recorded yet.")

    st.subheader("Recent scan runs")
    runs = db.list_acquisition_runs(cur)
    if runs:
        st.dataframe([
            {"Source": run["source_name"], "Started": run["started_at"], "Status": run["status"], "Seen": run["records_seen"], "New": run["records_new"], "Downloaded": run["documents_downloaded"], "Processed": run["documents_processed"], "Manual": run["manual_action_required"], "Failed": run["documents_failed"], "Error": run["error"] or ""}
            for run in runs
        ], use_container_width=True)
    else:
        st.info("No scan runs recorded yet.")

    st.subheader("Opportunity radar")
    predictions = db.list_predictions(cur, status="ACTIVE")
    if predictions:
        st.dataframe([
            {"Requirement": prediction["normalized_title"], "Anchor": prediction["anchor_event_type"], "Expected": prediction["predicted_date"], "Window": f"{prediction['window_start']} to {prediction['window_end']}", "Confidence": prediction["confidence"]}
            for prediction in predictions
        ], use_container_width=True)
    else:
        st.info("No prediction window is available yet. At least two dated historical cycles are required.")


# ---------------------------------------------------------------------
# Upload Document
# ---------------------------------------------------------------------
elif page == "Upload Document":
    cur, conn = get_cursor()
    st.title("Document Upload / Ingestion")

    contracts = db.list_contracts(cur)
    contract_titles = {c["title"]: c["id"] for c in contracts}

    mode = st.radio("Link this document to", ["New contract", "Existing contract"], horizontal=True)
    contract_id = None
    contract_title = None
    if mode == "Existing contract" and contract_titles:
        chosen = st.selectbox("Choose contract", list(contract_titles.keys()))
        contract_id = contract_titles[chosen]
    else:
        contract_title = st.text_input("New contract title", placeholder="e.g. AMC of Fire Fighting System, Terminal Building")

    doc_type = st.selectbox("Document type", DOC_TYPES)
    doc_title = st.text_input("Document title (optional)")
    source_url = st.text_input("Source URL (optional)")
    doc_date = st.text_input("Document date, ISO format (optional, e.g. 2025-03-14)")

    input_mode = st.radio("Input method", ["Upload file (PDF/TXT)", "Paste text"], horizontal=True)

    raw_bytes, filename, raw_text = None, None, None
    if input_mode == "Upload file (PDF/TXT)":
        uploaded = st.file_uploader("Upload PDF or TXT", type=["pdf", "txt"])
        if uploaded:
            raw_bytes = uploaded.read()
            filename = uploaded.name
    else:
        raw_text = st.text_area("Paste document text", height=200)

    if st.button("Ingest Document", type="primary"):
        try:
            result = pipeline.ingest_document(
                cur, raw_bytes=raw_bytes, raw_text=raw_text, filename=filename,
                doc_type=doc_type, contract_id=contract_id, contract_title=contract_title,
                source_url=source_url or None, doc_title=doc_title or None,
                doc_date=doc_date or None,
            )
            conn.commit()
            st.success(
                f"Ingested. {result.n_facts_extracted} fact(s) extracted. "
                f"{'(deduplicated — already existed)' if result.deduplicated else ''} "
                f"Contract status: {result.status}, expiry confidence: {result.expiry_confidence}."
            )
            if result.reconciliation_summary["conflicts"]:
                st.warning(f"{len(result.reconciliation_summary['conflicts'])} conflict(s) detected — see Review Queue.")
            if result.similar_contracts_warning:
                names = ", ".join(f"\"{m['title']}\" ({m['similarity']:.0%} similar)" for m in result.similar_contracts_warning)
                st.warning(
                    f"This looks similar to existing contract(s): {names}. "
                    "If this is the same underlying contract, consider re-ingesting this "
                    "document under 'Existing contract' instead to avoid a duplicate record."
                )
            select_contract(result.contract_id)
        except ValueError as e:
            st.error(str(e))

    if st.session_state.selected_contract_id:
        st.divider()
        render_contract_detail(cur, st.session_state.selected_contract_id)


# ---------------------------------------------------------------------
# Contract Search
# ---------------------------------------------------------------------
elif page == "Contract Search":
    cur, conn = get_cursor()
    st.title("Contract Search")

    query = st.text_input("Search by contract title, organisation, or vendor")
    results = db.search_contracts(cur, query) if query else db.list_contracts(cur)

    st.write(f"{len(results)} result(s)")
    for c in results:
        col1, col2, col3 = st.columns([5, 2, 1])
        col1.write(f"{STATUS_COLORS.get(c['status'],'')} **{c['title']}**")
        col2.write(c["current_expiry_estimate"] or "UNKNOWN")
        if col3.button("View", key=f"search_view_{c['id']}"):
            select_contract(c["id"])
            st.rerun()

    st.divider()
    st.subheader("Export")
    all_contracts = db.list_contracts(cur)
    if st.button("Prepare CSV export of all contracts"):
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "id", "title", "status", "current_expiry_estimate", "current_expiry_ceiling",
            "expiry_confidence", "last_human_verified_at",
        ])
        for c in all_contracts:
            conf = db.get_latest_confidence(cur, c["id"])
            writer.writerow([
                c["id"], c["title"], c["status"], c["current_expiry_estimate"],
                c["current_expiry_ceiling"], conf["expiry_confidence"] if conf else "",
                c["last_human_verified_at"] or "",
            ])
        st.download_button("⬇ Download contracts.csv", data=buf.getvalue(),
                            file_name="contracts_export.csv", mime="text/csv")

    if st.session_state.selected_contract_id:
        st.divider()
        render_contract_detail(cur, st.session_state.selected_contract_id)


# ---------------------------------------------------------------------
# Review Queue
# ---------------------------------------------------------------------
elif page == "Review Queue":
    cur, conn = get_cursor()
    st.title("Review Queue")
    st.caption("Contracts needing human attention: conflicting evidence, low confidence selected for a paid report, "
               "or otherwise flagged per spec Part 8's targeted-review triggers.")

    queue = db.list_review_queue(cur)
    if not queue:
        st.success("Nothing in the review queue.")
    for c in queue:
        col1, col2, col3 = st.columns([5, 2, 1])
        col1.write(f"{STATUS_COLORS.get(c['status'],'')} **{c['title']}** — {c['status']}")
        open_conflicts = db.list_open_conflicts(cur, c["id"])
        col2.write(f"{len(open_conflicts)} open conflict(s)")
        if col3.button("Review", key=f"review_{c['id']}"):
            select_contract(c["id"])
            st.rerun()

    st.subheader("Open fact conflicts")
    conflicts = db.list_open_conflicts(cur)
    for conf in conflicts:
        fact_a = cur.execute("SELECT * FROM extracted_facts WHERE id = ?", (conf["fact_id_a"],)).fetchone()
        fact_b = cur.execute("SELECT * FROM extracted_facts WHERE id = ?", (conf["fact_id_b"],)).fetchone()
        with st.expander(f"Conflict on contract #{conf['contract_id']} — {conf['fact_type']}"):
            colA, colB = st.columns(2)
            colA.write(f"**Value A:** {fact_a['fact_value']}")
            colA.write(f"Evidence: \"{fact_a['evidence_quote']}\"")
            colB.write(f"**Value B:** {fact_b['fact_value']}")
            colB.write(f"Evidence: \"{fact_b['evidence_quote']}\"")
            note = st.text_input("Resolution note", key=f"resolve_note_{conf['id']}")
            if st.button("Mark resolved", key=f"resolve_btn_{conf['id']}"):
                db.resolve_conflict(cur, conf["id"], note or "Resolved by reviewer")
                conn.commit()
                st.rerun()

    if st.session_state.selected_contract_id:
        st.divider()
        render_contract_detail(cur, st.session_state.selected_contract_id)


# ---------------------------------------------------------------------
# Requirements & Cycles (Layer 2)
# ---------------------------------------------------------------------
elif page == "Requirements & Cycles":
    cur, conn = get_cursor()
    st.title("Requirements & Procurement Cycles")
    st.caption(
        "A requirement is the underlying recurring need (e.g. \"CCTV AMC — XYZ Hospital\") that "
        "survives changes in tender title, vendor, or wording across multiple contracts over time. "
        "This page is descriptive history only — it does not predict future procurement."
    )

    requirements = db.list_requirements(cur)
    if not requirements:
        st.info("No requirements identified yet. Requirements are detected automatically as documents "
                 "are ingested, when their title/organisation matches a known asset/service category.")

    for r in requirements:
        org = cur.execute("SELECT name FROM organisations WHERE id = ?", (r["org_id"],)).fetchone()
        aliases = db.list_aliases_for_requirement(cur, r["id"])
        cycle = db.get_latest_cycle(cur, r["id"])
        events = db.list_events_for_requirement(cur, r["id"])

        with st.expander(
            f"**{r['asset_keyword']}** — {org['name'] if org else 'organisation unresolved'}"
            f"{' (' + r['location'] + ')' if r['location'] else ''}  ·  {len(aliases)} alias(es)  ·  "
            f"{len(events)} event(s)"
        ):
            st.write("**Known titles (aliases) for this requirement:**")
            for a in aliases:
                st.write(f"- {a['alias_text']}")

            st.write("**Procurement events:**")
            for e in sorted(events, key=lambda e: e["event_date"] or ""):
                st.write(f"- {e['event_date'] or 'undated'} — {e['event_type']}")

            st.write("**Historical cycle statistics:**")
            if cycle is None or cycle["n_cycles"] == 0:
                st.write("No cycle data yet.")
            elif cycle["median_interval_days"] is None:
                st.write(
                    f"INSUFFICIENT_DATA — {cycle['n_cycles']} observation(s) of "
                    f"'{cycle['anchor_event_type'] or 'no anchorable event type'}'. "
                    "At least two dated occurrences of the same event type are needed to measure "
                    "an interval at all."
                )
            else:
                import json as _json
                intervals = _json.loads(cycle["interval_days"])
                cols = st.columns(5)
                cols[0].metric("Cycles observed", cycle["n_cycles"])
                cols[1].metric("Median interval (days)", round(cycle["median_interval_days"]))
                cols[2].metric("Mean interval (days)", round(cycle["mean_interval_days"]))
                cols[3].metric("Min / Max (days)", f"{cycle['min_interval_days']:.0f} / {cycle['max_interval_days']:.0f}")
                cols[4].metric("Std. dev (days)", round(cycle["stddev_interval_days"], 1))
                st.caption(f"Anchor event type: {cycle['anchor_event_type']}  |  Intervals measured: {intervals}")

            st.caption(
                "Every event above is linked to this requirement with a stated match status and "
                "evidence (organisation, asset, location, title similarity) — see requirement_event_links "
                "in the database for the full audit trail of any linkage."
            )
