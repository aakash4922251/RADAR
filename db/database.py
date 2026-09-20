"""
Thin, explicit data-access layer over SQLite.

No ORM: the schema is small, evidence-first, and every write path needs
to be auditable by a human reading this file top to bottom.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Optional

DEFAULT_DB_PATH = os.environ.get(
    "CER_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "contract_expiry_radar.db"),
)
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")


def get_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    conn = get_connection(db_path)
    with open(SCHEMA_PATH, "r") as f:
        conn.executescript(f.read())
    discovered_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(discovered_records)").fetchall()
    }
    if "estimated_value" not in discovered_columns:
        conn.execute("ALTER TABLE discovered_records ADD COLUMN estimated_value TEXT")
    # Older scans used the generic PENDING default when a source exposed
    # metadata but no document URL. Give those existing rows the accurate
    # metadata-only state without changing records that have documents queued.
    conn.execute(
        """UPDATE discovered_records SET status = 'DISCOVERED',
           acquisition_status = 'DISCOVERED_METADATA_ONLY'
           WHERE acquisition_status = 'PENDING'
             AND NOT EXISTS (
                 SELECT 1 FROM acquired_documents
                 WHERE acquired_documents.record_id = discovered_records.id
             )"""
    )
    conn.commit()
    conn.close()


@contextmanager
def db_cursor(db_path: str = DEFAULT_DB_PATH):
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------
# Reference lookups (get-or-create)
# ---------------------------------------------------------------------
def get_or_create(cur: sqlite3.Cursor, table: str, name: str, extra: Optional[dict] = None) -> int:
    row = cur.execute(f"SELECT id FROM {table} WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    extra = extra or {}
    cols = ["name"] + list(extra.keys())
    vals = [name] + list(extra.values())
    placeholders = ",".join(["?"] * len(vals))
    cur.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders})", vals)
    return cur.lastrowid


# ---------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------
def create_contract(cur, title: str, org_id=None, vendor_id=None, category_id=None,
                     contract_value=None) -> int:
    cur.execute(
        """INSERT INTO contracts (title, org_id, vendor_id, category_id, contract_value, status)
           VALUES (?,?,?,?,?, 'UNKNOWN')""",
        (title, org_id, vendor_id, category_id, contract_value),
    )
    return cur.lastrowid


def update_contract_status(cur, contract_id: int, **fields):
    if not fields:
        return
    fields["updated_at"] = "CURRENT_TS_PLACEHOLDER"
    cols = ", ".join(f"{k} = ?" for k in fields if k != "updated_at")
    vals = [v for k, v in fields.items() if k != "updated_at"]
    cur.execute(
        f"UPDATE contracts SET {cols}, updated_at = datetime('now') WHERE id = ?",
        vals + [contract_id],
    )


def get_contract(cur, contract_id: int) -> Optional[sqlite3.Row]:
    return cur.execute("SELECT * FROM contracts WHERE id = ?", (contract_id,)).fetchone()


def list_contracts(cur, status: Optional[str] = None) -> list:
    if status:
        return cur.execute(
            "SELECT * FROM contracts WHERE status = ? ORDER BY updated_at DESC", (status,)
        ).fetchall()
    return cur.execute("SELECT * FROM contracts ORDER BY updated_at DESC").fetchall()


def search_contracts(cur, query: str) -> list:
    q = f"%{query}%"
    return cur.execute(
        """SELECT c.*, o.name AS org_name, v.name AS vendor_name
           FROM contracts c
           LEFT JOIN organisations o ON o.id = c.org_id
           LEFT JOIN vendors v ON v.id = c.vendor_id
           WHERE c.title LIKE ? OR o.name LIKE ? OR v.name LIKE ?
           ORDER BY c.updated_at DESC""",
        (q, q, q),
    ).fetchall()


# ---------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------
def find_document_by_hash(cur, full_hash: str) -> Optional[sqlite3.Row]:
    return cur.execute("SELECT * FROM documents WHERE raw_text_full_hash = ?", (full_hash,)).fetchone()


def insert_document(cur, *, contract_id, doc_type, doc_subtype=None, source_url=None,
                     source_org=None, doc_title=None, doc_date=None, page_number=None,
                     raw_text, raw_text_full_hash, supersedes_document_id=None) -> int:
    cur.execute(
        """INSERT INTO documents
           (contract_id, doc_type, doc_subtype, source_url, source_org, doc_title,
            doc_date, page_number, raw_text, raw_text_full_hash, supersedes_document_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (contract_id, doc_type, doc_subtype, source_url, source_org, doc_title,
         doc_date, page_number, raw_text, raw_text_full_hash, supersedes_document_id),
    )
    return cur.lastrowid


def link_document_to_contract(cur, document_id: int, contract_id: int):
    cur.execute("UPDATE documents SET contract_id = ? WHERE id = ?", (contract_id, document_id))


def get_document(cur, document_id: int) -> Optional[sqlite3.Row]:
    return cur.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()


def list_documents_for_contract(cur, contract_id: int) -> list:
    return cur.execute(
        "SELECT * FROM documents WHERE contract_id = ? ORDER BY doc_date ASC", (contract_id,)
    ).fetchall()


# ---------------------------------------------------------------------
# Extracted facts
# ---------------------------------------------------------------------
def insert_fact(cur, *, contract_id, document_id, fact_type, fact_value, evidence_quote,
                context_window=None, char_offset_start=None, char_offset_end=None,
                extraction_rule_id, fact_confidence) -> int:
    cur.execute(
        """INSERT INTO extracted_facts
           (contract_id, document_id, fact_type, fact_value, evidence_quote, context_window,
            char_offset_start, char_offset_end, extraction_rule_id, fact_confidence)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (contract_id, document_id, fact_type, str(fact_value), evidence_quote, context_window,
         char_offset_start, char_offset_end, extraction_rule_id, fact_confidence),
    )
    return cur.lastrowid


def get_facts_for_contract(cur, contract_id: int, fact_type: Optional[str] = None,
                            include_superseded: bool = False) -> list:
    q = "SELECT * FROM extracted_facts WHERE contract_id = ?"
    params: list = [contract_id]
    if fact_type:
        q += " AND fact_type = ?"
        params.append(fact_type)
    if not include_superseded:
        q += " AND superseded_by_fact_id IS NULL"
    q += " ORDER BY extracted_at ASC"
    return cur.execute(q, params).fetchall()


def get_facts_for_document(cur, document_id: int) -> list:
    return cur.execute("SELECT * FROM extracted_facts WHERE document_id = ?", (document_id,)).fetchall()


def supersede_fact(cur, old_fact_id: int, new_fact_id: int):
    cur.execute("UPDATE extracted_facts SET superseded_by_fact_id = ? WHERE id = ?",
                (new_fact_id, old_fact_id))


# ---------------------------------------------------------------------
# Timeline events
# ---------------------------------------------------------------------
def insert_timeline_event(cur, *, contract_id, event_type, event_date=None, document_id=None,
                           prior_expiry_estimate=None, new_expiry_estimate=None, narrative=None) -> int:
    cur.execute(
        """INSERT INTO contract_timeline_events
           (contract_id, event_date, event_type, document_id, prior_expiry_estimate,
            new_expiry_estimate, narrative)
           VALUES (?,?,?,?,?,?,?)""",
        (contract_id, event_date, event_type, document_id, prior_expiry_estimate,
         new_expiry_estimate, narrative),
    )
    return cur.lastrowid


def list_timeline_for_contract(cur, contract_id: int) -> list:
    return cur.execute(
        "SELECT * FROM contract_timeline_events WHERE contract_id = ? ORDER BY observed_at ASC",
        (contract_id,),
    ).fetchall()


# ---------------------------------------------------------------------
# Expiry calculations
# ---------------------------------------------------------------------
def insert_expiry_calculation(cur, *, contract_id, calculation_type, formula_text,
                               input_fact_ids: Iterable[int], result_date, confidence,
                               superseded_by_calculation_id=None) -> int:
    cur.execute(
        """INSERT INTO expiry_calculations
           (contract_id, calculation_type, formula_text, input_fact_ids, result_date,
            confidence, superseded_by_calculation_id)
           VALUES (?,?,?,?,?,?,?)""",
        (contract_id, calculation_type, formula_text, json.dumps(list(input_fact_ids)),
         result_date, confidence, superseded_by_calculation_id),
    )
    return cur.lastrowid


def get_latest_calculation(cur, contract_id: int, calculation_type: Optional[str] = None) -> Optional[sqlite3.Row]:
    q = "SELECT * FROM expiry_calculations WHERE contract_id = ? AND superseded_by_calculation_id IS NULL"
    params = [contract_id]
    if calculation_type:
        q += " AND calculation_type = ?"
        params.append(calculation_type)
    q += " ORDER BY computed_at DESC, id DESC LIMIT 1"
    return cur.execute(q, params).fetchone()


def list_calculations_for_contract(cur, contract_id: int) -> list:
    return cur.execute(
        "SELECT * FROM expiry_calculations WHERE contract_id = ? ORDER BY computed_at ASC",
        (contract_id,),
    ).fetchall()


# ---------------------------------------------------------------------
# Confidence assessment
# ---------------------------------------------------------------------
def insert_confidence_assessment(cur, *, contract_id, award_date_confidence, duration_confidence,
                                  start_date_confidence, source_reliability, cross_source_verified,
                                  cross_source_detail, expiry_confidence, limiting_factor) -> int:
    cur.execute(
        """INSERT INTO confidence_assessment
           (contract_id, award_date_confidence, duration_confidence, start_date_confidence,
            source_reliability, cross_source_verified, cross_source_detail, expiry_confidence,
            limiting_factor)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (contract_id, award_date_confidence, duration_confidence, start_date_confidence,
         source_reliability, int(cross_source_verified), cross_source_detail, expiry_confidence,
         limiting_factor),
    )
    return cur.lastrowid


def get_latest_confidence(cur, contract_id: int) -> Optional[sqlite3.Row]:
    return cur.execute(
        "SELECT * FROM confidence_assessment WHERE contract_id = ? ORDER BY assessed_at DESC, id DESC LIMIT 1",
        (contract_id,),
    ).fetchone()


# ---------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------
def insert_conflict(cur, *, contract_id, fact_type, fact_id_a, fact_id_b) -> int:
    cur.execute(
        """INSERT INTO fact_conflicts (contract_id, fact_type, fact_id_a, fact_id_b)
           VALUES (?,?,?,?)""",
        (contract_id, fact_type, fact_id_a, fact_id_b),
    )
    return cur.lastrowid


def list_open_conflicts(cur, contract_id: Optional[int] = None) -> list:
    if contract_id:
        return cur.execute(
            "SELECT * FROM fact_conflicts WHERE contract_id = ? AND resolved = 0", (contract_id,)
        ).fetchall()
    return cur.execute("SELECT * FROM fact_conflicts WHERE resolved = 0").fetchall()


def resolve_conflict(cur, conflict_id: int, note: str):
    cur.execute(
        "UPDATE fact_conflicts SET resolved = 1, resolution_note = ? WHERE id = ?",
        (note, conflict_id),
    )


def list_review_queue(cur) -> list:
    return cur.execute("SELECT * FROM contracts_needing_review").fetchall()


# ---------------------------------------------------------------------
# Automated public-source acquisition
# ---------------------------------------------------------------------
def upsert_acquisition_source(cur, *, source_key, name, base_url, poll_interval_minutes=15,
                              enabled=True, discovery_enabled=True, document_download_enabled=True) -> int:
    existing = cur.execute("SELECT id FROM acquisition_sources WHERE source_key = ?", (source_key,)).fetchone()
    values = (name, base_url, int(enabled), int(discovery_enabled), int(document_download_enabled), poll_interval_minutes)
    if existing:
        cur.execute(
            """UPDATE acquisition_sources
               SET name = ?, base_url = ?, enabled = ?, discovery_enabled = ?,
                   document_download_enabled = ?, poll_interval_minutes = ?, updated_at = datetime('now')
               WHERE id = ?""",
            values + (existing["id"],),
        )
        return existing["id"]
    cur.execute(
        """INSERT INTO acquisition_sources
           (source_key, name, base_url, enabled, discovery_enabled,
            document_download_enabled, poll_interval_minutes)
           VALUES (?,?,?,?,?,?,?)""",
        (source_key, name, base_url, int(enabled), int(discovery_enabled),
         int(document_download_enabled), poll_interval_minutes),
    )
    return cur.lastrowid


def get_acquisition_source(cur, source_key: str):
    return cur.execute("SELECT * FROM acquisition_sources WHERE source_key = ?", (source_key,)).fetchone()


def list_acquisition_sources(cur, enabled_only=False) -> list:
    query = "SELECT * FROM acquisition_sources"
    if enabled_only:
        query += " WHERE enabled = 1"
    return cur.execute(query + " ORDER BY name").fetchall()


def start_acquisition_run(cur, source_id: int) -> int:
    cur.execute("INSERT INTO acquisition_runs (source_id) VALUES (?)", (source_id,))
    return cur.lastrowid


def finish_acquisition_run(cur, run_id: int, *, status, records_seen=0, records_new=0,
                           documents_downloaded=0, documents_skipped=0, documents_failed=0,
                           manual_action_required=0, documents_processed=0, error=None):
    cur.execute(
        """UPDATE acquisition_runs SET finished_at = datetime('now'), status = ?,
           records_seen = ?, records_new = ?, documents_downloaded = ?, documents_skipped = ?,
           documents_failed = ?, manual_action_required = ?, documents_processed = ?, error = ?
           WHERE id = ?""",
        (status, records_seen, records_new, documents_downloaded, documents_skipped,
         documents_failed, manual_action_required, documents_processed, error, run_id),
    )


def update_acquisition_source_status(cur, source_id: int, *, status, successful=False):
    cur.execute(
        """UPDATE acquisition_sources SET status = ?, last_scan_at = datetime('now'),
           last_success_at = CASE WHEN ? THEN datetime('now') ELSE last_success_at END,
           updated_at = datetime('now') WHERE id = ?""",
        (status, int(successful), source_id),
    )


def upsert_discovered_record(cur, *, source_id, external_id, canonical_url=None, title=None,
                             organisation=None, published_at=None, closing_at=None, opening_at=None,
                             tender_type=None, location=None, classification=None, estimated_value=None,
                             detail_url=None,
                             source_page_url=None, raw_metadata=None):
    existing = cur.execute(
        "SELECT id FROM discovered_records WHERE source_id = ? AND external_id = ?",
        (source_id, external_id),
    ).fetchone()
    values = (canonical_url, title, organisation, published_at, closing_at, opening_at,
              tender_type, location, classification, estimated_value, detail_url, source_page_url, raw_metadata)
    if existing:
        cur.execute(
            """UPDATE discovered_records SET canonical_url = COALESCE(?, canonical_url),
               title = COALESCE(?, title), organisation = COALESCE(?, organisation),
               published_at = COALESCE(?, published_at), closing_at = COALESCE(?, closing_at),
               opening_at = COALESCE(?, opening_at), tender_type = COALESCE(?, tender_type),
               location = COALESCE(?, location), classification = COALESCE(?, classification),
               estimated_value = COALESCE(?, estimated_value), detail_url = COALESCE(?, detail_url),
               source_page_url = COALESCE(?, source_page_url),
               raw_metadata = COALESCE(?, raw_metadata), last_seen_at = datetime('now')
               WHERE id = ?""",
            values + (existing["id"],),
        )
        return existing["id"], False
    cur.execute(
        """INSERT INTO discovered_records
           (source_id, external_id, canonical_url, title, organisation, published_at, closing_at,
            opening_at, tender_type, location, classification, estimated_value, detail_url,
            source_page_url, raw_metadata)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (source_id, external_id) + values,
    )
    return cur.lastrowid, True


def list_discovered_records(cur, *, source_id=None, acquisition_status=None, limit=100):
    query = "SELECT dr.*, s.source_key, s.name AS source_name FROM discovered_records dr JOIN acquisition_sources s ON s.id = dr.source_id WHERE 1=1"
    params = []
    if source_id is not None:
        query += " AND dr.source_id = ?"; params.append(source_id)
    if acquisition_status:
        query += " AND dr.acquisition_status = ?"; params.append(acquisition_status)
    query += " ORDER BY dr.first_seen_at DESC LIMIT ?"; params.append(limit)
    return cur.execute(query, params).fetchall()


def set_discovered_record_status(cur, record_id: int, *, status=None, acquisition_status=None, error=None):
    fields, values = [], []
    if status is not None: fields.append("status = ?"); values.append(status)
    if acquisition_status is not None: fields.append("acquisition_status = ?"); values.append(acquisition_status)
    if error is not None: fields.append("last_error = ?"); values.append(error)
    if fields:
        values.append(record_id)
        cur.execute("UPDATE discovered_records SET " + ", ".join(fields) + " WHERE id = ?", values)


def upsert_acquired_document(cur, *, record_id, source_url, document_url, filename=None):
    existing = cur.execute(
        "SELECT id FROM acquired_documents WHERE record_id = ? AND document_url = ?",
        (record_id, document_url),
    ).fetchone()
    if existing:
        return existing["id"], False
    cur.execute(
        "INSERT INTO acquired_documents (record_id, source_url, document_url, filename) VALUES (?,?,?,?)",
        (record_id, source_url, document_url, filename),
    )
    return cur.lastrowid, True


def update_acquired_document(cur, document_row_id: int, **fields):
    allowed = {"filename", "local_path", "sha256", "content_type", "content_length", "http_status",
                "downloaded_at", "status", "error_code", "error_detail", "document_id"}
    values = [(key, value) for key, value in fields.items() if key in allowed]
    if not values:
        return
    cur.execute(
        "UPDATE acquired_documents SET " + ", ".join(f"{key} = ?" for key, _ in values) + " WHERE id = ?",
        [value for _, value in values] + [document_row_id],
    )


def find_acquired_document_by_hash(cur, sha256: str):
    return cur.execute(
        "SELECT * FROM acquired_documents WHERE sha256 = ? AND status = 'DOWNLOADED' ORDER BY id LIMIT 1",
        (sha256,),
    ).fetchone()


def list_acquisition_runs(cur, source_id=None, limit=20):
    query = "SELECT ar.*, s.source_key, s.name AS source_name FROM acquisition_runs ar JOIN acquisition_sources s ON s.id = ar.source_id"
    params = []
    if source_id is not None:
        query += " WHERE ar.source_id = ?"; params.append(source_id)
    query += " ORDER BY ar.started_at DESC, ar.id DESC LIMIT ?"; params.append(limit)
    return cur.execute(query, params).fetchall()


def upsert_watchlist_item(cur, *, source_id=None, field, value):
    existing = cur.execute(
        "SELECT id FROM acquisition_watchlist WHERE source_id IS ? AND field = ? AND value = ?",
        (source_id, field, value),
    ).fetchone()
    if existing:
        return existing["id"]
    cur.execute("INSERT INTO acquisition_watchlist (source_id, field, value) VALUES (?,?,?)",
                (source_id, field, value))
    return cur.lastrowid


def list_watchlist(cur, source_id=None):
    if source_id is None:
        return cur.execute("SELECT * FROM acquisition_watchlist WHERE enabled = 1 ORDER BY field, value").fetchall()
    return cur.execute(
        "SELECT * FROM acquisition_watchlist WHERE enabled = 1 AND (source_id IS NULL OR source_id = ?) ORDER BY field, value",
        (source_id,),
    ).fetchall()


def upsert_prediction(cur, *, requirement_id, anchor_event_type, predicted_date, window_start,
                      window_end, interval_days, dispersion_days, confidence):
    existing = cur.execute("SELECT id FROM procurement_predictions WHERE requirement_id = ?", (requirement_id,)).fetchone()
    values = (anchor_event_type, predicted_date, window_start, window_end, interval_days, dispersion_days, confidence)
    if existing:
        cur.execute(
            """UPDATE procurement_predictions SET anchor_event_type = ?, predicted_date = ?,
               window_start = ?, window_end = ?, interval_days = ?, dispersion_days = ?,
               confidence = ?, status = 'ACTIVE', matched_record_id = NULL, computed_at = datetime('now')
               WHERE id = ?""",
            values + (existing["id"],),
        )
        return existing["id"]
    cur.execute(
        """INSERT INTO procurement_predictions
           (requirement_id, anchor_event_type, predicted_date, window_start, window_end,
            interval_days, dispersion_days, confidence)
           VALUES (?,?,?,?,?,?,?,?)""",
        (requirement_id,) + values,
    )
    return cur.lastrowid


def get_prediction_for_requirement(cur, requirement_id):
    return cur.execute("SELECT * FROM procurement_predictions WHERE requirement_id = ?", (requirement_id,)).fetchone()


def list_predictions(cur, status=None):
    query = "SELECT pp.*, r.asset_keyword, r.normalized_title FROM procurement_predictions pp JOIN requirements r ON r.id = pp.requirement_id"
    params = []
    if status:
        query += " WHERE pp.status = ?"; params.append(status)
    return cur.execute(query + " ORDER BY pp.predicted_date", params).fetchall()


def mark_prediction_matched(cur, prediction_id, record_id):
    cur.execute("UPDATE procurement_predictions SET status = 'MATCHED', matched_record_id = ? WHERE id = ?", (record_id, prediction_id))


# ---------------------------------------------------------------------
# Layer 2 — Requirements
# ---------------------------------------------------------------------
def create_requirement(cur, *, org_id, asset_keyword, location, normalized_title) -> int:
    cur.execute(
        """INSERT INTO requirements (org_id, asset_keyword, location, normalized_title)
           VALUES (?,?,?,?)""",
        (org_id, asset_keyword, location, normalized_title),
    )
    return cur.lastrowid


def get_requirement(cur, requirement_id: int) -> Optional[sqlite3.Row]:
    return cur.execute("SELECT * FROM requirements WHERE id = ?", (requirement_id,)).fetchone()


def list_requirements(cur) -> list:
    return cur.execute("SELECT * FROM requirements ORDER BY updated_at DESC").fetchall()


def list_requirements_for_org_asset(cur, org_id: Optional[int], asset_keyword: str) -> list:
    if org_id is None:
        return cur.execute(
            "SELECT * FROM requirements WHERE org_id IS NULL AND asset_keyword = ?", (asset_keyword,)
        ).fetchall()
    return cur.execute(
        "SELECT * FROM requirements WHERE org_id = ? AND asset_keyword = ?", (org_id, asset_keyword)
    ).fetchall()


def touch_requirement(cur, requirement_id: int):
    cur.execute("UPDATE requirements SET updated_at = datetime('now') WHERE id = ?", (requirement_id,))


def add_requirement_alias(cur, *, requirement_id, alias_text, source_contract_id=None) -> int:
    existing = cur.execute(
        """SELECT id FROM requirement_aliases
           WHERE requirement_id = ? AND alias_text = ? AND source_contract_id IS ?""",
        (requirement_id, alias_text, source_contract_id),
    ).fetchone()
    if existing:
        return existing["id"]
    cur.execute(
        """INSERT INTO requirement_aliases (requirement_id, alias_text, source_contract_id)
           VALUES (?,?,?)""",
        (requirement_id, alias_text, source_contract_id),
    )
    return cur.lastrowid


def list_aliases_for_requirement(cur, requirement_id: int) -> list:
    return cur.execute(
        "SELECT * FROM requirement_aliases WHERE requirement_id = ? ORDER BY created_at ASC",
        (requirement_id,),
    ).fetchall()


def get_requirement_for_contract(cur, contract_id: int) -> Optional[sqlite3.Row]:
    """A contract is linked to a requirement indirectly, via its
    procurement_events -> requirement_event_links. Returns the requirement
    with the strongest linkage, if any."""
    row = cur.execute(
        """SELECT r.* FROM requirements r
           JOIN requirement_event_links rel ON rel.requirement_id = r.id
           JOIN procurement_events pe ON pe.id = rel.event_id
           WHERE pe.contract_id = ?
           ORDER BY CASE rel.match_status
               WHEN 'STRONG_MATCH' THEN 0 WHEN 'PROBABLE_MATCH' THEN 1 ELSE 2 END
           LIMIT 1""",
        (contract_id,),
    ).fetchone()
    return row


# ---------------------------------------------------------------------
# Layer 2 — Procurement events
# ---------------------------------------------------------------------
def insert_procurement_event(cur, *, contract_id, event_type, event_date, document_id=None,
                              source_fact_id=None, notes=None) -> Optional[int]:
    """Idempotent: re-deriving the same event from the same document is a
    no-op (UNIQUE constraint on contract_id, event_type, event_date,
    document_id), returning the existing row's id instead of erroring."""
    existing = cur.execute(
        """SELECT id FROM procurement_events
           WHERE contract_id IS ? AND event_type = ? AND event_date IS ? AND document_id IS ?""",
        (contract_id, event_type, event_date, document_id),
    ).fetchone()
    if existing:
        return existing["id"]
    cur.execute(
        """INSERT INTO procurement_events
           (contract_id, event_type, event_date, document_id, source_fact_id, notes)
           VALUES (?,?,?,?,?,?)""",
        (contract_id, event_type, event_date, document_id, source_fact_id, notes),
    )
    return cur.lastrowid


def list_events_for_contract(cur, contract_id: int) -> list:
    return cur.execute(
        "SELECT * FROM procurement_events WHERE contract_id = ? ORDER BY event_date ASC",
        (contract_id,),
    ).fetchall()


def list_events_for_requirement(cur, requirement_id: int) -> list:
    return cur.execute(
        """SELECT pe.* FROM procurement_events pe
           JOIN requirement_event_links rel ON rel.event_id = pe.id
           WHERE rel.requirement_id = ?
           ORDER BY pe.event_date ASC""",
        (requirement_id,),
    ).fetchall()


def link_event_to_requirement(cur, *, requirement_id, event_id, match_status, match_score,
                               match_evidence_json) -> int:
    existing = cur.execute(
        "SELECT id FROM requirement_event_links WHERE requirement_id = ? AND event_id = ?",
        (requirement_id, event_id),
    ).fetchone()
    if existing:
        cur.execute(
            """UPDATE requirement_event_links
               SET match_status = ?, match_score = ?, match_evidence = ?, linked_at = datetime('now')
               WHERE id = ?""",
            (match_status, match_score, match_evidence_json, existing["id"]),
        )
        return existing["id"]
    cur.execute(
        """INSERT INTO requirement_event_links
           (requirement_id, event_id, match_status, match_score, match_evidence)
           VALUES (?,?,?,?,?)""",
        (requirement_id, event_id, match_status, match_score, match_evidence_json),
    )
    return cur.lastrowid


# ---------------------------------------------------------------------
# Layer 2 — Procurement cycles
# ---------------------------------------------------------------------
def insert_procurement_cycle(cur, *, requirement_id, anchor_event_type, cycle_dates_json,
                              interval_days_json, n_cycles, median_interval_days, mean_interval_days,
                              min_interval_days, max_interval_days, stddev_interval_days,
                              last_event_date, last_event_type) -> int:
    cur.execute(
        """INSERT INTO procurement_cycles
           (requirement_id, anchor_event_type, cycle_dates, interval_days, n_cycles,
            median_interval_days, mean_interval_days, min_interval_days, max_interval_days,
            stddev_interval_days, last_event_date, last_event_type)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (requirement_id, anchor_event_type, cycle_dates_json, interval_days_json, n_cycles,
         median_interval_days, mean_interval_days, min_interval_days, max_interval_days,
         stddev_interval_days, last_event_date, last_event_type),
    )
    new_id = cur.lastrowid
    cur.execute(
        """UPDATE procurement_cycles SET superseded_by_cycle_id = ?
           WHERE requirement_id = ? AND id != ? AND superseded_by_cycle_id IS NULL""",
        (new_id, requirement_id, new_id),
    )
    return new_id


def get_latest_cycle(cur, requirement_id: int) -> Optional[sqlite3.Row]:
    return cur.execute(
        """SELECT * FROM procurement_cycles WHERE requirement_id = ?
           AND superseded_by_cycle_id IS NULL ORDER BY computed_at DESC, id DESC LIMIT 1""",
        (requirement_id,),
    ).fetchone()
