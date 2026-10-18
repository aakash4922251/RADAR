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

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import database as db
from core import pipeline
from core.procurement_intelligence import process_bytes, process_zip, process_directory, fetch_and_process_url, init_intelligence_tables
from core.dataset_import import DEFAULT_REPO, DatasetAsset, inspect_github_dataset, download_asset, register_imported_dataset, query_imported_dataset, duckdb_available, find_historical_aoc_root
from core.proof_packet import build_proof_packet_text
from core.brief_pdf import build_buyer_brief, format_inr
from core.contract_expiry import details_root
from core.contract_tracker import portfolio, reread_all, track_documents
from core.ingestion import SUPPORTED_EXTENSIONS as UPLOAD_EXTENSIONS, ocr_available
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


def close_page_connections():
    """Roll back and close every connection a page run opened. Pages commit
    what they mean to keep; anything left open would hold SQLite's write lock
    and block every other session and the background worker."""
    for conn in st.session_state.get("_db_connections", []):
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
    st.session_state["_db_connections"] = []


def get_cursor():
    conn = db.get_connection()
    st.session_state.setdefault("_db_connections", []).append(conn)
    return conn.cursor(), conn


# A run cut short by st.rerun() or an error skips the cleanup at the end of
# this script, so release whatever the previous run left behind first.
close_page_connections()


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

VERIFY_CAPTION = ("Verify any row on the official portal: open [CPPP → Result of Tenders](https://eprocure.gov.in/cppp/resultoftendersnew) and search its Tender ID. Portal links are session-bound and expire, so the app shows the ID instead of a link.")


DOC_TYPES = [
    "nit", "bid_document", "corrigendum", "aoc", "loa", "work_order",
    "signed_agreement", "site_handover_letter", "sla_stc",
    "platform_amendment", "termination_notice", "department_page", "other",
]


# ---------------------------------------------------------------------
# Sidebar navigation
# ---------------------------------------------------------------------
PRIMARY_NAV = [
    ("My contracts", "Tracker", ":material/assignment:"),
    ("Contracts ending", "Expiry", ":material/event_upcoming:"),
    ("Buyer profiles", "Buyer", ":material/apartment:"),
    ("Repeat buyers", "Finder", ":material/travel_explore:"),
    ("Live tenders", "Monitoring", ":material/sensors:"),
    ("Data Center", "Data Center", ":material/cloud_upload:"),
]

MORE_NAV = [
    ("Add document manually", "Upload Document", ":material/upload_file:"),
    ("Search", "Search", ":material/search:"),
    ("Home", "Home", ":material/home:"),
    ("Radar", "Radar", ":material/radar:"),
    ("Alerts", "Alerts", ":material/notifications:"),
    ("Tenders", "Tenders", ":material/description:"),
    ("Procurement cycles", "Procurement Cycles", ":material/cycle:"),
    ("Documents", "Documents", ":material/folder_open:"),
    ("Organisations", "Organisations", ":material/apartment:"),
    ("Evidence", "Evidence", ":material/fact_check:"),
    ("Analytics", "Analytics", ":material/monitoring:"),
    ("Backtesting", "Backtesting", ":material/science:"),
    ("Sources", "Sources", ":material/source:"),
    ("Settings", "Settings", ":material/settings:"),
    ("Contract search", "Contract Search", ":material/search:"),
    ("Review queue", "Review Queue", ":material/rule:"),
]


def set_active_page(name: str):
    st.session_state.active_page = name
    if name == "Radar":
        st.session_state.selected_requirement_id = None


if "active_page" not in st.session_state:
    st.session_state.active_page = "Tracker"
elif st.session_state.active_page == "Overview":
    st.session_state.active_page = "Tracker"
elif st.session_state.active_page == "Opportunities":
    st.session_state.active_page = "Radar"
elif st.session_state.active_page == "Live Monitor":
    st.session_state.active_page = "Monitoring"

st.sidebar.markdown(
    '<div class="radar-brand">OPPORTUNITY RADAR</div>'
    '<div class="radar-brand-sub">Know which government contracts end — before the tender is out</div>',
    unsafe_allow_html=True,
)
for label, destination, icon in PRIMARY_NAV:
    st.sidebar.button(
        label,
        key=f"primary_nav_{destination}",
        icon=icon,
        type="primary" if st.session_state.active_page == destination else "secondary",
        width="stretch",
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
            width="stretch",
            on_click=set_active_page,
            args=(destination,),
        )

page = st.session_state.active_page


def historical_root_path():
    return Path(os.path.dirname(os.path.abspath(__file__))) / "data" / "historical"


# The historical scan reads millions of Parquet rows, so results are cached
# per filter combination and recomputed at most once a day.
@st.cache_data(ttl=24 * 3600, show_spinner=False)
def cached_cycle_signals(root: str, as_of_iso: str, state: str, city: str, keyword: str):
    # Every requirement still active: next award expected anywhere from two
    # years ago (often late or extended) to three years ahead.
    from core.dataset_import import historical_aoc_cycle_signals
    return historical_aoc_cycle_signals(root, as_of=as_of_iso, horizon_days=1095, overdue_days=730, limit=None,
                                        state=state, city=city, keyword=keyword)


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def cached_backtest(root: str, as_of_iso: str):
    from core.dataset_import import backtest_aoc_signals
    return backtest_aoc_signals(root, as_of=as_of_iso, horizon_days=182)


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def cached_contracts_ending(root: str, as_of_iso: str, within_days: int, state: str, city: str, keyword: str,
                            min_value: float):
    from core.contract_expiry import contracts_ending
    return contracts_ending(root, as_of=as_of_iso, within_days=within_days, state=state, city=city,
                            keyword=keyword, min_value_inr=min_value)


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def cached_expiry_coverage(root: str):
    from core.contract_expiry import expiry_coverage
    return expiry_coverage(root)


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def cached_expiry_backtest(root: str):
    from core.contract_expiry import backtest_contract_expiry
    results = [backtest_contract_expiry(root, as_of=a) for a in ("2023-07-01", "2024-01-01", "2024-07-01", "2025-01-01")]
    return [(r["as_of"], r["summary"]) for r in results if not r["summary"].empty]


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def cached_search_buyers(root: str, text: str):
    from core.buyer_intel import search_buyers
    return search_buyers(root, text)


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def cached_buyer_profile(root: str, buyer: str, as_of_iso: str):
    from core.buyer_intel import buyer_profile
    return buyer_profile(root, buyer, as_of=as_of_iso)


def open_buyer(name: str):
    st.session_state.buyer_selected = name
    st.session_state.buyer_search = name
    st.session_state.active_page = "Buyer"


def kpi_row(items):
    cols = st.columns(len(items))
    for col, (label, value) in zip(cols, items):
        col.metric(label, value)


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def cached_aoc_stats(root: str):
    from core.dataset_import import historical_aoc_stats
    return historical_aoc_stats(root)


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
    if home_actions[0].button("Explore Radar", type="primary", icon=":material/radar:", width="stretch"):
        set_page_and_rerun("Radar")
    if home_actions[1].button("Latest activity", icon=":material/history:", width="stretch"):
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
            st.dataframe([{"File":r.filename,"Type":r.document_type,"Status":r.status,"Facts":len(r.facts)} for r in results], width="stretch", hide_index=True)

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
                                st.dataframe(df, width="stretch", hide_index=True)
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
            st.dataframe([dict(r) for r in rows], width="stretch", hide_index=True)
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
elif page == "Tracker":
    cur, conn = get_cursor()
    st.markdown('<div class="eyebrow">Contract tracker</div>', unsafe_allow_html=True)
    st.title("My contracts")
    st.markdown(
        '<div class="page-subtitle">Upload award letters, work orders or agreements — one file, many files or a ZIP. '
        'Each contract\'s end date is read from the documents themselves, with the exact quote that proves it.</div>',
        unsafe_allow_html=True,
    )
    today = datetime.now().date()
    book = portfolio(cur, today=today)

    with st.expander("Add documents", expanded=book.empty, icon=":material/upload_file:"):
        ocr_ready = ocr_available()
        uploads = st.file_uploader(
            "Award letters, work orders, agreements, corrigenda — PDF, scans, photos, Word, Excel or a ZIP",
            type=sorted(ext.lstrip(".") for ext in UPLOAD_EXTENSIONS) + ["zip"],
            accept_multiple_files=True, key="tracker_uploads",
        )
        photo = None
        if ocr_ready and st.toggle("Take a photo instead", key="tracker_camera",
                                   help="Use this device's camera. Hold the page flat, fill the frame, avoid glare."):
            photo = st.camera_input("Photograph the document", key="tracker_camera_input")
        same = st.checkbox("These files are all about one contract", key="tracker_same",
                           help="Leave unticked when each file is a different contract. Files that share a "
                                "CPPP Tender ID or tender reference number are linked automatically.")
        if not ocr_ready:
            st.caption("Scanned PDFs and photos need text recognition (OCR), which is not installed on this "
                       "computer. Digital PDFs, Word, Excel and text files work as they are.")
        files = [(u.name, u.getvalue()) for u in uploads or []]
        if photo is not None:
            files.append((f"photo_{datetime.now():%Y%m%d_%H%M%S}.jpg", photo.getvalue()))
        if st.button("Add to tracker", type="primary", disabled=not files, icon=":material/add:"):
            with st.spinner("Reading documents (scans and photos take a few seconds per page)..."):
                results = track_documents(cur, files, same_contract=same)
            conn.commit()
            st.session_state.tracker_last_results = [r.__dict__ for r in results]
            st.rerun()
        last = st.session_state.get("tracker_last_results")
        if last:
            added = sum(r["status"] == "ADDED" for r in last)
            st.success(f"{added} document(s) added · {sum(r['status'] == 'ALREADY_TRACKED' for r in last)} already tracked · "
                       f"{sum(r['status'] in ('FAILED', 'SKIPPED') for r in last)} not read")
            st.dataframe(pd.DataFrame([{
                "File": r["filename"], "Result": {"ADDED": "Added", "ALREADY_TRACKED": "Already tracked",
                                                  "FAILED": "Could not read", "SKIPPED": "Unsupported"}[r["status"]],
                "Contract": r["contract_title"] or "", "Ends on": r["end_date"] or "Not found in document",
                "Note": r["message"],
            } for r in last]), width="stretch", hide_index=True)

    if book.empty:
        st.markdown('<div class="empty-calm">No contracts tracked yet.</div>', unsafe_allow_html=True)
        st.write("Add an award letter or work order above to see its end date and the evidence behind it.")
    else:
        known = book["days_left"].notna()
        tender_stage = book["stage"] == "TENDER"
        # A tender has no end date until it is awarded; that is expected, not a problem.
        attention = book["status"].isin(["NEEDS_REVIEW", "CONFLICTING_EVIDENCE", "INSUFFICIENT_EVIDENCE"]) & ~tender_stage
        kpi_row([
            ("Contracts tracked", f"{len(book):,}"),
            ("Ending in 90 days", f"{int((known & book['days_left'].between(0, 90)).sum()):,}"),
            ("Ending in 12 months", f"{int((known & book['days_left'].between(0, 365)).sum()):,}"),
            ("Need a closer look", f"{int(attention.sum()):,}"),
        ])
        view = st.segmented_control("Show", ["All", "Ending in 90 days", "Need a closer look", "Tenders not awarded yet",
                                             "End date unknown"], default="All", key="tracker_view")
        shown = book
        if view == "Ending in 90 days":
            shown = book[known & book["days_left"].between(0, 90)]
        elif view == "Need a closer look":
            shown = book[attention]
        elif view == "Tenders not awarded yet":
            shown = book[tender_stage]
        elif view == "End date unknown":
            shown = book[~known & ~tender_stage]
        confidence_label = {"HIGH": "Strong", "MEDIUM": "Good", "LOW": "Weak"}
        stage_label = {"AWARDED": "Awarded", "TENDER": "Tender — not awarded yet", "UNCLEAR": "Unclear"}
        display = pd.DataFrame({
            "Ends on": [e if isinstance(e, str) else ("Not awarded yet" if st_ == "TENDER" else "Unknown")
                        for e, st_ in zip(shown["current_expiry_estimate"], shown["stage"])],
            "Days left": shown["days_left"],
            "Contract": shown["title"],
            "Buyer": shown["buyer"].map(buyer_display_name),
            "Supplier": shown["vendor"].fillna("—"),
            "Stage": shown["stage"].map(stage_label),
            "Evidence": [confidence_label.get(c, "—" if st_ == "TENDER" else "Not enough")
                         for c, st_ in zip(shown["expiry_confidence"], shown["stage"])],
            "Status": [("Waiting for award" if st_ == "TENDER" and s_ == "INSUFFICIENT_EVIDENCE" else contract_status_label(s_))
                       for s_, st_ in zip(shown["status"], shown["stage"])],
            "Latest possible end": shown["current_expiry_ceiling"].fillna("—"),
            "Documents": shown["documents"],
        })
        st.dataframe(display, width="stretch", hide_index=True, column_config={
            "Contract": st.column_config.TextColumn(width="large"),
            "Days left": st.column_config.NumberColumn(format="%d"),
            "Latest possible end": st.column_config.TextColumn(
                help="End date if every extension option in the documents were used."),
        })
        c1, c2 = st.columns([1, 3])
        c1.download_button("Download (CSV)", data=display.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"my_contracts_{today.isoformat()}.csv", mime="text/csv",
                           icon=":material/download:")
        if c2.button("Re-read all documents with the latest rules", icon=":material/refresh:",
                     help="Reads every tracked document again. Use after an update so documents added "
                          "earlier benefit from improved reading. The calculation history is kept."):
            with st.spinner("Re-reading documents..."):
                n = reread_all(cur)
            conn.commit()
            st.session_state.tracker_last_results = None
            st.toast(f"Re-read {n} contract(s).")
            st.rerun()

        st.divider()
        def _ends(r):
            if isinstance(r.current_expiry_estimate, str):
                return f"ends {r.current_expiry_estimate}"
            return "not awarded yet" if r.stage == "TENDER" else "end date unknown"
        titles = {int(r.id): f"{r.title} · {_ends(r)}" for r in book.itertuples()}
        chosen = st.selectbox("Open a contract", list(titles), format_func=lambda i: titles[i], key="tracker_pick")
        if chosen:
            more = st.file_uploader("Add another document to this contract (e.g. an extension order)",
                                    type=sorted(ext.lstrip(".") for ext in UPLOAD_EXTENSIONS) + ["zip"], accept_multiple_files=True,
                                    key=f"tracker_more_{chosen}")
            if more and st.button("Add to this contract", key=f"tracker_more_btn_{chosen}"):
                with st.spinner("Reading documents..."):
                    track_documents(cur, [(u.name, u.getvalue()) for u in more], contract_id=chosen)
                conn.commit()
                st.rerun()
            render_contract_detail(cur, chosen)
            with st.expander("Remove this contract", icon=":material/delete:"):
                st.write("Removes the contract and the documents uploaded for it, for example a wrong upload or a "
                         "duplicate. This cannot be undone.")
                sure = st.checkbox("Yes, remove it", key=f"tracker_remove_ok_{chosen}")
                if st.button("Remove contract", key=f"tracker_remove_{chosen}", disabled=not sure, type="primary"):
                    pipeline.remove_contract(cur, chosen)
                    conn.commit()
                    st.session_state.pop("tracker_pick", None)
                    st.toast("Contract removed.")
                    st.rerun()

elif page == "Expiry":
    st.markdown('<div class="eyebrow">Contract expiry radar</div>', unsafe_allow_html=True)
    st.title("Contracts ending soon")
    st.markdown(
        '<div class="page-subtitle">Government contracts whose published completion date is coming up — with the '
        'current holder and value. A contract ending is the earliest public sign that its replacement will be tendered.</div>',
        unsafe_allow_html=True,
    )
    droot = details_root(historical_root_path())
    if not droot or not duckdb_available():
        st.info("Award details are not imported yet. Go to Data Center → Historical Data and import the aoc_details partitions.")
    else:
        coverage = cached_expiry_coverage(str(droot))
        today = datetime.now().date()
        f1, f2, f3 = st.columns([2, 1.2, 1.2])
        keyword = f1.text_input("What do you supply?", placeholder="e.g. pipeline, security, painting, civil works", key="exp_keyword").strip()
        state = f2.text_input("State", placeholder="e.g. Maharashtra", key="exp_state").strip()
        city = f3.text_input("Buyer, site or city", placeholder="e.g. HPCL, Mumbai", key="exp_city").strip()
        g1, g2, g3 = st.columns([1.2, 1.2, 2])
        within_label = g1.selectbox("Ending within", ["3 months", "6 months", "12 months"], index=1, key="exp_within")
        value_label = g2.selectbox("Contract value", ["Any value", "₹10 lakh+", "₹1 crore+", "₹10 crore+"], key="exp_value")
        repeat_only = g3.checkbox("Only work this buyer has awarded before", key="exp_repeat",
                                  help="Contracts whose buyer awarded the same requirement at least once earlier — the strongest re-tender signal.")
        within_days = {"3 months": 91, "6 months": 182, "12 months": 365}[within_label]
        min_value = {"Any value": 0, "₹10 lakh+": 1e6, "₹1 crore+": 1e7, "₹10 crore+": 1e8}[value_label]

        try:
            with st.spinner("Reading award records..."):
                ending = cached_contracts_ending(str(droot), today.isoformat(), within_days, state, city, keyword, min_value)
        except Exception as exc:
            st.error(f"Search failed: {exc}")
            ending = None

        if ending is not None:
            if repeat_only:
                ending = ending[ending["prior_awards"] > 0]
            if ending.empty:
                st.info("No published contract end dates match these filters. Try a broader keyword, a longer window or a lower value.")
            else:
                kpi_row([
                    ("Contracts ending", f"{len(ending):,}"),
                    ("Combined value", format_inr(ending["contract_value"].sum())),
                    ("Buyers", f"{ending['buyer'].nunique():,}"),
                    ("Awarded before", f"{int((ending['prior_awards'] > 0).sum()):,}"),
                ])
                display = pd.DataFrame({
                    "Ends on": ending["end_date"].dt.date,
                    "Days left": (ending["end_date"].dt.date - today).map(lambda d: d.days),
                    "Buyer": ending["buyer"],
                    "Contract": ending["description"],
                    "Current holder": ending["incumbent"],
                    "Value": ending["contract_value"].map(format_inr),
                    "Value (INR)": ending["contract_value"],
                    "Term": ending["duration_days"].map(lambda d: f"{d / 30.44:.0f} months"),
                    "Bids last time": ending["num_bids_received"],
                    "Awarded before": ending["prior_awards"].astype(int),
                    "Tender ID": ending["tender_id"],
                    "Tender ref.": ending["tender_ref_no"],
                })
                st.dataframe(display, width="stretch", hide_index=True, column_config={
                    "Contract": st.column_config.TextColumn(width="large"),
                    "Current holder": st.column_config.TextColumn(width="medium"),
                    "Value (INR)": None,
                    "Days left": st.column_config.NumberColumn(format="%d"),
                })
                st.caption(VERIFY_CAPTION)
                a1, a2 = st.columns([1, 2])
                a1.download_button(
                    "Download list (CSV)", data=display.to_csv(index=False).encode("utf-8-sig"),
                    file_name=f"contracts_ending_{today.isoformat()}.csv", mime="text/csv",
                    icon=":material/download:", type="primary", width="stretch",
                )
                with a2:
                    buyers = ending["buyer"].value_counts().index.tolist()
                    b1, b2 = st.columns([3, 1])
                    chosen = b1.selectbox("Open a buyer profile", buyers, key="exp_buyer_pick", label_visibility="collapsed")
                    b2.button("Open profile", key="exp_open_buyer", on_click=open_buyer, args=(chosen,), width="stretch")

        with st.expander("Where does this come from, and how reliable is it?"):
            names = ", ".join(coverage["buyer_names"])
            st.write(
                f"**Source.** Award-of-contract records on the Central Public Procurement Portal. "
                f"{coverage['contracts_with_end_date']:,} awarded contracts carry a completion date published by the buyer "
                f"itself; nothing is estimated. Buyers that publish completion dates today: {names}."
            )
            st.write(
                "**Accuracy check.** For past dates, the check takes contracts that were due to end in the next six months, "
                "then looks at when the same buyer awarded the same work again."
            )
            if st.button("Run accuracy check (about 20 seconds)", key="exp_backtest"):
                with st.spinner("Re-running the past..."):
                    st.session_state.exp_backtest_result = cached_expiry_backtest(str(droot))
            results = st.session_state.get("exp_backtest_result")
            if results:
                rows = []
                for as_of, summary in results:
                    r = summary.iloc[0]
                    rows.append({
                        "Pretend date": as_of.strftime("%d %b %Y"),
                        "Contracts tested": int(r["contracts_tested"]),
                        "Re-awarded later": f"{r['reawarded_by_data_end']:.0%}",
                        "Of those, within 90 days of end date": f"{r['hit_rate_when_reawarded']:.0%}",
                        "New award near end date": f"{r['hit_rate_near_end_date']:.1%}",
                        "Same-width window at a wrong time": f"{r['baseline_hit_rate']:.1%}",
                    })
                st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
                st.caption(
                    "Read the last two columns together: when they are close, the end date told us nothing about timing; when the "
                    "first is clearly higher, it did. Many contracts are one-off works that are never re-awarded, so treat each "
                    "row as a lead to watch, not a guaranteed tender."
                )

elif page == "Buyer":
    st.markdown('<div class="eyebrow">Buyer intelligence</div>', unsafe_allow_html=True)
    st.title("Buyer profiles")
    st.markdown(
        '<div class="page-subtitle">What a government buyer awards, to whom, how competitive it is, and which of its contracts end next.</div>',
        unsafe_allow_html=True,
    )
    droot = details_root(historical_root_path())
    if not droot or not duckdb_available():
        st.info("Award details are not imported yet. Go to Data Center → Historical Data and import the aoc_details partitions.")
    else:
        today = datetime.now().date()
        query = st.text_input("Find a buyer", placeholder="e.g. Hindustan Petroleum, BHEL, AIIMS, Municipal Corporation", key="buyer_search").strip()
        selected = st.session_state.get("buyer_selected")
        if query:
            matches = cached_search_buyers(str(droot), query)
            if matches.empty:
                st.info("No buyer name contains that text. Try a shorter part of the name.")
            else:
                options = matches["buyer"].tolist()
                labels = {b: f"{l} · {a:,} awards" for b, l, a in zip(matches["buyer"], matches["label"], matches["awards"])}
                index = options.index(selected) if selected in options else 0
                selected = st.selectbox("Buyer", options, index=index, format_func=lambda b: labels[b], key="buyer_pick")
                st.session_state.buyer_selected = selected
        else:
            selected = None

        if selected:
            with st.spinner("Building profile..."):
                profile = cached_buyer_profile(str(droot), selected, today.isoformat())
            s = profile["summary"]
            st.subheader(s["buyer"])
            st.caption(f"Award records from {s['first_award']:%d %b %Y} to {s['last_award']:%d %b %Y}.")
            kpi_row([
                ("Contracts awarded", f"{s['awards']:,}"),
                ("Total awarded value", format_inr(s["total_value"])),
                ("Distinct suppliers", f"{s['distinct_suppliers']:,}"),
                ("Median bids per tender", f"{s['median_bids']:.0f}" if s["median_bids"] else "—"),
                ("Ending in 12 months", f"{len(profile['ending']):,}"),
            ])

            d1, d2, d3 = st.columns([2, 1, 1])
            prepared_for = d1.text_input("Prepared for (optional, printed on the brief)", key="brief_for", placeholder="Your client or company name")
            pdf = build_buyer_brief(profile, prepared_for=prepared_for.strip(), generated=today)
            safe = "".join(ch if ch.isalnum() else "_" for ch in s["buyer"])[:60]
            d2.download_button("Opportunity Brief (PDF)", data=pdf, file_name=f"opportunity_brief_{safe}.pdf",
                               mime="application/pdf", icon=":material/picture_as_pdf:", type="primary", width="stretch")
            d3.download_button("Ending contracts (CSV)", data=profile["ending"].to_csv(index=False).encode("utf-8-sig"),
                               file_name=f"contracts_ending_{safe}.csv", mime="text/csv",
                               icon=":material/download:", width="stretch")

            t1, t2, t3, t4 = st.tabs(["Contracts ending", "Repeat requirements", "Top suppliers", "Recent awards"])
            with t1:
                ending = profile["ending"]
                if ending.empty:
                    st.info("This buyer has not published completion dates for contracts ending in the next 12 months.")
                else:
                    st.dataframe(pd.DataFrame({
                        "Ends on": ending["end_date"].dt.date,
                        "Contract": ending["description"],
                        "Current holder": ending["incumbent"],
                        "Value": ending["contract_value"].map(format_inr),
                        "Term": ending["duration_days"].map(lambda d: f"{d / 30.44:.0f} months"),
                        "Awarded before": ending["prior_awards"].astype(int),
                        "Tender ID": ending["tender_id"],
                        "Tender ref.": ending["tender_ref_no"],
                    }), width="stretch", hide_index=True,
                        column_config={"Contract": st.column_config.TextColumn(width="large")})
            with t2:
                repeat = profile["repeat_requirements"]
                if repeat.empty:
                    st.info("No requirement has been awarded more than once by this buyer.")
                else:
                    st.dataframe(pd.DataFrame({
                        "Requirement (latest wording)": repeat["requirement"],
                        "Awards": repeat["awards"],
                        "Total value": repeat["value"].map(format_inr),
                        "First award": repeat["first_award"].dt.date,
                        "Latest award": repeat["last_award"].dt.date,
                    }), width="stretch", hide_index=True,
                        column_config={"Requirement (latest wording)": st.column_config.TextColumn(width="large")})
            with t3:
                suppliers = profile["suppliers"]
                if suppliers.empty:
                    st.info("No named suppliers in this buyer's award records.")
                else:
                    st.dataframe(pd.DataFrame({
                        "Supplier": suppliers["supplier"],
                        "Contracts won": suppliers["contracts_won"],
                        "Value of contracts won": suppliers["value_involved"].map(format_inr),
                        "Latest win": suppliers["last_win"].dt.date,
                    }).head(200), width="stretch", hide_index=True)
                    st.caption("Joint awards count once for each named winner; value is that of the whole contract.")
            with t4:
                recent = profile["recent_awards"]
                st.dataframe(pd.DataFrame({
                    "Awarded": recent["contract_date"].dt.date,
                    "Contract": recent["description"],
                    "Winner": recent["incumbent"],
                    "Value": recent["contract_value"].map(format_inr),
                    "Bids": recent["num_bids_received"],
                    "Tender ID": recent["tender_id"],
                    "Tender ref.": recent["tender_ref_no"],
                }), width="stretch", hide_index=True, column_config={
                    "Contract": st.column_config.TextColumn(width="large"),
                })
            st.caption(VERIFY_CAPTION)
        elif not query:
            st.markdown('<div class="empty-calm">Search for any central or state government buyer.</div>', unsafe_allow_html=True)
            st.write("Profiles cover every buyer in the CPPP award records — not only those that publish end dates.")

elif page == "Finder":
    st.markdown('<div class="eyebrow">Repeat government buyers</div>', unsafe_allow_html=True)
    st.title("Repeat buyers")
    aoc_root = find_historical_aoc_root(historical_root_path())
    if not aoc_root or not duckdb_available():
        st.info("No award history is available yet. Go to Data Center → Historical Data and import at least 3 years of AOC data.")
    else:
        stats = cached_aoc_stats(str(aoc_root))
        years = stats.get("year_values", [])
        st.caption(
            f"Government buyers who keep awarding contracts for what you supply, found from "
            f"{stats.get('total', 0):,} official award records ({years[0]}–{years[-1]}) on the Central Public Procurement Portal."
            if years else "Government buyers who keep awarding contracts for what you supply, found from official award history."
        )
        if len(years) < 3:
            st.warning(f"Only {len(years)} year(s) of award history imported. Annual contracts need 3+ years to show a pattern — import more years in Data Center.")

        f1, f2, f3, f4 = st.columns([2, 1.2, 1.2, 1.2])
        keyword = f1.text_input("What do you supply?", placeholder="e.g. CCTV, housekeeping, security, lift", key="finder_keyword").strip()
        state = f2.text_input("State", placeholder="e.g. Maharashtra", key="finder_state").strip()
        city = f3.text_input("City, district or buyer", placeholder="e.g. Pune, AIIMS", key="finder_city").strip()
        min_signal = f4.selectbox("Evidence", ["Medium or strong", "Strong only", "All, incl. early"], key="finder_signal")

        today = datetime.now().date()
        try:
            with st.spinner("Scanning award history (first search takes ~20 seconds, repeats are instant)..."):
                signal_df = cached_cycle_signals(str(aoc_root), today.isoformat(), state, city, keyword)
        except Exception as exc:
            st.error(f"Opportunity search failed: {exc}")
            signal_df = None

        if signal_df is not None:
            allowed = {"All, incl. early": {"HIGH", "MEDIUM", "EARLY"}, "Medium or strong": {"HIGH", "MEDIUM"}, "Strong only": {"HIGH"}}[min_signal]
            signal_df = signal_df[signal_df["signal_strength"].isin(allowed)].copy()
            signal_df["_rank"] = signal_df["signal_strength"].map({"HIGH": 0, "MEDIUM": 1, "EARLY": 2})
            signal_df = signal_df.sort_values(["_rank", "award_count", "last_award_date"], ascending=[True, False, False])
            if signal_df.empty:
                st.info("No repeat buyers match these filters. Try a broader keyword, remove the state/city filter, or include early signals.")
            else:
                m1, m2, m3 = st.columns(3)
                m1.metric("Repeat requirements", f"{len(signal_df):,}")
                m2.metric("Strong evidence", f"{int((signal_df['signal_strength'] == 'HIGH').sum()):,}")
                m3.metric("Distinct buyers", f"{signal_df['org_name'].nunique():,}")

                display = pd.DataFrame({
                    "Evidence": signal_df["signal_strength"].map({"HIGH": "Strong", "MEDIUM": "Medium", "EARLY": "Early"}),
                    "Buyer": signal_df["org_name"].map(buyer_display_name),
                    "State": signal_df["state"].fillna(""),
                    "Requirement (latest title)": signal_df["title"],
                    "Awards seen": signal_df["award_count"].astype(int),
                    "Typical gap": signal_df["median_gap_days"].map(lambda d: f"~{d / 30.44:.0f} months"),
                    "First seen": signal_df["first_award_date"].dt.date,
                    "Last awarded": signal_df["last_award_date"].dt.date,
                    "Last tender ID": signal_df["last_tender_id"],
                })
                st.dataframe(display, width="stretch", hide_index=True, column_config={
                    "Requirement (latest title)": st.column_config.TextColumn(width="large"),
                    "Buyer": st.column_config.TextColumn(width="medium"),
                })
                st.caption(VERIFY_CAPTION)
                st.download_button(
                    "Download this list (CSV)", data=display.to_csv(index=False).encode("utf-8-sig"),
                    file_name=f"repeat_buyers_{today.isoformat()}.csv", mime="text/csv",
                    icon=":material/download:", type="primary",
                )
                st.caption(
                    "How this works: the same buyer awarded the same requirement at least twice. "
                    "Strong = 6+ awards at a steady pace; Medium = 4+ awards; Early = 2–3 awards. "
                    "This shows who keeps buying, not the date of the next tender — add the "
                    "buyer's current award or work order under My contracts to find when it ends."
                )

        with st.expander("How reliable is this?"):
            check_as_of = datetime(today.year - 1, 1, 1).date()
            st.write(
                f"The accuracy check pretends it is {check_as_of:%d %b %Y}, builds this list from older awards only, "
                "and then looks at what those buyers actually did afterwards."
            )
            if st.button("Run accuracy check (about 30 seconds)", key="finder_backtest"):
                with st.spinner("Re-running the past..."):
                    st.session_state.finder_backtest_result = cached_backtest(str(aoc_root), check_as_of.isoformat())
            result = st.session_state.get("finder_backtest_result")
            if result:
                summary = result["summary"].set_index("evidence")
                rows = []
                for key, label in (("HIGH", "Strong"), ("MEDIUM", "Medium"), ("EARLY", "Early")):
                    if key in summary.index and summary.loc[key, "scored"]:
                        rows.append({
                            "Evidence": label,
                            "Requirements tested": int(summary.loc[key, "scored"]),
                            "Bought again afterwards": f"{summary.loc[key, 'retendered_by_data_end']:.0%}",
                            "Hit in predicted window": f"{summary.loc[key, 'hit_rate']:.0%}",
                            "Hit in a wrong-time window": f"{summary.loc[key, 'baseline_hit_rate']:.0%}",
                        })
                st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
                st.caption(
                    f"Checked against awards up to {result['data_end']:%d %b %Y}. “Bought again afterwards” is the "
                    "figure to rely on. The two window columns show that timing a re-tender from award history alone "
                    "is no better than chance, which is why this list does not promise dates."
                )

elif page == "Radar":
    cur, conn = get_cursor()
    aoc_root = find_historical_aoc_root(historical_root_path())
    if aoc_root and duckdb_available():
        from core.dataset_import import historical_aoc_stats, query_historical_aocs
        st.divider()
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
                st.dataframe(aoc_df, width="stretch", hide_index=True, column_config={
                    "detail_url": None,
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
    st.title("Add a document manually")
    st.caption("For full control: choose the document type and the contract it belongs to yourself. For everyday use, My contracts does this automatically.")

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
    st.session_state.active_page = "Tracker"
    st.rerun()

close_page_connections()









