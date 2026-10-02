from __future__ import annotations

import json
from datetime import date

import streamlit as st

from acquisition.config import settings as acquisition_settings
from core.backtesting import backtest_requirement_as_of, evaluate_backtest_results
from core.proof_packet import build_proof_packet_text
from db import database as db


def _opportunities(cur):
    return cur.execute(
        """SELECT pp.*, r.asset_keyword, r.normalized_title, r.location,
              r.org_id, o.name AS org_name,
              COALESCE((SELECT alias_text FROM requirement_aliases a
                    WHERE a.requirement_id = r.id
                    ORDER BY a.created_at DESC, a.id DESC LIMIT 1), r.normalized_title) AS display_title
           FROM procurement_predictions pp
           JOIN requirements r ON r.id = pp.requirement_id
           LEFT JOIN organisations o ON o.id = r.org_id
           WHERE pp.status = 'ACTIVE'
           ORDER BY CASE pp.confidence WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END,
                    pp.predicted_date"""
    ).fetchall()


def _show_opportunity_detail(cur, opportunity):
    requirement_id = opportunity["requirement_id"]
    if st.button("Back to opportunities", icon=":material/arrow_back:"):
        st.session_state.selected_requirement_id = None
        st.rerun()

    organisation = opportunity["org_name"] or "Organisation not resolved"
    st.markdown('<div class="eyebrow">Re-opportunity signal</div>', unsafe_allow_html=True)
    st.title(opportunity["display_title"])
    latest_location = cur.execute(
        """SELECT dr.location FROM procurement_events pe
           JOIN requirement_event_links rel ON rel.event_id = pe.id
           JOIN discovered_records dr ON dr.id = pe.discovered_record_id
           WHERE rel.requirement_id = ? AND dr.location IS NOT NULL
           ORDER BY pe.event_date DESC LIMIT 1""",
        (requirement_id,),
    ).fetchone()
    location = latest_location["location"] if latest_location else opportunity["location"]
    st.caption(f"{organisation} · {location or 'Location not exposed'}")

    stats = db.get_latest_cycle(cur, requirement_id)
    events = db.list_events_for_requirement(cur, requirement_id)
    metrics = st.columns(4)
    metrics[0].metric("Expected window", f"{opportunity['window_start']} → {opportunity['window_end']}")
    metrics[1].metric("Expected date", opportunity["predicted_date"])
    metrics[2].metric("Confidence", opportunity["confidence"])
    metrics[3].metric("Observed tenders", sum(1 for event in events if event["discovered_record_id"] is not None))

    st.subheader("Why this was flagged")
    reasons = []
    if stats and stats["median_interval_days"] is not None:
        reasons.append(f"Historical cycle measured at a median of {stats['median_interval_days']:.0f} days.")
        reasons.append(f"{stats['n_cycles']} dated {stats['anchor_event_type']} observations support the estimate.")
        if stats["stddev_interval_days"] is not None:
            reasons.append(f"Observed interval standard deviation is {stats['stddev_interval_days']:.1f} days.")
    else:
        reasons.append("The requirement has insufficient dated history for a measured cycle.")
    reasons.append("The window is an inference from observed procurement dates, not a confirmed future tender.")
    for reason in reasons:
        st.markdown(f"<div class='signal-row'><span class='signal-check'>✓</span>{reason}</div>", unsafe_allow_html=True)

    st.subheader("Procurement timeline")
    dated_events = sorted((event for event in events if event["event_date"]), key=lambda event: event["event_date"])
    if dated_events:
        timeline = st.columns(min(len(dated_events), 4))
        for index, event in enumerate(dated_events):
            with timeline[index % len(timeline)]:
                st.markdown(f"<div class='timeline-date'>{event['event_date']}</div>", unsafe_allow_html=True)
                st.caption(event["event_type"].replace("_", " ").title())
    else:
        st.info("No dated procurement observations are linked yet.")

    st.subheader("Evidence trail")
    for event in events:
        link = cur.execute(
            "SELECT match_status, match_score, match_evidence FROM requirement_event_links WHERE requirement_id = ? AND event_id = ?",
            (requirement_id, event["id"]),
        ).fetchone()
        with st.expander(f"{event['event_date'] or 'Undated'} · {event['event_type'].replace('_', ' ').title()}"):
            if event["discovered_record_id"] is not None:
                tender = cur.execute(
                    "SELECT external_id, title, organisation, detail_url, source_page_url FROM discovered_records WHERE id = ?",
                    (event["discovered_record_id"],),
                ).fetchone()
                if tender:
                    st.write(f"Tender reference: {tender['external_id']}")
                    st.write(tender["title"] or "Title unavailable")
                    if tender["detail_url"]:
                        st.link_button("Official tender record", tender["detail_url"], icon=":material/open_in_new:")
                    if tender["source_page_url"]:
                        st.link_button("Source listing", tender["source_page_url"], icon=":material/open_in_new:")
                    attached = cur.execute(
                        "SELECT filename, document_url, status, sha256 FROM acquired_documents WHERE record_id = ? ORDER BY id",
                        (event["discovered_record_id"],),
                    ).fetchall()
                    for document in attached:
                        st.write(
                            f"{document['filename'] or 'Tender document'} · {document['status']} · "
                            f"SHA-256: {document['sha256'] or 'not available'}"
                        )
                        st.link_button("Open tender document", document["document_url"], icon=":material/description:")
            if event["document_id"] is not None:
                document = db.get_document(cur, event["document_id"])
                if document:
                    st.write(f"Contract evidence: {document['doc_title'] or document['doc_type']}")
                    if document["source_url"]:
                        st.link_button("Open source document", document["source_url"], icon=":material/open_in_new:")
            if link:
                st.write(f"Requirement identity: {link['match_status']} · score {link['match_score']:.2f}")
                st.json(json.loads(link["match_evidence"]))


def render_opportunities(cur):
    st.markdown('<div class="eyebrow">Procurement intelligence</div>', unsafe_allow_html=True)
    st.title("Re-opportunity radar")
    st.caption("Potential recurring procurements inferred from dated history. This is not a guarantee that a tender will be issued.")
    opportunities = _opportunities(cur)
    selected_id = st.session_state.get("selected_requirement_id")
    selected = next((row for row in opportunities if row["requirement_id"] == selected_id), None)
    if selected:
        _show_opportunity_detail(cur, selected)
        return

    search = st.text_input("Find an opportunity", placeholder="Requirement, organisation, or service")
    confidence_filter = st.selectbox("Evidence confidence", ["All", "HIGH", "MEDIUM", "LOW"])
    filtered = [row for row in opportunities if
                (confidence_filter == "All" or row["confidence"] == confidence_filter)
                and (not search or search.lower() in " ".join(str(row[key] or "") for key in ("display_title", "normalized_title", "org_name", "asset_keyword")).lower())]
    if not filtered:
        st.info("No matching opportunities. A requirement needs at least two dated procurement observations before a cycle can be estimated.")
        if st.button("Review tenders", icon=":material/description:"):
            st.session_state.active_page = "Tenders"
            st.rerun()
        return

    for offset in range(0, len(filtered), 2):
        columns = st.columns(2)
        for column, opportunity in zip(columns, filtered[offset:offset + 2]):
            with column:
                with st.container(border=True):
                    st.write(opportunity["display_title"])
                    st.caption(f"{opportunity['org_name'] or 'Organisation not resolved'} · {opportunity['asset_keyword']}")
                    st.markdown(
                        f"<span class='opportunity-window'>Expected · {opportunity['window_start']} to {opportunity['window_end']}</span>",
                        unsafe_allow_html=True,
                    )
                    stats = db.get_latest_cycle(cur, opportunity["requirement_id"])
                    cycle = f"{stats['median_interval_days']:.0f} day median" if stats and stats["median_interval_days"] is not None else "Cycle unavailable"
                    st.write(f"{cycle} · {opportunity['confidence']} evidence confidence")
                    if st.button("View signal", key=f"open_opportunity_{opportunity['requirement_id']}", icon=":material/arrow_forward:"):
                        st.session_state.selected_requirement_id = opportunity["requirement_id"]
                        st.rerun()


def render_tenders(cur):
    st.markdown('<div class="eyebrow">Official-source observations</div>', unsafe_allow_html=True)
    st.title("Tenders")
    st.caption("Tender notices and their source documents are tracked independently from contract records.")
    records = db.list_discovered_records(cur, limit=500)
    query = st.text_input("Search tenders", placeholder="Title, reference, organisation, or location")
    status_values = ["All statuses"] + sorted({record["acquisition_status"] for record in records})
    status = st.selectbox("Acquisition status", status_values)
    filtered = [record for record in records if
                (status == "All statuses" or record["acquisition_status"] == status)
                and (not query or query.lower() in " ".join(str(record[key] or "") for key in ("title", "external_id", "organisation", "location")).lower())]
    st.caption(f"{len(filtered)} tender record(s)")
    if not filtered:
        st.info("No tender records match these filters.")
        return
    for record in filtered:
        title = record["title"] or record["external_id"]
        with st.expander(f"{title} · {record['external_id']} · {record['acquisition_status']}"):
            fields = st.columns(4)
            fields[0].write(f"Organisation\n\n{record['organisation'] or 'Not exposed'}")
            fields[1].write(f"Location\n\n{record['location'] or 'Not exposed'}")
            fields[2].write(f"Published\n\n{record['published_at'] or 'Not exposed'}")
            fields[3].write(f"Closing\n\n{record['closing_at'] or 'Not exposed'}")
            if record["detail_url"]:
                st.link_button("Official tender record", record["detail_url"], icon=":material/open_in_new:")
            documents = cur.execute(
                "SELECT filename, document_url, status, sha256 FROM acquired_documents WHERE record_id = ? ORDER BY id",
                (record["id"],),
            ).fetchall()
            if documents:
                st.write("Acquired tender documents")
                for document in documents:
                    st.write(f"{document['filename'] or 'Document'} · {document['status']} · SHA-256 {document['sha256'] or 'pending'}")
                    st.link_button("Open document source", document["document_url"], icon=":material/description:")
            linked_events = cur.execute(
                """SELECT pe.event_date, rel.requirement_id
                   FROM procurement_events pe
                   JOIN requirement_event_links rel ON rel.event_id = pe.id
                   WHERE pe.discovered_record_id = ?""",
                (record["id"],),
            ).fetchall()
            if linked_events:
                st.write("Linked requirement history")
                for event in linked_events:
                    requirement = db.get_requirement(cur, event["requirement_id"]) if event["requirement_id"] else None
                    if requirement:
                        st.write(f"{event['event_date']} · {requirement['normalized_title']}")


def render_organisations(cur):
    st.markdown('<div class="eyebrow">Buyer intelligence</div>', unsafe_allow_html=True)
    st.title("Organisations")
    rows = cur.execute(
        """SELECT o.id, o.name,
                  (SELECT COUNT(*) FROM requirements r WHERE r.org_id = o.id) AS requirement_count,
                  (SELECT COUNT(*) FROM contracts c WHERE c.org_id = o.id) AS contract_count,
                  (SELECT COUNT(*) FROM discovered_records d WHERE d.organisation = o.name
                    OR d.organisation LIKE o.name || '||%') AS tender_count
           FROM organisations o ORDER BY o.name"""
    ).fetchall()
    query = st.text_input("Search organisations")
    rows = [row for row in rows if not query or query.lower() in row["name"].lower()]
    if not rows:
        st.info("Organisations appear as source records and contracts are linked.")
        return
    st.dataframe([
        {"Organisation": row["name"], "Requirements": row["requirement_count"],
         "Contracts": row["contract_count"], "Tender observations": row["tender_count"]}
        for row in rows
    ], use_container_width=True, hide_index=True)


def render_documents(cur):
    st.markdown('<div class="eyebrow">Source library</div>', unsafe_allow_html=True)
    st.title("Documents")
    tabs = st.tabs(["Tender documents", "Contract evidence"])
    with tabs[0]:
        rows = cur.execute(
            """SELECT ad.*, dr.external_id, dr.title AS tender_title
               FROM acquired_documents ad JOIN discovered_records dr ON dr.id = ad.record_id
               ORDER BY ad.discovered_at DESC LIMIT 300"""
        ).fetchall()
        if rows:
            st.dataframe([
                {"Tender": row["tender_title"] or row["external_id"], "File": row["filename"] or "Unknown",
                 "Status": row["status"], "Downloaded": row["downloaded_at"] or "Not downloaded",
                 "SHA-256": row["sha256"] or "Not available", "Source URL": row["document_url"]}
                for row in rows
            ], use_container_width=True, hide_index=True)
        else:
            st.info("No tender documents have been acquired yet.")
    with tabs[1]:
        rows = cur.execute(
            """SELECT d.id, d.doc_type, d.doc_title, d.doc_date, d.source_url, c.title AS contract_title
               FROM documents d LEFT JOIN contracts c ON c.id = d.contract_id
               ORDER BY d.fetched_at DESC LIMIT 300"""
        ).fetchall()
        if rows:
            st.dataframe([
                {"Contract": row["contract_title"] or "Unlinked", "Type": row["doc_type"],
                 "Document": row["doc_title"] or "Untitled", "Date": row["doc_date"] or "Unknown",
                 "Source URL": row["source_url"] or "Uploaded"}
                for row in rows
            ], use_container_width=True, hide_index=True)
        else:
            st.info("No contract documents have been ingested.")


def render_evidence(cur):
    st.markdown('<div class="eyebrow">Traceable conclusions</div>', unsafe_allow_html=True)
    st.title("Evidence")
    tender_rows = cur.execute(
        """SELECT pe.id, pe.event_date, pe.discovered_record_id, pe.document_id,
                  dr.external_id, dr.title, rel.requirement_id, rel.match_status,
                  rel.match_score, rel.match_evidence
           FROM procurement_events pe
           JOIN requirement_event_links rel ON rel.event_id = pe.id
           LEFT JOIN discovered_records dr ON dr.id = pe.discovered_record_id
           ORDER BY pe.event_date DESC LIMIT 250"""
    ).fetchall()
    if tender_rows:
        for row in tender_rows:
            label = f"{row['event_date'] or 'Undated'} · {row['external_id'] or 'Contract evidence'} · {row['title'] or 'Procurement event'}"
            with st.expander(label):
                st.write(f"Link status: {row['match_status']} · score {row['match_score']:.2f}")
                st.json(json.loads(row["match_evidence"]))
                if row["discovered_record_id"]:
                    docs = cur.execute(
                        "SELECT filename, document_url, sha256, status FROM acquired_documents WHERE record_id = ?",
                        (row["discovered_record_id"],),
                    ).fetchall()
                    for doc in docs:
                        st.write(f"{doc['filename'] or 'Tender document'} · {doc['status']} · SHA-256 {doc['sha256'] or 'pending'}")
                        st.link_button("Open official document", doc["document_url"], icon=":material/open_in_new:")
    else:
        st.info("No linked procurement evidence is available yet.")

    st.subheader("Prediction signal trail")
    predictions = cur.execute(
        """SELECT p.prediction_id, p.predicted_window_start, p.predicted_window_end,
                  p.confidence, p.prediction_status, r.normalized_title
           FROM predictions p JOIN requirements r ON r.id = p.requirement_id
           WHERE p.prediction_id = (
               SELECT MAX(latest.prediction_id) FROM predictions latest
               WHERE latest.requirement_id = p.requirement_id
           ) ORDER BY p.created_at DESC"""
    ).fetchall()
    if predictions:
        for prediction in predictions:
            with st.expander(f"{prediction['normalized_title']} · {prediction['confidence']} · {prediction['prediction_status']}"):
                st.write(
                    f"Inferred window: {prediction['predicted_window_start'] or 'Unavailable'} to "
                    f"{prediction['predicted_window_end'] or 'Unavailable'}"
                )
                signals = cur.execute(
                    "SELECT signal_name, signal_value, signal_detail FROM prediction_evidence WHERE prediction_id = ? ORDER BY id",
                    (prediction["prediction_id"],),
                ).fetchall()
                for signal in signals:
                    st.write(f"{signal['signal_name']}: {signal['signal_value']} · {signal['signal_detail']}")
    else:
        st.info("No contract-lifecycle prediction signals have been recorded.")

    st.subheader("Contract proof packets")
    contracts = db.list_contracts(cur)
    if contracts:
        contract_id = st.selectbox(
            "Select a contract", [row["id"] for row in contracts],
            format_func=lambda selected: next(row["title"] for row in contracts if row["id"] == selected),
        )
        st.code(build_proof_packet_text(cur, contract_id), language=None)
    else:
        st.info("Contract proof packets appear when contract documents are ingested.")


def render_sources(cur):
    st.markdown('<div class="eyebrow">Monitoring configuration</div>', unsafe_allow_html=True)
    st.title("Sources")
    config = acquisition_settings()
    sources = db.list_acquisition_sources(cur)
    if sources:
        st.dataframe([
            {"Source": row["name"], "Status": row["status"], "Enabled": bool(row["enabled"]),
             "Last scan": row["last_scan_at"] or "Never", "Last success": row["last_success_at"] or "Never"}
            for row in sources
        ], use_container_width=True, hide_index=True)
    else:
        st.info("No procurement sources have completed a scan yet.")
    st.subheader("CPPP source settings")
    cols = st.columns(3)
    cols[0].metric("Acquisition", "Enabled" if config["enabled"] else "Paused")
    cols[1].metric("CPPP connector", "Enabled" if config["cppp_enabled"] else "Paused")
    cols[2].metric("Polling interval", f"{config['poll_interval_minutes']} min")
    st.write(f"Search URL: {config['cppp_search_url']}")
    st.caption("Settings are read from environment variables. Restart the app or worker after changing them.")


def render_alerts(cur):
    st.markdown('<div class="eyebrow">Items requiring attention</div>', unsafe_allow_html=True)
    st.title("Alerts")
    manual = db.list_discovered_records(cur, acquisition_status="MANUAL_ACTION_REQUIRED")
    queue = db.list_review_queue(cur)
    first, second = st.columns(2)
    first.metric("Manual source actions", len(manual))
    second.metric("Contracts for review", len(queue))
    st.subheader("Source access or document blocks")
    if manual:
        st.dataframe([
            {"Tender": row["title"] or row["external_id"], "Organisation": row["organisation"] or "Unknown",
             "Reason": row["last_error"] or "Manual retrieval required", "Detail page": row["detail_url"] or "Not available"}
            for row in manual
        ], use_container_width=True, hide_index=True)
    else:
        st.success("No tender records currently require manual source action.")
    st.subheader("Contract evidence review")
    if queue:
        st.dataframe([
            {"Contract": row["title"], "Status": row["status"], "Expiry": row["current_expiry_estimate"] or "Unknown"}
            for row in queue
        ], use_container_width=True, hide_index=True)
    else:
        st.success("No contract evidence conflicts are waiting for review.")


def render_analytics(cur):
    st.markdown('<div class="eyebrow">Portfolio measurement</div>', unsafe_allow_html=True)
    st.title("Analytics")
    tenders = cur.execute("SELECT COUNT(*) FROM discovered_records").fetchone()[0]
    events = cur.execute("SELECT COUNT(*) FROM procurement_events WHERE event_date IS NOT NULL").fetchone()[0]
    requirements = len(db.list_requirements(cur))
    contracts = len(db.list_contracts(cur))
    metrics = st.columns(4)
    metrics[0].metric("Tender records", tenders)
    metrics[1].metric("Dated observations", events)
    metrics[2].metric("Requirements tracked", requirements)
    metrics[3].metric("Contracts represented", contracts)
    st.subheader("Observed procurement events by month")
    rows = cur.execute(
        """SELECT substr(event_date, 1, 7) AS month, COUNT(*) AS count
           FROM procurement_events WHERE event_date IS NOT NULL
           GROUP BY substr(event_date, 1, 7) ORDER BY month"""
    ).fetchall()
    if rows:
        st.bar_chart({"Month": [row["month"] for row in rows], "Events": [row["count"] for row in rows]}, x="Month", y="Events", color="#2876c7")
    else:
        st.info("Analytics will populate from dated tender and contract events.")


def render_backtesting(cur):
    st.markdown('<div class="eyebrow">Forecast evaluation</div>', unsafe_allow_html=True)
    st.title("Backtesting")
    st.caption("Choose a historical as-of date. Later procurement observations are held out as outcomes.")
    requirements = db.list_requirements(cur)
    if not requirements:
        st.info("Backtesting becomes available when requirements have dated procurement histories.")
        return
    requirement_id = st.selectbox(
        "Requirement", [row["id"] for row in requirements],
        format_func=lambda selected: next(row["normalized_title"] for row in requirements if row["id"] == selected),
    )
    as_of = st.date_input("Pretend the radar is standing on", value=date.today())
    if st.button("Run walk-forward test", type="primary", icon=":material/play_arrow:"):
        result = backtest_requirement_as_of(cur, requirement_id, as_of)
        report = evaluate_backtest_results(cur, [result], minimum_dataset_size=10)
        cols = st.columns(4)
        cols[0].metric("History available", result["used_events"])
        cols[1].metric("Next observed tender", result["actual_event_date"] or "None")
        cols[2].metric("Window result", result["match_status"])
        cols[3].metric("Evaluation", "More history needed" if report["precision"] is None else f"{report['precision']:.0%} precision")
        st.write(f"As of {result['as_of_date']}, forecast window: "
                 f"{result['prediction']['predicted_window_start'] or 'Unavailable'} to "
                 f"{result['prediction']['predicted_window_end'] or 'Unavailable'}.")
        st.caption("Single-requirement results are diagnostic only; aggregate performance is not reported below the minimum sample size.")


def render_settings():
    st.markdown('<div class="eyebrow">System configuration</div>', unsafe_allow_html=True)
    st.title("Settings")
    config = acquisition_settings()
    st.subheader("Procurement source")
    st.write(f"CPPP search URL: {config['cppp_search_url']}")
    st.write(f"Acquisition: {'enabled' if config['enabled'] else 'paused'}")
    st.write(f"CPPP connector: {'enabled' if config['cppp_enabled'] else 'paused'}")
    st.write(f"Polling interval: {config['poll_interval_minutes']} minutes")
    st.write(f"Request timeout: {config['request_timeout']} seconds")
    st.write(f"Maximum retries: {config['max_retries']}")
    st.write(f"Document storage: {config['storage_root']}")
    st.caption("Values are environment-backed; edit the deployment environment and restart the app to change them.")