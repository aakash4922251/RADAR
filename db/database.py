"""
Thin, explicit data-access layer over SQLite.

No ORM: the schema is small, evidence-first, and every write path needs
to be auditable by a human reading this file top to bottom.
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Iterable, Optional

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
    conn.commit()
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


# ---------------------------------------------------------------------
# Automated public-source acquisition
