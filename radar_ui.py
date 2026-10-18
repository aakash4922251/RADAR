from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import streamlit as st

from acquisition.config import settings as acquisition_settings
from core.backtesting import backtest_requirement_as_of, evaluate_backtest_results
from core.proof_packet import build_proof_packet_text
from db import database as db
from ui_theme import environment_for_hour


def greeting_for_hour(hour: int, name: str = "there") -> str:
    greeting = "Good morning" if 5 <= hour < 12 else "Good afternoon" if 12 <= hour < 17 else "Good evening"
    return f"{greeting}, {name}"


def evidence_label(confidence: str) -> str:
    return {"HIGH": "Strong evidence", "MEDIUM": "Growing evidence", "LOW": "Early signal"}.get(
        (confidence or "").upper(), "Not enough information yet"
    )


def contract_status_label(status: str) -> str:
    labels = {
        "CONFLICTING_EVIDENCE": "We found conflicting information",
        "INSUFFICIENT_EVIDENCE": "Not enough information yet",
        "HIGH_CONFIDENCE": "Strong supporting evidence",
        "VERIFIED": "Reviewed information",
        "NEEDS_REVIEW": "Needs a closer look",
        "EXPIRED_UNVERIFIED": "May have ended",
        "EXPIRED": "Recorded end date has passed",
        "EXTENDED": "Extension recorded",
        "CANCELLED": "Cancellation recorded",
        "UNKNOWN": "Status not known yet",
    }
    return labels.get(status, "Contract information available")


def acquisition_status_label(status: str) -> str:
    return {
        "TENDER_EVIDENCE_ONLY": "Notice collected",
        "DISCOVERED_METADATA_ONLY": "Notice recorded",
        "MANUAL_ACTION_REQUIRED": "Needs a manual check",
        "FAILED": "Could not collect document",
        "DUPLICATE": "Already collected",
        "PROCESSED": "Notice recorded",
        "DISCOVERED": "Notice found",
        "PENDING": "Waiting to collect",
    }.get(status, "Notice recorded")


def buyer_display_name(value: str | None) -> str:
    if not value:
        return "Organisation not listed"
    return " · ".join(part.strip() for part in value.split("||") if part.strip())


def format_opportunity_window(start: str | None, end: str | None) -> str:
    try:
        start_date = date.fromisoformat(str(start)[:10])
        end_date = date.fromisoformat(str(end)[:10])
    except (TypeError, ValueError):
        return "Expected timing is still being worked out"
    first = start_date.strftime("%b")
    last = end_date.strftime("%b")
    if start_date.year == end_date.year:
        months = first if first == last else f"{first}-{last}"
        return f"{months} {end_date.year}"
    return f"{first} {start_date.year} - {last} {end_date.year}"


def opportunity_buckets(opportunity, today: date | None = None) -> set[str]:
    today = today or date.today()
    buckets = {"All"}
    try:
        predicted = date.fromisoformat(str(opportunity["predicted_date"])[:10])
        window_start = date.fromisoformat(str(opportunity["window_start"])[:10])
        window_end = date.fromisoformat(str(opportunity["window_end"])[:10])
    except (TypeError, ValueError):
        return buckets | {"Watching"}

    soon_edge = today + timedelta(days=90)
    if window_end >= today and window_start <= soon_edge:
        buckets.add("Coming Soon")
    else:
        buckets.add("Watching")

    observed = opportunity["latest_tender_date"] if "latest_tender_date" in opportunity.keys() else None
    if observed:
        try:
            observed_date = date.fromisoformat(str(observed)[:10])
            if today - timedelta(days=14) <= observed_date <= today:
                buckets.add("New")
        except ValueError:
            pass
    return buckets


def human_age(value: str | None, now: datetime | None = None) -> str:
    if not value:
        return "No scan recorded yet"
    try:
        then = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        current = now or datetime.now().astimezone()
        if then.tzinfo is None:
            then = then.replace(tzinfo=current.tzinfo)
        minutes = max(0, int((current - then).total_seconds() // 60))
    except (ValueError, TypeError):
        return "Last scan time unavailable"
    if minutes < 1:
        return "Just checked"
    if minutes < 60:
        return f"Checked {minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"Checked {hours} hr ago"
    days = hours // 24
    return f"Checked {days} day{'s' if days != 1 else ''} ago"


def timestamp_is_recent(value: str | None, *, within_hours: int = 24, now: datetime | None = None) -> bool:
    if not value:
        return False
    try:
        then = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        current = now or datetime.now().astimezone()
        if then.tzinfo is None:
            then = then.replace(tzinfo=current.tzinfo)
        age = (current - then).total_seconds()
        return 0 <= age <= within_hours * 3600
    except (ValueError, TypeError):
        return False


def history_timeline_years(event_dates: list[str], current_year: int | None = None) -> list[str]:
    current_year = current_year or date.today().year
    observed_years = sorted({str(value)[:4] for value in event_dates if value and len(str(value)) >= 4})
    years = observed_years[-4:]
    this_year = str(current_year)
    if this_year not in years:
        years.append(this_year)
    return years


def _opportunities(cur):
    return cur.execute(
        """SELECT pp.*, r.asset_keyword, r.normalized_title, r.location,
              r.org_id, o.name AS org_name,
              COALESCE((SELECT alias_text FROM requirement_aliases a
                    WHERE a.requirement_id = r.id
                    ORDER BY a.created_at DESC, a.id DESC LIMIT 1), r.normalized_title) AS display_title,
              (SELECT MAX(pe.event_date) FROM procurement_events pe
                    JOIN requirement_event_links rel ON rel.event_id = pe.id
                    WHERE rel.requirement_id = r.id
                      AND pe.discovered_record_id IS NOT NULL) AS latest_tender_date,
              (SELECT dr.location FROM procurement_events pe
                    JOIN requirement_event_links rel ON rel.event_id = pe.id
                    JOIN discovered_records dr ON dr.id = pe.discovered_record_id
                    WHERE rel.requirement_id = r.id AND dr.location IS NOT NULL
                    ORDER BY pe.event_date DESC LIMIT 1) AS observed_location
           FROM procurement_predictions pp
           JOIN requirements r ON r.id = pp.requirement_id
           LEFT JOIN organisations o ON o.id = r.org_id
           WHERE pp.status = 'ACTIVE'
           ORDER BY CASE pp.confidence WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END,
                    pp.predicted_date"""
    ).fetchall()


def _show_opportunity_detail(cur, opportunity):
    requirement_id = opportunity["requirement_id"]
    if st.button("Back to Radar", icon=":material/arrow_back:"):
        st.session_state.selected_requirement_id = None
        st.rerun()

    organisation = opportunity["org_name"] or "Organisation not resolved"
    st.markdown('<div class="eyebrow">A possible repeat procurement</div>', unsafe_allow_html=True)
    st.title(opportunity["display_title"])
    location = opportunity["observed_location"] or opportunity["location"]
    st.caption(f"{organisation} · {location or 'Location not exposed'}")

    stats = db.get_latest_cycle(cur, requirement_id)
    events = db.list_events_for_requirement(cur, requirement_id)
    top = st.columns([3, 1.2])
    top[0].markdown("**Expected opportunity**")
    top[0].markdown(f"<div class='page-title'>{format_opportunity_window(opportunity['window_start'], opportunity['window_end'])}</div>", unsafe_allow_html=True)
    label = evidence_label(opportunity["confidence"])
    tone = "strong" if opportunity["confidence"] == "HIGH" else "medium" if opportunity["confidence"] == "MEDIUM" else "early"
    top[1].markdown(f"<span class='evidence-{tone}'>{label}</span>", unsafe_allow_html=True)

    st.subheader("Why did we find this?")
    reasons = []
    tender_events = [event for event in events if event["discovered_record_id"] is not None and event["event_date"]]
    if tender_events:
        reasons.append("A similar procurement has appeared before.")
    else:
        reasons.append("This requirement is linked to procurement evidence.")
    if stats and stats["median_interval_days"] is not None:
        years = stats["median_interval_days"] / 365.25
        months = round(stats["median_interval_days"] / 30.4375)
        if .8 <= years <= 1.2:
            interval_text = "around the same time each year"
        elif months >= 2:
            interval_text = f"about every {months} months"
        elif months == 1:
            interval_text = "about once a month"
        else:
            interval_text = f"about every {round(stats['median_interval_days'])} days"
        reasons.append(f"We found {len(tender_events)} past procurements for this need.")
        reasons.append(f"They appeared {interval_text}.")
        today = date.today()
        try:
            start = date.fromisoformat(opportunity["window_start"][:10])
            end = date.fromisoformat(opportunity["window_end"][:10])
            if end >= today and start <= today + timedelta(days=90):
                reasons.append("The expected time is getting close.")
        except (TypeError, ValueError):
            pass
    else:
        reasons.append("There is not enough past activity to measure timing yet.")
    if tender_events:
        newest_event = max(tender_events, key=lambda event: event["event_date"])
        try:
            window_start = date.fromisoformat(opportunity["window_start"][:10])
            window_end = date.fromisoformat(opportunity["window_end"][:10])
            if newest_event["event_date"] < window_start.isoformat() and date.today() <= window_end:
                reasons.append("No tender has been recorded in this expected window yet.")
        except (TypeError, ValueError):
            pass
    for reason in reasons:
        st.markdown(f"✓ {reason}")
    st.caption("This is an estimate from past activity, not a confirmed tender.")

    st.subheader("Procurement history")
    dated_events = sorted((event for event in events if event["event_date"]), key=lambda event: event["event_date"])
    tender_years = [event["event_date"] for event in dated_events if event["discovered_record_id"] is not None]
    timeline_years = history_timeline_years(tender_years)
    this_year = str(date.today().year)
    if timeline_years:
        columns = st.columns(len(timeline_years))
        for index, year in enumerate(timeline_years):
            observed = any(value.startswith(year) for value in tender_years)
            with columns[index]:
                st.markdown(f"<div class='timeline-date'>{year}</div>", unsafe_allow_html=True)
                st.markdown("●" if observed else "◉" if year == this_year else "—")
                st.caption("Tender seen" if observed else "Now" if year == this_year else "No record")
    if dated_events:
        for event in reversed(dated_events[-5:]):
            event_type = event["event_type"].replace("_", " ").title()
            tender = None
            if event["discovered_record_id"] is not None:
                tender = cur.execute(
                    "SELECT external_id, title, detail_url FROM discovered_records WHERE id = ?",
                    (event["discovered_record_id"],),
                ).fetchone()
            st.markdown(f"**{event['event_date']} · {event_type}**")
            if tender:
                st.write(tender["title"] or "Tender title not available")
                if tender["detail_url"]:
                    st.link_button("View official notice", tender["detail_url"], icon=":material/open_in_new:")
    else:
        st.info("No dated procurement history is available yet.")

    with st.expander("Evidence and advanced details"):
        st.write(f"Observed cycle: {stats['median_interval_days']:.0f} days" if stats and stats["median_interval_days"] is not None else "Cycle: not measured")
        st.write(f"Evidence level: {label}")
        for event in events:
            link = cur.execute(
                "SELECT match_status, match_score, match_evidence FROM requirement_event_links WHERE requirement_id = ? AND event_id = ?",
                (requirement_id, event["id"]),
            ).fetchone()
            if link:
                st.write(f"{event['event_date'] or 'Undated'} · {event['event_type'].replace('_', ' ').title()} · match score {link['match_score']:.2f}")
                st.json(json.loads(link["match_evidence"]))
            if event["discovered_record_id"] is not None:
                docs = cur.execute(
                    "SELECT filename, document_url, sha256 FROM acquired_documents WHERE record_id = ? ORDER BY id",
                    (event["discovered_record_id"],),
                ).fetchall()
                for document in docs:
                    st.write(f"{document['filename'] or 'Tender document'} · SHA-256 {document['sha256'] or 'not available'}")
                    st.link_button("Open source document", document["document_url"], icon=":material/description:")


def render_opportunities(cur):
    st.markdown('<div class="eyebrow">Procurement intelligence</div>', unsafe_allow_html=True)
    st.title("Radar")
    st.caption("What should I pay attention to?")
    opportunities = _opportunities(cur)
    selected_id = st.session_state.get("selected_requirement_id")
    selected = next((row for row in opportunities if row["requirement_id"] == selected_id), None)
    if selected:
        _show_opportunity_detail(cur, selected)
        return

    selected_tab = st.session_state.get("radar_tab", "All")
    tabs = st.tabs(["All", "New", "Coming Soon", "Watching"])
    for tab_name, tab in zip(("All", "New", "Coming Soon", "Watching"), tabs):
        with tab:
            subset = [row for row in opportunities if tab_name in opportunity_buckets(row)]
            if not subset:
                st.markdown('<div class="empty-calm">🌤️ Everything looks calm.</div>', unsafe_allow_html=True)
                st.write("Your radar is still watching government procurement.")
                if st.button("See recent tenders", key=f"empty_tenders_{tab_name}", icon=":material/description:"):
                    st.session_state.active_page = "Tenders"
                    st.rerun()
                continue
            for opportunity in subset:
                with st.container(border=True):
                    st.markdown(f"### {opportunity['display_title']}")
                    location = opportunity["observed_location"] or opportunity["location"]
                    buyer_line = opportunity["org_name"] or "Organisation not listed"
                    if location:
                        buyer_line += f" · {location}"
                    st.write(buyer_line)
                    st.markdown(
                        f"**Expected opportunity** · {format_opportunity_window(opportunity['window_start'], opportunity['window_end'])}"
                    )
                    st.markdown(f"<span class='evidence-{('strong' if opportunity['confidence'] == 'HIGH' else 'medium' if opportunity['confidence'] == 'MEDIUM' else 'early')}'>{evidence_label(opportunity['confidence'])}</span>", unsafe_allow_html=True)
                    if st.button(
                        "View opportunity",
                        key=f"open_opportunity_{tab_name}_{opportunity['requirement_id']}",
                        icon=":material/arrow_forward:",
                    ):
                        st.session_state.selected_requirement_id = opportunity["requirement_id"]
                        st.rerun()


def render_tenders(cur, compact: bool = False):
    if not compact:
        st.markdown('<div class="eyebrow">Official-source observations</div>', unsafe_allow_html=True)
        st.title("Tender notices")
    records = db.list_discovered_records(cur, limit=500)
    query = st.text_input("Search tender notices", placeholder="Tender, organisation, or place", key="compact_tender_search" if compact else "tender_search")
    status = "All statuses"
    if not compact:
        status_values = ["All statuses"] + sorted({record["acquisition_status"] for record in records})
        status = st.selectbox("Show", status_values)
    filtered = [record for record in records if
                (status == "All statuses" or record["acquisition_status"] == status)
                and (not query or query.lower() in " ".join(str(record[key] or "") for key in ("title", "external_id", "organisation", "location")).lower())]
    st.caption(f"{len(filtered)} notice(s) found")
    if not filtered:
        st.info("No notices match that search.")
        return
    visible_records = filtered[:10] if compact else filtered
    if compact and len(filtered) > len(visible_records):
        st.caption("Showing the 10 most recent notices. Use More → Tenders for filters and the full list.")
    for record in visible_records:
        title = record["title"] or record["external_id"]
        buyer = buyer_display_name(record["organisation"])
        place = record["location"] or "Place not listed"
        date_text = record["published_at"] or record["closing_at"] or "Date not listed"
        if compact:
            st.markdown(f"**{title}**")
            st.write(f"{buyer} · {place}")
            st.caption(f"{date_text} · {acquisition_status_label(record['acquisition_status'])}")
            if record["detail_url"]:
                st.link_button("Official notice", record["detail_url"], icon=":material/open_in_new:", key=f"compact_tender_{record['id']}")
            st.divider()
            continue

        st.markdown(f"### {title}")
        st.write(f"{buyer} · {place}")
        st.caption(f"{date_text} · {acquisition_status_label(record['acquisition_status'])}")
        if record["detail_url"]:
            st.link_button("Official notice", record["detail_url"], icon=":material/open_in_new:")
        documents = cur.execute(
            "SELECT filename, document_url, status, sha256 FROM acquired_documents WHERE record_id = ? ORDER BY id",
            (record["id"],),
        ).fetchall()
        with st.expander("More notice details"):
            st.write(f"Reference: {record['external_id']}")
            st.write(f"Published: {record['published_at'] or 'Not listed'} · Closing: {record['closing_at'] or 'Not listed'}")
            st.write(f"Tender value: {record['estimated_value'] or 'Not listed'}")
            for document in documents:
                file_state = "Collected" if document["status"] in {"DOWNLOADED", "PROCESSED", "DUPLICATE"} else acquisition_status_label(document["status"])
                st.write(f"{document['filename'] or 'Tender document'} · {file_state}")
                st.link_button("Open source document", document["document_url"], icon=":material/description:")
            linked_events = cur.execute(
                """SELECT pe.event_date, rel.requirement_id
                   FROM procurement_events pe
                   JOIN requirement_event_links rel ON rel.event_id = pe.id
                   WHERE pe.discovered_record_id = ?""",
                (record["id"],),
            ).fetchall()
            for event in linked_events:
                requirement = db.get_requirement(cur, event["requirement_id"]) if event["requirement_id"] else None
                if requirement:
                    st.write(f"Similar requirement: {requirement['normalized_title']} · {event['event_date']}")


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
    st.markdown('<div class="eyebrow">Things that may need you</div>', unsafe_allow_html=True)
    st.title("Alerts")
    manual = db.list_discovered_records(cur, acquisition_status="MANUAL_ACTION_REQUIRED")
    queue = db.list_review_queue(cur)
    if not manual and not queue:
        st.markdown('<div class="empty-calm">You\'re all caught up.</div>', unsafe_allow_html=True)
        st.write("Your radar will show items here when something needs a closer look.")
        return

    if manual:
        st.subheader("Official source needs a check")
        for row in manual:
            title = row["title"] or row["external_id"]
            with st.container(border=True):
                st.markdown(f"### {title}")
                st.write(row["organisation"] or "Organisation not listed")
                reason = row["last_error"] or "The source needs a manual check before this item can be collected."
                lowered = reason.lower()
                friendly_reason = "The official site asked for a person to continue." if "captcha" in lowered else "The document could not be collected automatically." if "auth" in lowered or "blocked" in lowered else "This source item needs a quick manual check."
                st.write(friendly_reason)
                with st.expander("Advanced details"):
                    st.write(reason)
                    st.write(row["detail_url"] or "No detail page was supplied by the source.")
                if row["detail_url"]:
                    st.link_button("Open official notice", row["detail_url"], icon=":material/open_in_new:")

    if queue:
        st.subheader("Contract information needs review")
        for contract in queue:
            with st.container(border=True):
                st.markdown(f"### {contract['title']}")
                st.write(contract_status_label(contract["status"]))
                if st.button("Review information", key=f"alert_review_{contract['id']}"):
                    st.session_state.selected_contract_id = contract["id"]
                    st.session_state.active_page = "Contract Search"
                    st.rerun()


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