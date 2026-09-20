"""
Proof Packet generator (spec Part 7).

Renders the exact block structure required: facts+evidence table,
calculation, confidence breakdown, exceptions, status, and recommended
action. A human should be able to verify the expiry date in under two
minutes by reading this alone.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from db import database as db


def _fmt_date(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    try:
        from datetime import date
        d = date.fromisoformat(iso)
        return d.strftime("%d-%b-%Y")
    except Exception:
        return iso


def build_proof_packet_text(cur, contract_id: int) -> str:
    contract = db.get_contract(cur, contract_id)
    if not contract:
        return "Contract not found."

    org = cur.execute("SELECT name FROM organisations WHERE id = ?", (contract["org_id"],)).fetchone()
    vendor = cur.execute("SELECT name FROM vendors WHERE id = ?", (contract["vendor_id"],)).fetchone()

    facts = db.get_facts_for_contract(cur, contract_id)
    calc = db.get_latest_calculation(cur, contract_id)
    conf = db.get_latest_confidence(cur, contract_id)
    documents = db.list_documents_for_contract(cur, contract_id)
    open_conflicts = db.list_open_conflicts(cur, contract_id)
    timeline = db.list_timeline_for_contract(cur, contract_id)

    lines = []
    lines.append("CONTRACT EXPIRY PROOF")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')} | Model version: v2.0")
    lines.append("")
    lines.append(f"Organisation:   {org['name'] if org else 'Unknown'}")
    lines.append(f"Vendor:         {vendor['name'] if vendor else 'Unknown'}")
    lines.append(f"Contract:       {contract['title']}")
    lines.append("")
    lines.append("FACTS AND EVIDENCE")

    by_type: dict[str, list] = {}
    for f in facts:
        by_type.setdefault(f["fact_type"], []).append(f)

    def render_fact_row(label: str, fact_row, value_fmt=None):
        if not fact_row:
            lines.append(f"  {label:22s} not stated / not found")
            return
        val = value_fmt(fact_row["fact_value"]) if value_fmt else fact_row["fact_value"]
        doc = db.get_document(cur, fact_row["document_id"])
        src = f"{doc['doc_type']} — {doc['source_url'] or doc['doc_title'] or 'uploaded document'}" if doc else "unknown source"
        page = f" (p.{doc['page_number']})" if doc and doc["page_number"] else ""
        lines.append(f"  {label:22s} {val}")
        lines.append(f"    evidence: \"{fact_row['evidence_quote']}\"")
        lines.append(f"    source:   {src}{page}  [confidence: {fact_row['fact_confidence']}]")

    award = by_type.get("award_date", [None])[0]
    render_fact_row("Award date", award, _fmt_date)

    # Start date — render via the resolution note stored in confidence assessment / recompute
    from core.expiry_engine import compute_expiry
    non_superseded_facts = db.get_facts_for_contract(cur, contract_id)
    result = compute_expiry(non_superseded_facts)
    sr = result.start_resolution
    lines.append(f"  {'Start date':22s} {_fmt_date(sr.start_date.isoformat()) if sr.start_date else 'UNKNOWN'}")
    lines.append(f"    basis: {sr.note}")

    dur = by_type.get("base_duration_months", [None])[0] or by_type.get("base_duration_explicit_end_date", [None])[0]
    render_fact_row("Base duration", dur)

    ext_months = by_type.get("extension_option_months", [])
    ext_count = by_type.get("extension_option_count", [])
    if ext_months:
        lines.append(f"  {'Extension option':22s} {len(ext_months)} option(s), "
                      f"{[m['fact_value'] for m in ext_months]} months each "
                      f"(count evidence: {[c['fact_value'] for c in ext_count]})")
    else:
        lines.append(f"  {'Extension option':22s} none stated")

    lines.append(f"  {'Cross-source check':22s} "
                  f"{'YES' if conf and conf['cross_source_verified'] else 'NO'} "
                  f"— {conf['cross_source_detail'] if conf else 'not yet assessed'}")

    lines.append("")
    lines.append("CALCULATION")
    lines.append(f"  {result.base_formula}")
    if result.duration_confidence == "MEDIUM" or sr.confidence == "MEDIUM":
        lines.append("  (note: one or more inputs are MEDIUM-confidence proxies — see notes above)")
    lines.append("")

    lines.append("CONFIDENCE BREAKDOWN")
    if conf:
        lines.append(f"  Award-date confidence:        {conf['award_date_confidence']}")
        lines.append(f"  Duration confidence:          {conf['duration_confidence']}")
        lines.append(f"  Start-date confidence:        {conf['start_date_confidence']}  "
                      f"{'<- limiting factor' if conf['limiting_factor'] and 'start_date' in conf['limiting_factor'] else ''}")
        lines.append(f"  Source reliability:           {conf['source_reliability']}")
        lines.append(f"  Cross-source verification:    {'YES' if conf['cross_source_verified'] else 'NO'}")
        lines.append(f"  -> EXPIRY CONFIDENCE:         {conf['expiry_confidence']}")
        lines.append(f"  Limiting factor:              {conf['limiting_factor']}")
    else:
        lines.append("  Not yet assessed.")

    lines.append("")
    lines.append("EXCEPTIONS")
    lines.append(f"  Extension option found: {'YES' if ext_months else 'NO'}")
    lines.append(f"  Termination notice found: {'YES' if by_type.get('termination_date') else 'NO'}")
    lines.append(f"  Conflicting facts (open): {len(open_conflicts)}")
    corrigenda = [d for d in documents if d["doc_type"] == "corrigendum"]
    substantive = [d for d in corrigenda if d["doc_subtype"] == "substantive_duration_change"]
    lines.append(f"  Corrigenda reviewed: {len(corrigenda)} ({len(substantive)} duration-substantive)")

    lines.append("")
    lines.append(f"STATUS: {contract['status']}")
    if conf and conf["expiry_confidence"] in ("MEDIUM", "LOW", "UNKNOWN"):
        lines.append(f"Recommended action: {'Verify start date via RTI/site contact' if sr.confidence != 'HIGH' else 'Verify remaining open items'} "
                      f"before treating {_fmt_date(result.current_expiry_estimate.isoformat()) if result.current_expiry_estimate else 'this estimate'} as firm.")
    else:
        lines.append("Recommended action: No further verification required at this time; re-check on next document ingestion.")

    lines.append("")
    lines.append(f"CURRENT EXPIRY ESTIMATE: {_fmt_date(result.current_expiry_estimate.isoformat()) if result.current_expiry_estimate else 'UNKNOWN'}")
    lines.append(f"CURRENT EXPIRY CEILING (if all extensions exercised): "
                 f"{_fmt_date(result.current_expiry_ceiling.isoformat()) if result.current_expiry_ceiling else 'UNKNOWN'}")

    return "\n".join(lines)
