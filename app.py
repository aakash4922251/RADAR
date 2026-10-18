"""
Contract Expiry Radar — public page.

Upload a procurement document, get the contract's end date with proof.
This page shows only what the current browser session has uploaded — it is
not a shared company dashboard (that is pages/01_Internal_Dashboard.py).

Run with:  streamlit run app.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.contract_tracker import portfolio, track_documents
from core.ingestion import SUPPORTED_EXTENSIONS, ocr_available
from core.proof_packet import build_proof_packet_text
from db import database as db
from ui_labels import evidence_label, period_label, status_label
from ui_shared import close_page_connections, get_cursor, init_page
from ui_theme import apply_theme

st.set_page_config(page_title="Contract Expiry Radar", page_icon=":material/event_upcoming:", layout="centered")
init_page()
apply_theme(mode="public")

UPLOAD_TYPES = sorted(ext.lstrip(".") for ext in SUPPORTED_EXTENSIONS) + ["zip"]
NOT_ADDED = {"FAILED", "SKIPPED"}
PILL_CLASS = {"HIGH": "evidence-strong", "MEDIUM": "evidence-medium"}

cur, conn = get_cursor()
st.session_state.setdefault("my_contract_ids", [])  # this session's own contracts, most recent first

st.markdown(
    '<div class="bg-blob blob-1"></div><div class="bg-blob blob-2"></div><div class="bg-blob blob-3"></div>',
    unsafe_allow_html=True,
)
st.markdown(
    '<div class="brand-row"><span class="brand-mark">CE</span>'
    '<span class="brand-name">Contract Expiry Radar</span></div>',
    unsafe_allow_html=True,
)
st.markdown('<div class="hero-title">When does your government contract end?</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="hero-sub">Upload the tender notice, award letter, work order or agreement. '
    'We read the dates and give you the end date — with the exact sentence that proves it.</div>',
    unsafe_allow_html=True,
)
st.markdown(
    '<div class="step-row">'
    '<div class="step"><div class="step-num">1</div><div class="step-label">Upload</div>'
    '<div class="step-copy">PDF, scan, photo, Word, Excel or a ZIP</div></div>'
    '<div class="step"><div class="step-num">2</div><div class="step-label">We read it</div>'
    '<div class="step-copy">A few seconds per page, longer for scans</div></div>'
    '<div class="step"><div class="step-num">3</div><div class="step-label">Get your date</div>'
    '<div class="step-copy">End date, confidence, and the proof</div></div>'
    '</div>',
    unsafe_allow_html=True,
)

ocr_ready = ocr_available()
uploads = st.file_uploader(
    "Drop your documents here, or click to browse",
    type=UPLOAD_TYPES, accept_multiple_files=True, key="uploads",
)
photo = None
if ocr_ready and st.toggle("Take a photo instead", key="camera", help="Hold the page flat, fill the frame, avoid glare."):
    photo = st.camera_input("Photograph the document", key="camera_input", label_visibility="collapsed")
same = False
if uploads and len(uploads) > 1:
    same = st.checkbox("These files are all about the same contract", key="same_contract")
if not ocr_ready:
    st.caption("Scanned PDFs and photos need text recognition, which isn't set up on this server yet — "
               "digital PDFs, Word, Excel and text files work as they are.")

files = [(u.name, u.getvalue()) for u in uploads or []]
if photo is not None:
    files.append((f"photo_{datetime.now():%Y%m%d_%H%M%S}.jpg", photo.getvalue()))

if st.button("Find my deadline", type="primary", disabled=not files, icon=":material/upload_file:", width="stretch"):
    with st.spinner("Reading your documents — this takes a few seconds per page..."):
        results = track_documents(cur, files, same_contract=same)
    conn.commit()
    for r in results:
        if r.contract_id is not None and r.contract_id not in st.session_state.my_contract_ids:
            st.session_state.my_contract_ids.insert(0, r.contract_id)
    st.session_state.last_results = [r.__dict__ for r in results]
    st.rerun()

last = st.session_state.get("last_results")
if last:
    problems = [r for r in last if r["status"] in NOT_ADDED]
    if problems:
        with st.container(border=True):
            st.markdown("**Some files couldn't be used:**")
            for r in problems:
                st.caption(f"{r['filename']} — {r['message'] or 'Unsupported or unreadable file.'}")

mine = st.session_state.my_contract_ids
if mine:
    book = portfolio(cur).set_index("id")
    st.divider()
    st.markdown("### Your results")
    for contract_id in mine:
        if contract_id not in book.index:
            continue
        row = book.loc[contract_id]
        ends = row["current_expiry_estimate"] if isinstance(row["current_expiry_estimate"], str) else None
        has_evidence = row["stage"] != "TENDER" and row["expiry_confidence"] in PILL_CLASS

        # A real Streamlit container, not a hand-written <div> — Streamlit
        # renders each st.markdown() call as its own sibling element, so an
        # opening tag in one call and a closing tag in a later one never
        # actually wraps the widgets in between; only st.container() does.
        with st.container(border=True, key=f"result_{contract_id}"):
            st.markdown(f'<div class="result-title">{row["title"]}</div>', unsafe_allow_html=True)
            c1, c2 = st.columns([2, 1])
            with c1:
                if ends:
                    st.markdown('<div class="result-end-label">Contract ends</div>', unsafe_allow_html=True)
                    st.markdown(f'<div class="result-end">{ends}</div>', unsafe_allow_html=True)
                else:
                    st.markdown('<div class="result-end-label">Status</div>', unsafe_allow_html=True)
                    st.markdown(f'<div class="result-end" style="font-size:1.2rem;">'
                               f'{status_label(row["status"], row["stage"])}</div>', unsafe_allow_html=True)
                buyer = row["buyer"]
                if isinstance(buyer, str) and buyer.strip():
                    st.markdown(f'<div class="result-meta">Buyer: {buyer.split("||")[0].strip()}</div>',
                               unsafe_allow_html=True)
            with c2:
                if has_evidence:
                    pill = PILL_CLASS[row["expiry_confidence"]]
                    st.markdown(f'<span class="{pill}">{evidence_label(row["expiry_confidence"], row["stage"])} '
                               f'evidence</span>', unsafe_allow_html=True)
                    ceiling = row["current_expiry_ceiling"]
                    if isinstance(ceiling, str) and ceiling != ends:
                        st.caption(f"Latest possible: {ceiling}")
                elif pd.notna(row["period_months"]):
                    st.caption(f"Contract period once awarded: {period_label(row['period_months'])}")

            with st.expander("See the proof"):
                calc = db.get_latest_calculation(cur, contract_id)
                fact_ids = json.loads(calc["input_fact_ids"]) if calc and calc["input_fact_ids"] else []
                all_facts = db.get_facts_for_contract(cur, contract_id, include_superseded=True)
                shown = ([f for f in all_facts if f["id"] in fact_ids]
                        or [f for f in all_facts if not f["superseded_by_fact_id"]])
                if not shown:
                    st.caption("No usable dates were found in this document.")
                for f in shown:
                    st.write(f"**{f['fact_type'].replace('_', ' ').capitalize()}:** “{f['evidence_quote']}”")
                if db.list_open_conflicts(cur, contract_id):
                    st.warning("Your documents disagree on at least one date — add the missing document to "
                               "settle it, or treat this as provisional.")
                packet = build_proof_packet_text(cur, contract_id)
                st.download_button("Download full Proof Packet", data=packet,
                                   file_name=f"proof_packet_{contract_id}.txt", key=f"dl_{contract_id}")

st.markdown(
    '<div class="trust-note">Nothing is estimated silently — every date is read from your documents, '
    'and you can see the exact sentence it came from.</div>',
    unsafe_allow_html=True,
)

close_page_connections()
