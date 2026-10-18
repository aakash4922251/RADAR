"""
Contract Expiry Radar — internal dashboard (us, not the public).

Everything: the full portfolio, every extracted fact and the rule that
found it, conflict resolution, contract removal, and a database
backup/restore so an upload survives the hosting platform restarting.

Gated by a shared password (ADMIN_PASSWORD) — set before deploying, see
README → Deploy. This is a simple shared-secret gate, not individual
accounts; rely on the host serving over HTTPS for the password itself.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import pipeline
from core.contract_tracker import portfolio, reread_all, track_documents
from core.ingestion import SUPPORTED_EXTENSIONS, ocr_available
from core.proof_packet import build_proof_packet_text
from db import database as db
from ui_labels import buyer_label, evidence_label, period_label, status_label
from ui_shared import close_page_connections, get_cursor, get_setting, init_page
from ui_theme import apply_theme

st.set_page_config(page_title="Internal Dashboard — Contract Expiry Radar",
                   page_icon=":material/admin_panel_settings:", layout="wide")
apply_theme(mode="internal")

ADMIN_PASSWORD = get_setting("ADMIN_PASSWORD")
if "internal_authed" not in st.session_state:
    st.session_state.internal_authed = False

if not st.session_state.internal_authed:
    st.markdown('<div class="radar-brand">CONTRACT EXPIRY RADAR</div>'
               '<div class="radar-brand-sub">Internal dashboard</div>', unsafe_allow_html=True)
    if not ADMIN_PASSWORD:
        st.error("No ADMIN_PASSWORD is set on this server, so this page is locked for everyone, including you. "
                 "Set the ADMIN_PASSWORD secret (see README → Deploy) and reload.")
        st.stop()
    entered = st.text_input("Password", type="password", key="internal_pw")
    if st.button("Enter", type="primary") and entered:
        if entered == ADMIN_PASSWORD:
            st.session_state.internal_authed = True
            st.rerun()
        else:
            st.error("Wrong password.")
    st.stop()

init_page()

UPLOAD_TYPES = sorted(ext.lstrip(".") for ext in SUPPORTED_EXTENSIONS) + ["zip"]
RESULT_LABELS = {"ADDED": "Added", "ALREADY_TRACKED": "Already tracked", "FAILED": "Could not read",
                 "SKIPPED": "Not added"}


def render_contract(cur, conn, contract_id: int, row: dict, today):
    contract = db.get_contract(cur, contract_id)
    st.markdown(f"### {contract['title']}")
    st.caption(f"Buyer: {buyer_label(row['buyer'])}  ·  Supplier: {row['vendor'] or 'Not stated'}")
    ends = contract["current_expiry_estimate"]
    cols = st.columns(4)
    cols[0].metric("Contract ends", ends or ("Not awarded yet" if row["stage"] == "TENDER" else "Unknown"),
                   delta=f"{(datetime.fromisoformat(ends).date() - today).days} days left" if ends else None,
                   delta_color="off")
    cols[1].metric("Contract period", period_label(row["period_months"]))
    cols[2].metric("Latest possible end", contract["current_expiry_ceiling"] or "—",
                   help="End date if every extension option in the documents were used.")
    cols[3].metric("Evidence", evidence_label(row["expiry_confidence"], row["stage"]))

    proof, evidence, documents = st.tabs(["Proof", "Evidence", "Documents"])
    with proof:
        packet = build_proof_packet_text(cur, contract_id)
        st.code(packet, language=None)
        st.download_button("Download Proof Packet", data=packet, file_name=f"proof_packet_contract_{contract_id}.txt",
                           icon=":material/download:")
    with evidence:
        for conflict in db.list_open_conflicts(cur, contract_id):
            a = cur.execute("SELECT * FROM extracted_facts WHERE id = ?", (conflict["fact_id_a"],)).fetchone()
            b = cur.execute("SELECT * FROM extracted_facts WHERE id = ?", (conflict["fact_id_b"],)).fetchone()
            with st.container(border=True):
                st.warning(f"The documents disagree on {conflict['fact_type'].replace('_', ' ')}.")
                ca, cb = st.columns(2)
                ca.write(f"**{a['fact_value']}** — \"{a['evidence_quote']}\"")
                cb.write(f"**{b['fact_value']}** — \"{b['evidence_quote']}\"")
                note = st.text_input("How was this settled?", key=f"resolve_note_{conflict['id']}")
                if st.button("Mark settled", key=f"resolve_{conflict['id']}"):
                    db.resolve_conflict(cur, conflict["id"], note or "Settled by reviewer")
                    pipeline.recalculate_contract(cur, contract_id)
                    conn.commit()
                    st.rerun()
        for f in db.get_facts_for_contract(cur, contract_id, include_superseded=True):
            replaced = " (replaced by stronger evidence)" if f["superseded_by_fact_id"] else ""
            with st.expander(f"{f['fact_type'].replace('_', ' ')}: {f['fact_value']} [{f['fact_confidence']}]{replaced}"):
                doc = db.get_document(cur, f["document_id"])
                st.write(f"\"{f['evidence_quote']}\"")
                st.caption(f"From {doc['doc_title'] or 'uploaded document'} ({doc['doc_type']}) · rule {f['extraction_rule_id']}")
    with documents:
        for d in db.list_documents_for_contract(cur, contract_id):
            with st.expander(f"{d['doc_title'] or 'Uploaded document'} — {d['doc_type']}"):
                st.text_area("Text read from the document", d["raw_text"], height=200, key=f"rawtext_{d['id']}")


def _sqlite_backup_bytes() -> bytes:
    """A consistent snapshot of the live SQLite file, taken via sqlite3's own
    backup API so it is correct even with WAL-mode writes in flight."""
    import sqlite3
    src = sqlite3.connect(db.DEFAULT_DB_PATH)
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        dst = sqlite3.connect(tmp_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        src.close()
        os.remove(tmp_path)


def _remove_with_retry(path: str, attempts: int = 5) -> None:
    """Windows can hold a file's handle open for a moment after a SQLite
    connection is closed (observed via Python's own traceback objects
    keeping the connection alive) — retry briefly rather than crash."""
    import time
    for attempt in range(attempts):
        try:
            os.remove(path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.2)


def _restore_sqlite(data: bytes) -> None:
    import gc
    import sqlite3

    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    with open(tmp_path, "wb") as f:
        f.write(data)
    try:
        check = sqlite3.connect(tmp_path)
        try:
            tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            check.close()
    except sqlite3.DatabaseError:
        tables = set()
    if "contracts" not in tables:
        os.remove(tmp_path)
        raise ValueError("That file doesn't look like a Contract Expiry Radar backup (no 'contracts' table).")

    close_page_connections()
    gc.collect()  # drop any lingering reference (e.g. a stored traceback) to the old connection
    for suffix in ("", "-wal", "-shm"):
        stale = db.DEFAULT_DB_PATH + suffix
        if os.path.exists(stale):
            _remove_with_retry(stale)
    os.replace(tmp_path, db.DEFAULT_DB_PATH)


cur, conn = get_cursor()
today = datetime.now().date()

st.markdown('<div class="radar-brand">CONTRACT EXPIRY RADAR</div>'
           '<div class="radar-brand-sub">Internal dashboard — every contract, every document, every rule.</div>',
           unsafe_allow_html=True)

with st.expander("Database backup", icon=":material/backup:"):
    st.caption("Most free hosting does not guarantee the app's local disk survives a restart. Download a backup "
               "after important uploads; restore it if the live database ever comes back empty.")
    bcol, rcol = st.columns(2)
    if bcol.button("Prepare a backup", icon=":material/backup:"):
        st.session_state.backup_bytes = _sqlite_backup_bytes()
        st.session_state.backup_stamp = f"{datetime.now():%Y-%m-%d_%H%M}"
    if st.session_state.get("backup_bytes"):
        bcol.download_button("Download backup", data=st.session_state.backup_bytes,
                             file_name=f"contract_expiry_backup_{st.session_state.backup_stamp}.db",
                             icon=":material/download:")
    restore_file = rcol.file_uploader("Restore from a backup file", type=["db"], key="restore_upload")
    if restore_file is not None:
        st.warning("This replaces every contract currently in the live database with what's in this file.")
        if st.checkbox("Yes, replace the live database", key="restore_ok") and st.button("Restore now", type="primary"):
            try:
                _restore_sqlite(restore_file.getvalue())
                st.success("Restored. Reloading...")
                st.rerun()
            except (ValueError, OSError) as exc:
                st.error(f"Restore failed: {exc}")

with st.expander("Add documents", icon=":material/upload_file:"):
    ocr_ready = ocr_available()
    uploads = st.file_uploader("Documents", type=UPLOAD_TYPES, accept_multiple_files=True, key="uploads")
    photo = None
    if ocr_ready and st.toggle("Take a photo instead", key="camera",
                               help="Hold the page flat, fill the frame, avoid glare."):
        photo = st.camera_input("Photograph the document", key="camera_input")
    same = st.checkbox("These files are all about one contract", key="same_contract",
                       help="Files that share a tender ID or tender reference number are linked automatically.")
    if not ocr_ready:
        st.caption("Scanned PDFs and photos need text recognition (OCR), which is not installed on this computer.")
    files = [(u.name, u.getvalue()) for u in uploads or []]
    if photo is not None:
        files.append((f"photo_{datetime.now():%Y%m%d_%H%M%S}.jpg", photo.getvalue()))
    if st.button("Read documents", type="primary", disabled=not files, icon=":material/add:"):
        with st.spinner("Reading documents (scans and photos take a few seconds per page)..."):
            results = track_documents(cur, files, same_contract=same)
        conn.commit()
        st.session_state.last_results = [r.__dict__ for r in results]
        st.rerun()
    last = st.session_state.get("last_results")
    if last:
        st.dataframe(pd.DataFrame([{
            "File": r["filename"], "Result": RESULT_LABELS[r["status"]], "Contract": r["contract_title"] or "",
            "Ends on": r["end_date"] or "—", "Note": r["message"],
        } for r in last]), width="stretch", hide_index=True)

book = portfolio(cur, today=today)
rows = {int(r["id"]): r for r in book.to_dict("records")}

if book.empty:
    st.info("No contracts yet. Add a tender notice, award letter or work order above.")
else:
    known = book["days_left"].notna()
    tender = book["stage"] == "TENDER"
    attention = book["status"].isin(["NEEDS_REVIEW", "CONFLICTING_EVIDENCE", "INSUFFICIENT_EVIDENCE"]) & ~tender
    kpis = st.columns(4)
    kpis[0].metric("Contracts", f"{len(book):,}")
    kpis[1].metric("Ending in 90 days", f"{int((known & book['days_left'].between(0, 90)).sum()):,}")
    kpis[2].metric("Tenders awaiting award", f"{int(tender.sum()):,}")
    kpis[3].metric("Need a closer look", f"{int(attention.sum()):,}")

    view = st.segmented_control("Show", ["All", "Ending in 90 days", "Need a closer look", "Tenders"],
                                default="All", key="view")
    shown = {"Ending in 90 days": book[known & book["days_left"].between(0, 90)],
             "Need a closer look": book[attention], "Tenders": book[tender]}.get(view, book)
    display = pd.DataFrame({
        "Ends on": [e if isinstance(e, str) else ("Not awarded yet" if s == "TENDER" else "Unknown")
                    for e, s in zip(shown["current_expiry_estimate"], shown["stage"])],
        "Days left": shown["days_left"],
        "Contract": shown["title"],
        "Buyer": shown["buyer"].map(buyer_label),
        "Supplier": shown["vendor"].fillna("—"),
        "Contract period": shown["period_months"].map(period_label),
        "Evidence": [evidence_label(c, s) for c, s in zip(shown["expiry_confidence"], shown["stage"])],
        "Status": [status_label(s, g) for s, g in zip(shown["status"], shown["stage"])],
        "Latest possible end": shown["current_expiry_ceiling"].fillna("—"),
    })
    st.dataframe(display, width="stretch", hide_index=True, column_config={
        "Contract": st.column_config.TextColumn(width="large"),
        "Days left": st.column_config.NumberColumn(format="%d"),
        "Latest possible end": st.column_config.TextColumn(help="End date if every extension option were used."),
    })
    c1, c2 = st.columns([1, 3])
    c1.download_button("Download (CSV)", data=display.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"contract_expiry_{today.isoformat()}.csv", mime="text/csv",
                       icon=":material/download:")
    if c2.button("Re-read all documents with the latest rules", icon=":material/refresh:",
                 help="Use after an update so documents added earlier benefit from improved reading."):
        with st.spinner("Re-reading documents..."):
            n = reread_all(cur)
        conn.commit()
        st.toast(f"Re-read {n} contract(s).")
        st.rerun()

    st.divider()

    def _choice(i):
        r = rows[i]
        ends = r["current_expiry_estimate"] if isinstance(r["current_expiry_estimate"], str) else \
            ("not awarded yet" if r["stage"] == "TENDER" else "end date unknown")
        return f"{r['title']} · {ends}"

    chosen = st.selectbox("Open a contract", list(rows), format_func=_choice, key="pick")
    if chosen:
        more = st.file_uploader("Add another document to this contract (e.g. the award letter or an extension order)",
                                type=UPLOAD_TYPES, accept_multiple_files=True, key=f"more_{chosen}")
        if more and st.button("Add to this contract", key=f"more_btn_{chosen}"):
            with st.spinner("Reading documents..."):
                track_documents(cur, [(u.name, u.getvalue()) for u in more], contract_id=chosen)
            conn.commit()
            st.rerun()
        render_contract(cur, conn, chosen, rows[chosen], today)
        with st.expander("Remove this contract", icon=":material/delete:"):
            st.write("Removes the contract and the documents uploaded for it. This cannot be undone.")
            sure = st.checkbox("Yes, remove it", key=f"remove_ok_{chosen}")
            if st.button("Remove contract", key=f"remove_{chosen}", disabled=not sure):
                pipeline.remove_contract(cur, chosen)
                conn.commit()
                st.session_state.pop("pick", None)
                st.rerun()

close_page_connections()
