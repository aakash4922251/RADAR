"""
Contract Expiry Radar — Streamlit application.

Run with:  streamlit run app.py
"""
from __future__ import annotations

import csv
import io
import os
import sys
from pathlib import Path
from datetime import datetime

import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import database as db
from core import pipeline
from core.procurement_intelligence import process_bytes, process_zip, process_directory, fetch_and_process_url, init_intelligence_tables
from core.dataset_import import DEFAULT_REPO, DatasetAsset, inspect_github_dataset, download_asset, register_imported_dataset, query_imported_dataset, duckdb_available, find_historical_aoc_root
from core.proof_packet import build_proof_packet_text
from acquisition.config import settings as acquisition_settings
from acquisition.http import PublicHttpClient
from acquisition.registry import get_connector
from acquisition.scanner import scan_source
from radar_ui import (
    buyer_display_name,
    contract_status_label,
    greeting_for_hour,
    human_age,
    opportunity_buckets,
    timestamp_is_recent,
    render_alerts,
    render_analytics,
    render_backtesting,
    render_documents,
    render_evidence,
    render_opportunities,
    render_organisations,
    render_settings,
    render_sources,
    render_tenders,
)
from ui_theme import apply_theme

st.set_page_config(page_title="Government Procurement Opportunity Radar", page_icon=":material/radar:", layout="wide", initial_sidebar_state="expanded")

db.init_db()  # idempotent — creates tables if they don't exist
apply_theme()


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
PRIMARY_NAV = [
    ("Home", "Home", ":material/home:"),
    ("Radar", "Radar", ":material/radar:"),
    ("Alerts", "Alerts", ":material/notifications:"),
    ("Search", "Search", ":material/search:"),
    ("Monitoring", "Monitoring", ":material/sensors:"),
    ("Data Center", "Data Center", ":material/cloud_upload:"),
]

MORE_NAV = [
    ("Tenders", "Tenders", ":material/description:"),
    ("Procurement cycles", "Procurement Cycles", ":material/cycle:"),
    ("Documents", "Documents", ":material/folder_open:"),
    ("Organisations", "Organisations", ":material/apartment:"),
    ("Evidence", "Evidence", ":material/fact_check:"),
    ("Analytics", "Analytics", ":material/monitoring:"),
    ("Backtesting", "Backtesting", ":material/science:"),
    ("Sources", "Sources", ":material/source:"),
    ("Settings", "Settings", ":material/settings:"),
    ("Upload contract evidence", "Upload Document", ":material/upload_file:"),
    ("Contract search", "Contract Search", ":material/search:"),
    ("Review queue", "Review Queue", ":material/rule:"),
]


def set_active_page(name: str):
    st.session_state.active_page = name
    if name == "Radar":
        st.session_state.selected_requirement_id = None


if "active_page" not in st.session_state:
    st.session_state.active_page = "Home"
elif st.session_state.active_page == "Overview":
    st.session_state.active_page = "Home"
elif st.session_state.active_page == "Opportunities":
    st.session_state.active_page = "Radar"
elif st.session_state.active_page == "Live Monitor":
    st.session_state.active_page = "Monitoring"

st.sidebar.markdown(
    '<div class="radar-brand">OPPORTUNITY RADAR</div>'
    '<div class="radar-brand-sub">Government procurement opportunities, made clear</div>',
    unsafe_allow_html=True,
)
for label, destination, icon in PRIMARY_NAV:
    st.sidebar.button(
        label,
        key=f"primary_nav_{destination}",
        icon=icon,
        type="primary" if st.session_state.active_page == destination else "secondary",
        use_container_width=True,
        on_click=set_active_page,
        args=(destination,),
    )

with st.sidebar.expander("More", expanded=st.session_state.active_page not in {item[1] for item in PRIMARY_NAV}):
    for label, destination, icon in MORE_NAV:
        st.button(
            label,
            key=f"more_nav_{destination}",
            icon=icon,
            type="primary" if st.session_state.active_page == destination else "secondary",
            use_container_width=True,
            on_click=set_active_page,
            args=(destination,),
        )

page = st.session_state.active_page


def historical_root_path():
    return Path(os.path.dirname(os.path.abspath(__file__))) / "data" / "historical"


def get_radar_rows(cur, limit=None):
    query = """
        SELECT pp.*, r.asset_keyword, r.normalized_title, r.location, o.name AS org_name,
               COALESCE((SELECT alias_text FROM requirement_aliases a
                 WHERE a.requirement_id = r.id
                 ORDER BY a.created_at DESC, a.id DESC LIMIT 1), r.normalized_title) AS display_title
        FROM procurement_predictions pp
        JOIN requirements r ON r.id = pp.requirement_id
        LEFT JOIN organisations o ON o.id = r.org_id
        WHERE pp.status = 'ACTIVE'
        ORDER BY CASE pp.confidence WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END,
             pp.predicted_date ASC
    """
    if limit is not None:
        query += " LIMIT ?"
        return cur.execute(query, (limit,)).fetchall()
    return cur.execute(query).fetchall()


def set_page_and_rerun(name: str):
    st.session_state.active_page = name
    if name == "Radar":
        st.session_state.selected_requirement_id = None
    st.rerun()

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
# Overview
# ---------------------------------------------------------------------
if page == "Home":
    cur, conn = get_cursor()
    opportunities = get_radar_rows(cur)
    tender_count = cur.execute("SELECT COUNT(*) FROM discovered_records").fetchone()[0]
    coming_soon = sum("Coming Soon" in opportunity_buckets(item) for item in opportunities)
    config = acquisition_settings()
    sources = db.list_acquisition_sources(cur)
    cppp_source = next((source for source in sources if source["source_key"] == "cppp"), None)
    last_scan = cppp_source["last_scan_at"] if cppp_source else None
    monitoring_enabled = config["enabled"] and config["cppp_enabled"]
    user_name = os.environ.get("RADAR_USER_NAME", "there").strip() or "there"

    st.markdown(f'<div class="welcome-title">{greeting_for_hour(datetime.now().astimezone().hour, user_name)} 👋</div>', unsafe_allow_html=True)
    st.markdown('<div class="welcome-copy">Your procurement radar is watching.</div>', unsafe_allow_html=True)
    status_class = "status-pill" if monitoring_enabled else "status-pill paused"
    status_label = "Monitoring CPPP" if monitoring_enabled else "Monitoring paused"
    st.markdown(f'<span class="{status_class}"><span class="status-dot"></span>{status_label}</span>', unsafe_allow_html=True)
    st.markdown(f'<span class="live-note">{human_age(last_scan)}</span>', unsafe_allow_html=True)

    st.write("")
    metrics = st.columns(3)
    metrics[0].metric("Opportunities", len(opportunities))
    metrics[1].metric("Coming soon", coming_soon)
    metrics[2].metric("Tenders tracked", tender_count)

    latest_run = db.list_acquisition_runs(cur, limit=1)
    if latest_run and latest_run[0]["records_new"] and timestamp_is_recent(latest_run[0]["finished_at"]):
        st.success(f"New tender notices found in the latest check: {latest_run[0]['records_new']}.")

    st.write("")
    home_actions = st.columns([1, 1, 3])
    if home_actions[0].button("Explore Radar", type="primary", icon=":material/radar:", use_container_width=True):
        set_page_and_rerun("Radar")
    if home_actions[1].button("Latest activity", icon=":material/history:", use_container_width=True):
        set_page_and_rerun("Monitoring")


# ---------------------------------------------------------------------
# Procurement Data Center
# ---------------------------------------------------------------------
elif page == "Data Center":
    cur, conn = get_cursor()
    init_intelligence_tables(conn)
    st.markdown('<div class="eyebrow">Automatic document intelligence</div>', unsafe_allow_html=True)
    st.title("Procurement Data Center")
    st.caption("Drop a ZIP, multiple PDFs, a local inbox, or a permitted document URL. Radar handles hashing, classification, extraction and evidence automatically.")

    tabs = st.tabs(["Send to Radar", "Historical Data", "Local Inbox", "Processed Documents"])
    with tabs[0]:
        url = st.text_input("Document or tender-page URL", placeholder="https://eprocure.gov.in/...")
        if st.button("Send URL to Radar", type="primary", disabled=not url.strip()):
            with st.spinner("Reading permitted public documents and discovering linked files..."):
                results = fetch_and_process_url(conn, url.strip())
            ok=sum(r.status=="PROCESSED" for r in results); dup=sum(r.status=="DUPLICATE" for r in results); fail=sum(r.status=="FAILED" for r in results)
            st.success(f"Processed {ok} · skipped {dup} duplicates · {fail} failed")
            for r in results[:100]:
                st.write(f"**{r.document_type}** — {r.filename} — {len(r.facts)} facts")
                if r.error: st.caption(r.error)

        uploads = st.file_uploader("Or upload PDFs / Office files / ZIPs", type=["pdf","txt","docx","doc","xlsx","xls","csv","zip"], accept_multiple_files=True)
        if uploads and st.button("Process uploaded files", type="primary"):
            results=[]
            progress=st.progress(0.0)
            for i,u in enumerate(uploads,1):
                data=u.getvalue()
                if u.name.lower().endswith(".zip"):
                    results.extend(process_zip(conn,data,u.name))
                else:
                    results.append(process_bytes(conn,data,u.name))
                progress.progress(i/len(uploads))
            ok=sum(r.status=="PROCESSED" for r in results); dup=sum(r.status=="DUPLICATE" for r in results); fail=sum(r.status=="FAILED" for r in results)
            st.success(f"Processed {ok} · skipped {dup} duplicates · {fail} failed")
            st.dataframe([{"File":r.filename,"Type":r.document_type,"Status":r.status,"Facts":len(r.facts)} for r in results], use_container_width=True, hide_index=True)

    with tabs[1]:
        st.subheader("Add historical procurement data")
        st.caption("Paste a public GitHub dataset/repository URL. Radar will inspect its release assets and download only the years/tables you choose. Large historical data stays in DuckDB/Parquet, not the main SQLite database.")
        source_url = st.text_input("Historical dataset / GitHub URL", value=DEFAULT_REPO, key="historical_dataset_url")
        if "historical_dataset_info" not in st.session_state:
            st.session_state.historical_dataset_info = None
        if st.button("Detect dataset", type="primary", key="detect_historical_dataset"):
            try:
                with st.spinner("Inspecting the public dataset..."):
                    st.session_state.historical_dataset_info = inspect_github_dataset(source_url.strip())
                st.success("Dataset detected.")
            except Exception as exc:
                st.session_state.historical_dataset_info = None
                st.error(str(exc))

        info = st.session_state.historical_dataset_info
        if info:
            st.markdown(f"**Source:** `{info['repo']}`  ")
            st.markdown(f"**Release:** `{info.get('release')}` · **Assets found:** {len(info['assets'])}")
            assets = info["assets"]
            if not assets:
                st.warning("No Parquet release assets were found. This source may require a different import adapter.")
            else:
                # Keep selection understandable: table + year + size, not 88 checkboxes.
                options = []
                for a in assets:
                    options.append((f"{a.table or 'unknown'} / {a.year or 'unparsed'} · {a.size_mb:.1f} MB", a))
                labels = [x[0] for x in options]
                # Default to the latest 3 AOC years actually present in the release.
                # The old UI hard-coded 2024+2025, which made a 3-year import look like
                # a 2-year dataset even when the user expected another year.
                aoc_years = sorted({str(x[1].year) for x in options if x[1].table == "aoc_tenders" and x[1].year}, reverse=True)
                default_aoc_years = set(aoc_years[:3])
                default_labels = [x[0] for x in options if x[1].table == "aoc_tenders" and str(x[1].year) in default_aoc_years]
                selected_labels = st.multiselect("Choose historical partitions to download", labels, default=default_labels)
                selected = [a for label, a in options if label in selected_labels]
                current_root = Path(os.path.dirname(os.path.abspath(__file__))) / "data" / "historical" / info["repo"].replace("/", "__")
                existing_aoc_years = sorted({p.parent.name.split("=", 1)[1] for p in (current_root / "aoc_tenders").glob("year=*/data.parquet") if "=" in p.parent.name}) if (current_root / "aoc_tenders").exists() else []
                if existing_aoc_years:
                    st.success(f"Already imported AOC partitions: {', '.join(existing_aoc_years)}")
                elif aoc_years:
                    st.info(f"No AOC partitions imported yet. Release contains AOC years including: {', '.join(aoc_years[:8])}{' …' if len(aoc_years) > 8 else ''}")
                total_mb = sum(a.size_mb for a in selected)
                st.info(f"Selected: {len(selected)} files · approximately {total_mb:.1f} MB")
                if selected and st.button("Download and index selected data", type="primary", key="download_historical_data"):
                    root = Path(os.path.dirname(os.path.abspath(__file__))) / "data" / "historical" / info["repo"].replace("/", "__")
                    root.mkdir(parents=True, exist_ok=True)
                    progress = st.progress(0.0)
                    status = st.empty()
                    completed = 0
                    imported = []
                    for asset in selected:
                        table = asset.table or "unparsed"
                        year = asset.year or "unparsed"
                        destination = root / table / f"year={year}" / "data.parquet"
                        status.write(f"Downloading `{asset.name}` …")
                        def _progress(done, total, base=completed, count=len(selected)):
                            frac = (base + (done / total if total else 0.0)) / count
                            progress.progress(min(frac, 1.0))
                        try:
                            download_asset(asset, destination, _progress)
                            imported.append(asset)
                        except Exception as exc:
                            st.error(f"{asset.name}: {exc}")
                        completed += 1
                    register_imported_dataset(root, info, imported)
                    progress.progress(1.0)
                    imported_years_now = sorted({str(a.year) for a in imported if a.table == "aoc_tenders" and a.year})
                    if imported_years_now:
                        st.success(f"Imported {len(imported)} of {len(selected)} selected partitions. AOC years imported in this run: {', '.join(imported_years_now)}. SHA-256 is verified when the source manifest provides it.")
                    else:
                        st.success(f"Imported {len(imported)} of {len(selected)} selected partitions. SHA-256 is verified when the source manifest provides it.")

                if duckdb_available() is None:
                    st.warning("DuckDB is not installed in this environment. Install dependencies with `python -m pip install -r requirements.txt` to query the imported historical data.")
                else:
                    root = Path(os.path.dirname(os.path.abspath(__file__))) / "data" / "historical" / info["repo"].replace("/", "__")
                    if root.exists():
                        st.subheader("Quick historical query")
                        q = st.text_area("SQL", value="SELECT COUNT(*) AS records FROM aoc_tenders", height=100, key="historical_sql")
                        if st.button("Run query", key="run_historical_sql"):
                            try:
                                df = query_imported_dataset(root, q)
                                st.dataframe(df, use_container_width=True, hide_index=True)
                            except Exception as exc:
                                st.error(str(exc))

    with tabs[2]:
        inbox=os.environ.get("RADAR_INBOX", os.path.join(os.path.dirname(os.path.abspath(__file__)),"data","inbox"))
        st.code(inbox)
        st.caption("Put thousands of PDFs/ZIPs here. Radar processes new files and skips previously hashed documents.")
        if st.button("Scan and process inbox", type="primary"):
            with st.spinner("Processing the inbox..."):
                results=process_directory(conn,inbox,recursive=True)
            st.success(f"Finished: {sum(r.status=="PROCESSED" for r in results)} processed · {sum(r.status=="DUPLICATE" for r in results)} duplicates · {sum(r.status=="FAILED" for r in results)} failed")

    with tabs[3]:
        rows=conn.execute("SELECT id,filename,document_type,classification_confidence,status,created_at FROM radar_documents ORDER BY id DESC LIMIT 500").fetchall()
        st.metric("Documents in intelligence store", conn.execute("SELECT COUNT(*) FROM radar_documents").fetchone()[0])
        if rows:
            st.dataframe([dict(r) for r in rows], use_container_width=True, hide_index=True)
        else:
            st.info("No documents processed yet.")

# ---------------------------------------------------------------------
# Live Monitor
# ---------------------------------------------------------------------
elif page == "Monitoring":
    cur, conn = get_cursor()
    st.markdown('<div class="eyebrow">Government source status</div>', unsafe_allow_html=True)
    st.title("Monitoring")
    config = acquisition_settings()
    enabled = config["enabled"] and config["cppp_enabled"]
    sources = db.list_acquisition_sources(cur)
    cppp_source = next((source for source in sources if source["source_key"] == "cppp"), None)
    runs = db.list_acquisition_runs(cur, limit=1)
    latest_run = runs[0] if runs else None
    last_scan = cppp_source["last_scan_at"] if cppp_source else None

    badge_class = "status-pill" if enabled else "status-pill paused"
    badge_text = "Monitoring enabled" if enabled else "Monitoring paused"
    st.markdown(f'<span class="{badge_class}"><span class="status-dot"></span>{badge_text}</span>', unsafe_allow_html=True)
    st.markdown(f'<div class="live-note">{human_age(last_scan)}</div>', unsafe_allow_html=True)

    metrics = st.columns(3)
    metrics[0].metric("New notices found", latest_run["records_new"] if latest_run else 0)
    metrics[1].metric("Documents collected", latest_run["documents_downloaded"] if latest_run else 0)
    manual_records = db.list_discovered_records(cur, acquisition_status="MANUAL_ACTION_REQUIRED")
    metrics[2].metric("Need your help", len(manual_records))

    if latest_run and latest_run["records_new"]:
        st.success(f"✨ New procurement notices found: {latest_run['records_new']}.")

    if st.button("Check government tenders", type="primary", icon=":material/sensors:", disabled=not enabled):
        client = PublicHttpClient(timeout=config["request_timeout"], max_retries=config["max_retries"], rate_limit_delay=config["rate_limit_delay"])
        try:
            connector = get_connector("cppp", client=client, search_url=config["cppp_search_url"])
            st.markdown('<span class="status-pill scanning"><span class="status-dot"></span>Scanning government tenders...</span>', unsafe_allow_html=True)
            with st.spinner("We're checking government sources..."):
                summary = scan_source(cur, connector, client=client, storage_root=config["storage_root"])
            conn.commit()
            if summary.status == "FAILED":
                st.error("The source check could not be completed. Try again later.")
                with st.expander("Technical details"):
                    st.write(summary.error or "No error details were recorded.")
            elif summary.status == "PARTIAL":
                st.warning(f"Check completed with {summary.records_seen} notices checked. Some items need attention.")
            else:
                if summary.records_new:
                    st.success(f"✨ {summary.records_new} new tender notice(s) found.")
                else:
                    st.success(f"Check complete. {summary.records_seen} notice(s) checked; no new tenders found.")
            if summary.manual_action_required:
                st.info(f"{summary.manual_action_required} item(s) need your help. See Alerts.")
        except Exception as exc:
            conn.commit()
            st.error("The source check could not be completed. Try again later.")
            with st.expander("Technical details"):
                st.write(str(exc))

    st.subheader("Latest activity")
    recent_records = db.list_discovered_records(cur, limit=4)
    if recent_records:
        for record in recent_records:
            st.write(f"{record['title'] or record['external_id']} · {record['organisation'] or 'Organisation not listed'}")
            st.caption(human_age(record["first_seen_at"]))
    else:
        st.markdown('<div class="empty-calm">We haven’t found any notices yet.</div>', unsafe_allow_html=True)
        st.write("Run a check to start watching procurement.")

    with st.expander("Advanced scan details"):
        if cppp_source:
            st.write(f"Source: {cppp_source['name']}")
            st.write(f"Last successful check: {cppp_source['last_success_at'] or 'None yet'}")
            st.write(f"Configured check interval: {config['poll_interval_minutes']} minutes")
        st.write(f"Search page: {config['cppp_search_url']}")
        if latest_run:
            st.write(f"Last run status: {latest_run['status']} · {latest_run['records_seen']} record(s) checked · {latest_run['documents_downloaded']} document(s) collected")


# ---------------------------------------------------------------------
# Radar: dedicated tender and intelligence screens
# ---------------------------------------------------------------------
elif page == "Radar":
    cur, conn = get_cursor()
    aoc_root = find_historical_aoc_root(historical_root_path())
    if aoc_root and duckdb_available():
        from core.dataset_import import historical_aoc_stats, query_historical_aocs, historical_aoc_cycle_signals
        st.divider()
        st.markdown('<div class="eyebrow">Recurring procurement intelligence</div>', unsafe_allow_html=True)
        st.subheader("Predicted upcoming re-opportunities")
        st.caption("These are calculated predictions from repeated historical AOC award dates. They are not confirmed future tenders until a new tender notice is detected.")
        try:
            rc1, rc2 = st.columns(2)
            with rc1:
                radar_state = st.text_input("State / Region Filter", placeholder="e.g. Maharashtra", key="radar_state").strip()
            with rc2:
                radar_city = st.text_input("City / District Filter", placeholder="e.g. Pune", key="radar_city").strip()

            with st.spinner("Aggregating historical dataset (this runs extremely fast via DuckDB)..."):
                signal_df = historical_aoc_cycle_signals(aoc_root, as_of=datetime.now().date(), horizon_days=365, limit=None, state=radar_state, city=radar_city)
            if signal_df.empty:
                st.info("No recurring procurement pattern was found in the imported AOC partitions for the next 12 months. Check the imported-year count below and make sure all intended year partitions are present.")
            else:
                today = datetime.now().date()
                current_mask = (signal_df["window_start"].dt.date <= today) & (signal_df["window_end"].dt.date >= today)
                current_count = int(current_mask.sum())
                future_count = int((signal_df["expected_next_date"].dt.date > today).sum())
                s1, s2, s3 = st.columns(3)
                s1.metric("Recurring signals", f"{len(signal_df):,}")
                s2.metric("In expected window now", f"{current_count:,}")
                s3.metric("Expected within 12 months", f"{future_count:,}")
                display = signal_df.copy()
                display["Expected window"] = display.apply(lambda r: f"{r['window_start'].date()} → {r['window_end'].date()}", axis=1)
                display["Last award"] = display["last_award_date"].dt.date
                display["Typical cycle"] = display["median_gap_days"].map(lambda x: f"{int(x)} days (~{x/30.44:.1f} mo)")
                display["Evidence"] = display["award_count"].map(lambda x: f"{int(x)} awards")
                display["Status"] = display.apply(lambda r: "🟡 Window now" if r["window_start"].date() <= today <= r["window_end"].date() else "🟡 Expected", axis=1)
                cols = ["Status", "signal_strength", "org_name", "title", "Expected window", "Last award", "Typical cycle", "Evidence", "last_tender_id", "last_detail_url"]
                display = display[cols].rename(columns={
                    "signal_strength": "Signal", "org_name": "Organisation", "title": "Requirement",
                    "last_tender_id": "Last Tender ID", "last_detail_url": "Source"
                })
                st.dataframe(display, use_container_width=True, hide_index=True, column_config={
                    "Source": st.column_config.LinkColumn("Source", display_text="Open"),
                    "Requirement": st.column_config.TextColumn("Requirement", width="large"),
                    "Organisation": st.column_config.TextColumn("Organisation", width="medium"),
                })
                st.caption("Method: same buyer + normalized requirement family, at least 2 dated awards, median historical interval, and an uncertainty window based on observed interval spread. This is an analytical prediction, not a guarantee of re-tendering.")
        except Exception as exc:
            st.error(f"Recurring opportunity analysis failed: {exc}")

        st.markdown('<div class="eyebrow">Historical award intelligence</div>', unsafe_allow_html=True)
        st.subheader("AOC history")
        stats = historical_aoc_stats(aoc_root)
        m1, m2, m3 = st.columns(3)
        m1.metric("Imported AOC records", f"{stats.get('total', 0):,}")
        m2.metric("Imported AOC years", f"{len(stats.get('partition_years', [])):,}")
        m3.metric("Organisations", f"{stats.get('organisations', 0):,}")
        imported_years = stats.get("year_values", [])
        partition_years = stats.get("partition_years", [])
        if imported_years:
            st.caption(f"AOC data years actually containing records: {', '.join(imported_years)}")
        if partition_years:
            st.caption(f"AOC partitions on disk: {', '.join(partition_years)}")
        if partition_years and partition_years != imported_years:
            st.warning(f"Partitions on disk: {', '.join(partition_years)}. Years with rows: {', '.join(imported_years) or 'none'}.")
        f1, f2, f3 = st.columns([2.5, 1, 1])
        aoc_search = f1.text_input("Search AOC history", placeholder="Organisation, tender ID, reference, or requirement", key="radar_aoc_search")
        aoc_year = f2.selectbox("Year", ["All years"] + [str(y) for y in range(datetime.now().year, 2011 - 1, -1)], key="radar_aoc_year")
        aoc_portal = f3.selectbox("Portal", ["All portals", "central", "state"], key="radar_aoc_portal")
        try:
            aoc_df = query_historical_aocs(aoc_root, aoc_search, aoc_year, aoc_portal, limit=None)
            st.caption(f"Showing {len(aoc_df):,} matching AOC records. Source records remain read-only in Parquet.")
            if not aoc_df.empty:
                st.dataframe(aoc_df, use_container_width=True, hide_index=True, column_config={
                    "detail_url": st.column_config.LinkColumn("Source", display_text="Open"),
                    "title": st.column_config.TextColumn("Award / Requirement", width="large"),
                    "org_name": st.column_config.TextColumn("Organisation", width="medium"),
                })
            else:
                st.info("No historical AOC records match these filters.")
        except Exception as exc:
            st.error(f"Historical AOC query failed: {exc}")
    else:
        st.info("No imported AOC history is available yet. Go to Procurement Data Center → Historical Data and import at least 2 AOC years.")

    st.divider()
    st.markdown('<div class="eyebrow">Live tender opportunities</div>', unsafe_allow_html=True)
    render_opportunities(cur)
elif page == "Search":
    cur, conn = get_cursor()
    st.markdown('<div class="eyebrow">Find procurement information</div>', unsafe_allow_html=True)
    st.title("Search")
    tender_tab, contract_tab = st.tabs(["Tender notices", "Contracts"])
    with tender_tab:
        render_tenders(cur, compact=True)
    with contract_tab:
        query = st.text_input("Search contracts", placeholder="Title, organisation, or vendor")
        results = db.search_contracts(cur, query) if query else db.list_contracts(cur)
        if not results:
            st.markdown('<div class="empty-calm">No matching contracts yet.</div>', unsafe_allow_html=True)
        for contract in results:
            org = cur.execute("SELECT name FROM organisations WHERE id = ?", (contract["org_id"],)).fetchone()
            with st.container(border=True):
                st.markdown(f"### {contract['title']}")
                st.write(buyer_display_name(org["name"] if org else None))
                st.write(contract_status_label(contract["status"]))
                if contract["current_expiry_estimate"]:
                    st.caption(f"Recorded contract end-date estimate: {contract['current_expiry_estimate']}")
                if st.button("View contract information", key=f"search_contract_{contract['id']}"):
                    st.session_state.selected_contract_id = contract["id"]
                    st.rerun()
        if st.session_state.selected_contract_id:
            st.divider()
            render_contract_detail(cur, st.session_state.selected_contract_id)

elif page == "Tenders":
    cur, conn = get_cursor()
    render_tenders(cur)

elif page == "Organisations":
    cur, conn = get_cursor()
    render_organisations(cur)

elif page == "Documents":
    cur, conn = get_cursor()
    render_documents(cur)

elif page == "Evidence":
    cur, conn = get_cursor()
    render_evidence(cur)

elif page == "Sources":
    cur, conn = get_cursor()
    render_sources(cur)

elif page == "Alerts":
    cur, conn = get_cursor()
    render_alerts(cur)

elif page == "Analytics":
    cur, conn = get_cursor()
    render_analytics(cur)

elif page == "Backtesting":
    cur, conn = get_cursor()
    render_backtesting(cur)

elif page == "Settings":
    render_settings()


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
# Procurement Cycles
# ---------------------------------------------------------------------
elif page == "Procurement Cycles":
    cur, conn = get_cursor()
    st.title("Procurement Cycles")
    st.caption(
        "A requirement is the underlying recurring need (e.g. \"CCTV AMC — XYZ Hospital\") that "
        "survives changes in tender title, vendor, or wording across tender and contract evidence. "
        "Tender observations remain separate from contract expiry records."
    )

    requirements = db.list_requirements(cur)
    if not requirements:
        st.info("No requirements identified yet. Requirements are detected automatically as documents "
                 "or tenders are observed, when title/organisation matches a known asset/service category.")

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
                st.write(f"- Observed {e['event_date'] or 'date unknown'} — {e['event_type']}")
                if e["discovered_record_id"] is not None:
                    tender = cur.execute(
                        "SELECT external_id, title, detail_url, source_page_url FROM discovered_records WHERE id = ?",
                        (e["discovered_record_id"],),
                    ).fetchone()
                    if tender:
                        st.write(f"Tender {tender['external_id']}: {tender['title'] or 'title unavailable'}")
                        if tender["detail_url"]:
                            st.link_button("Open source tender", tender["detail_url"], key=f"tender_source_{e['id']}")
                link = cur.execute(
                    "SELECT match_status, match_score, match_evidence FROM requirement_event_links WHERE event_id = ? AND requirement_id = ?",
                    (e["id"], r["id"]),
                ).fetchone()
                if link:
                    import json as _json
                    st.write(f"Link evidence: {link['match_status']} ({link['match_score']:.2f})")
                    st.json(_json.loads(link["match_evidence"]))

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
                "Observed dates are source facts. Cycle intervals and expected windows are inferences, "
                "not confirmed future tenders. Each event's linkage factors are shown above."
            )
            prediction = db.get_prediction_for_requirement(cur, r["id"])
            if prediction:
                st.write(
                    f"**Inferred re-opportunity window:** {prediction['window_start']} to "
                    f"{prediction['window_end']} (expected date {prediction['predicted_date']}; "
                    f"{prediction['confidence']} confidence)."
                )

else:
    st.session_state.active_page = "Overview"
    st.rerun()









