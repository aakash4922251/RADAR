"""
Government Procurement Re-Opportunity Radar - minimal Streamlit UI.

Correctness over aesthetics. Three concepts are kept visibly separate everywhere:
  EXPIRY (evidence about an existing contract)  |  PREDICTION (a forecast, never a fact)  |  NEW PROCUREMENT (an observed tender)
Run:  streamlit run app.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date

import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import queries, review  # noqa: E402
from core.ingestion import AccessBlockedError, fetch_public_url  # noqa: E402
from core.pipeline import ingest_document  # noqa: E402
from core.proof_packet import build_proof_packet, fmt_date, render_text  # noqa: E402
from db import database as db  # noqa: E402

DB_PATH = os.environ.get("CER_DB_PATH", db.DEFAULT_DB_PATH)
DOC_TYPES = ["nit", "bid_document", "corrigendum", "aoc", "loa", "work_order", "signed_agreement", "site_handover_letter",
             "sla_stc", "platform_amendment", "termination_notice", "department_page", "other"]
PAGES = ["Dashboard", "Contract detail", "Predictions", "Review queue", "Ingest document", "Guide"]
STATUS_HELP = {
    "WATCH": "Existing contract, supported expiry not yet passed. Expiry is a signal, NOT a guaranteed new tender.",
    "RECOMPETE_SIGNAL": "Past cycles suggest the requirement may be procured again (a forecast).",
    "NEW_PROCUREMENT": "An actual later tender for the same requirement has been observed.",
    "EXTENDED": "An extension has been exercised (not merely optioned).",
    "EXPIRED_UNVERIFIED": "Supported expiry has passed; an unpublished extension may exist.",
    "EXPIRED": "Expired and human-verified.",
    "UNKNOWN": "No supported expiry.",
}


def _df(rows: list[dict], cols: list[str] | None = None) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    return df[cols] if (cols and not df.empty) else df


def dashboard(cur, as_of: date) -> None:
    st.header("Dashboard - contracts and their expiry evidence")
    st.caption("Expiry is EVIDENCE about an existing contract. It does not mean a new tender will appear.")
    opts = queries.filter_options(cur)
    c1, c2, c3, c4 = st.columns(4)
    text = c1.text_input("Search (title / organisation / vendor)")
    org = c2.selectbox("Organisation", [""] + opts["organisation"])
    state = c3.selectbox("State", [""] + opts["state"])
    cat = c4.selectbox("Category", [""] + opts["category"])
    c5, c6, c7, c8 = st.columns(4)
    vendor = c5.selectbox("Vendor", [""] + opts["vendor"])
    conf = c6.selectbox("Confidence", ["", "HIGH", "MEDIUM", "LOW", "UNKNOWN"])
    status = c7.selectbox("Evidence status", ["", "VERIFIED", "HIGH_CONFIDENCE", "MEDIUM_CONFIDENCE", "NEEDS_REVIEW",
                                              "CONFLICTING_EVIDENCE", "INSUFFICIENT_EVIDENCE"])
    life = c8.selectbox("Lifecycle status", [""] + list(STATUS_HELP))
    within = st.number_input("Only contracts expiring within N days (0 = no limit)", min_value=0, value=0, step=30)
    rows = queries.dashboard_rows(cur, as_of, organisation=org or None, category=cat or None, state=state or None,
                                  vendor=vendor or None, confidence=conf or None, status=status or None,
                                  lifecycle=life or None, text=text or None)
    if within:
        rows = [r for r in rows if r["days_to_expiry"] is not None and 0 <= r["days_to_expiry"] <= within]
    st.write(f"{len(rows)} contract(s)")
    if rows:
        st.dataframe(_df(rows, ["id", "title", "organisation", "state", "vendor", "category", "expiry", "days_to_expiry",
                                "confidence", "evidence_status", "lifecycle_status", "extension_state"]), use_container_width=True)
        st.download_button("Download CSV", queries.to_csv(rows), file_name="contracts.csv", mime="text/csv")


def contract_detail(cur, as_of: date) -> None:
    st.header("Contract detail")
    rows = queries.dashboard_rows(cur, as_of, order_by_expiry=False)
    if not rows:
        st.info("No contracts yet. Ingest a document or run the demo seed.")
        return
    labels = {f"#{r['id']} - {r['title']}": r["id"] for r in rows}
    cid = labels[st.selectbox("Contract", list(labels))]
    p = build_proof_packet(cur, cid, as_of)
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Current expiry", fmt_date(p["current_expiry"]) if p["current_expiry"] else "UNKNOWN")
    m2.metric("Original expiry", fmt_date(p["original_expiry"]))
    m3.metric("Lifecycle", p["lifecycle_status"])
    m4.metric("Confidence", p["confidence"])
    st.write(p["why_seeing_this"])
    if p["confidence_explanation"]:
        st.caption("Why this confidence: " + p["confidence_explanation"])
    t1, t2, t3, t4, t5, t6 = st.tabs(["Proof Packet", "Facts & evidence", "Timeline", "Documents", "Conflicts", "Prediction"])
    with t1:
        st.code(render_text(p), language="text")
    with t2:
        st.dataframe(_df([{"fact": o["label"], "value": o["value"], "page": o["page"], "quote": o["quote"],
                           "document": (o["document"] or {}).get("type"), "rule": o["rule"], "confidence": o["confidence"],
                           "superseded": o["superseded"]} for o in p["observed"]]), use_container_width=True)
        st.write("**Calculation**"); st.code(p["calculation"], language="text")
        for x in p["uncertain"]:
            st.warning(x)
    with t3:
        st.dataframe(_df(p["timeline"]), use_container_width=True)
    with t4:
        st.dataframe(_df(p["documents"]), use_container_width=True)
    with t5:
        if not p["conflicts"]:
            st.success("No open conflicts.")
        for cf in p["conflicts"]:
            st.error(f"{cf['fact_type']}: {cf['a']['value']} ({cf['a']['document']['type']}) vs {cf['b']['value']} ({cf['b']['document']['type']})")
    with t6:
        _prediction_block(p)


def _prediction_block(p: dict) -> None:
    st.caption("A prediction is a FORECAST from past behaviour. It is never a fact and never a promise that a tender will occur.")
    if not p["requirement"]:
        st.info("No requirement linked (organisation unknown), so no cycle analysis.")
        return
    st.write(f"Requirement: **{p['requirement']['title']}**")
    st.dataframe(_df(p["procurement_history"]), use_container_width=True)
    pr = p["prediction"]
    if not pr:
        st.info("No prediction: insufficient procurement history (need at least 2 dated cycles).")
        return
    st.subheader(pr["label"])
    st.write(f"Status: **{pr['status']}** | Confidence: **{pr['confidence']}** | Window: "
             f"{fmt_date(pr['window'][0])} to {fmt_date(pr['window'][1])}")
    st.write(pr["explanation"])
    if pr["resolution"]:
        st.write("Outcome: " + pr["resolution"])


def predictions_page(cur, as_of: date) -> None:
    st.header("Predicted procurement windows")
    st.warning("PREDICTED PROCUREMENT - forecasts from historical cycles. Not actual tenders. A tender is never guaranteed.")
    show = st.selectbox("Show", ["PREDICTED", "CONFIRMED", "INVALIDATED", "SUPERSEDED", "ALL"])
    rows = queries.prediction_rows(cur, None if show == "ALL" else show)
    if not rows:
        st.info("No predictions in this state.")
        return
    st.dataframe(_df(rows, ["id", "requirement", "organisation", "status", "confidence", "window_start", "window_end",
                            "expected_date", "n_cycles", "median_interval_days", "resolution_note"]), use_container_width=True)
    for r in rows:
        with st.expander(f"{r['requirement']} - {r['confidence']} - {r['window_start']} to {r['window_end']}"):
            st.write(r["explanation"])
    st.download_button("Download CSV", queries.to_csv(rows, list(rows[0].keys())), file_name="predictions.csv", mime="text/csv")


def review_page(cur, as_of: date) -> None:
    st.header("Review queue - records needing human verification")
    q = queries.review_queue(cur)
    if not q:
        st.success("Nothing needs review.")
    for r in q:
        with st.expander(f"#{r['id']} {r['title']} - {r['evidence_status']} - open conflicts: {r['open_conflicts']}"):
            p = build_proof_packet(cur, r["id"], as_of)
            st.write(p["confidence_explanation"] or "")
            for cf in p["conflicts"]:
                st.error(f"{cf['fact_type']}: A = {cf['a']['value']} ({cf['a']['document']['type']}, \"{cf['a']['quote'][:80]}\")  vs  "
                         f"B = {cf['b']['value']} ({cf['b']['document']['type']}, \"{cf['b']['quote'][:80]}\")")
                pick = st.radio("Which is correct?", [f"A (fact {cf['a']['fact_id']})", f"B (fact {cf['b']['fact_id']})"], key=f"pick{cf['id']}")
                note = st.text_input("Reason (required)", key=f"note{cf['id']}")
                if st.button("Resolve conflict", key=f"res{cf['id']}"):
                    if not note.strip():
                        st.error("A reason is required.")
                    else:
                        win = cf["a"]["fact_id"] if pick.startswith("A") else cf["b"]["fact_id"]
                        review.resolve_conflict_choose(cur, cf["id"], win, note, as_of=as_of)
                        st.success("Resolved. Reload to refresh."); 
            if not p["conflicts"] and st.button("Mark verified by human", key=f"ver{r['id']}"):
                review.mark_verified(cur, r["id"], "verified in UI", as_of=as_of)
                st.success("Marked verified.")


def ingest_page(cur, as_of: date) -> None:
    st.header("Ingest an official procurement document")
    st.caption("Upload a PDF/text file, paste text, or fetch a PUBLIC URL. Portals that require login or a CAPTCHA are never "
               "bypassed: download the document yourself and upload it.")
    mode = st.radio("Source", ["Upload file", "Paste text", "Public URL"])
    up = pasted = url = None
    if mode == "Upload file":
        up = st.file_uploader("PDF or text file", type=["pdf", "txt"])
    elif mode == "Paste text":
        pasted = st.text_area("Document text")
    else:
        url = st.text_input("Public document URL")
    doc_type = st.selectbox("Document type", DOC_TYPES, index=DOC_TYPES.index("work_order"))
    doc_date = st.text_input("Document date printed on the document (YYYY-MM-DD, optional)")
    existing = {f"#{r['id']} - {r['title']}": r["id"] for r in queries.dashboard_rows(cur, as_of, order_by_expiry=False)}
    target = st.selectbox("Attach to contract", ["(new contract)"] + list(existing))
    title = org = state = vendor = category = ""
    if target == "(new contract)":
        title = st.text_input("New contract title (required)")
        org = st.text_input("Organisation"); state = st.text_input("State"); vendor = st.text_input("Vendor (optional)")
        category = st.text_input("Category (optional; auto-detected if blank)")
    if st.button("Ingest"):
        try:
            kw = dict(doc_type=doc_type, doc_date=doc_date or None, as_of=as_of)
            if target == "(new contract)":
                kw.update(contract_title=title, org_name=org or None, state=state or None, vendor_name=vendor or None, category=category or None)
            else:
                kw.update(contract_id=existing[target])
            if mode == "Upload file" and up is not None:
                res = ingest_document(cur, raw_bytes=up.getvalue(), filename=up.name, doc_title=up.name, **kw)
            elif mode == "Paste text" and pasted:
                res = ingest_document(cur, raw_text=pasted, **kw)
            elif mode == "Public URL" and url:
                res = ingest_document(cur, ingested=fetch_public_url(url), source_url=url, doc_title=url, **kw)
            else:
                st.error("Provide a file, text or URL first."); return
            st.success(f"Ingested. Contract #{res.contract_id}: {res.n_facts_extracted} facts, evidence status {res.status}, "
                       f"confidence {res.expiry_confidence}, lifecycle {res.lifecycle_status}.")
            if res.deduplicated:
                st.info("This exact document was already ingested; nothing was duplicated.")
            for s in res.similar_contracts:
                st.warning(f"Possible duplicate of existing contract #{s['contract_id']} '{s['title']}' (similarity {s['score']}). "
                           "Nothing was merged automatically.")
            if res.reconciliation_summary["conflicts"]:
                st.error(f"{len(res.reconciliation_summary['conflicts'])} conflicting fact(s) sent to the Review queue.")
        except AccessBlockedError as e:
            st.error(f"Access blocked (not bypassed): {e}")
        except Exception as e:  # surface the reason; nothing is saved on failure
            st.error(str(e))


def guide_page(cur, as_of: date) -> None:
    st.header("How to read this")
    st.write("**Contract expiry != guaranteed new tender.** Expiry is evidence; a recompete is a signal; a new tender is an event.")
    for k, v in STATUS_HELP.items():
        st.write(f"- **{k}** - {v}")
    st.write("Not implemented: NO_RECOMPETE_EVIDENCE status; alerts (tables exist); live portal connectors (CPPP/GeM).")


def main() -> None:
    st.set_page_config(page_title="Procurement Re-Opportunity Radar", layout="wide")
    db.init_db(DB_PATH)
    st.sidebar.title("Procurement Radar")
    page = st.sidebar.radio("Page", PAGES)
    as_of = st.sidebar.date_input("As-of date", value=date.today())
    st.sidebar.caption(f"Database: {DB_PATH}")
    fn = {"Dashboard": dashboard, "Contract detail": contract_detail, "Predictions": predictions_page,
          "Review queue": review_page, "Ingest document": ingest_page, "Guide": guide_page}[page]
    with db.db_cursor(DB_PATH) as cur:
        fn(cur, as_of)


main()
