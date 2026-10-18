-- Contract Expiry Radar — v2.0 Evidence-First Schema
-- Implements Part 3 of the Trust & Evidence Architecture spec.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
-- Reference tables
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS organisations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    is_masked INTEGER NOT NULL DEFAULT 0   -- e.g. defence-masked buyers, example #15
);

CREATE TABLE IF NOT EXISTS vendors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

-- ---------------------------------------------------------------------
-- 3.4 contracts — trimmed, derived fields only
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contracts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER REFERENCES organisations(id),
    vendor_id INTEGER REFERENCES vendors(id),
    category_id INTEGER REFERENCES categories(id),
    title TEXT NOT NULL,
    contract_value REAL,
    status TEXT NOT NULL DEFAULT 'UNKNOWN'
        CHECK (status IN (
            'VERIFIED','HIGH_CONFIDENCE','NEEDS_REVIEW','CONFLICTING_EVIDENCE',
            'INSUFFICIENT_EVIDENCE','EXPIRED','EXPIRED_UNVERIFIED','CANCELLED',
            'EXTENDED','UNKNOWN'
        )),
    current_expiry_estimate TEXT,          -- ISO date, always base/conservative
    current_expiry_ceiling TEXT,           -- ISO date, if every option exercised
    last_reconciled_at TEXT,
    last_human_verified_at TEXT,
    selected_for_paid_report INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------------------------------------------------------------------
-- 3.1 documents — the evidence ledger
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER REFERENCES contracts(id),   -- nullable until linked
    doc_type TEXT NOT NULL CHECK (doc_type IN (
        'nit','bid_document','corrigendum','aoc','loa','work_order',
        'signed_agreement','site_handover_letter','sla_stc',
        'platform_amendment','termination_notice','department_page','other'
    )),
    doc_subtype TEXT,
    source_url TEXT,
    source_org TEXT,
    doc_title TEXT,
    doc_date TEXT,               -- ISO date printed on the document
    page_number INTEGER,
    raw_text TEXT,
    raw_text_full_hash TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    supersedes_document_id INTEGER REFERENCES documents(id),
    UNIQUE (raw_text_full_hash)   -- dedup by full-document hash
);

-- ---------------------------------------------------------------------
-- 3.2 extracted_facts — one row per atomic claim
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS extracted_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER REFERENCES contracts(id),
    document_id INTEGER NOT NULL REFERENCES documents(id),
    fact_type TEXT NOT NULL CHECK (fact_type IN (
        'award_date','sanction_date','loa_date','work_order_date',
        'agreement_signed_date','site_handover_date','commencement_date_explicit',
        'base_duration_months','base_duration_explicit_end_date',
        'extension_option_months','extension_option_count','extension_exercised',
        'extension_exercised_new_end_date','absolute_expiry_ceiling',
        'platform_amendment_new_duration','termination_date','contract_value',
        'vendor_name','organisation_name'
    )),
    fact_value TEXT NOT NULL,
    evidence_quote TEXT NOT NULL,
    context_window TEXT,
    char_offset_start INTEGER,
    char_offset_end INTEGER,
    extraction_rule_id TEXT NOT NULL,
    extracted_at TEXT NOT NULL DEFAULT (datetime('now')),
    fact_confidence TEXT NOT NULL CHECK (fact_confidence IN ('HIGH','MEDIUM','LOW','UNKNOWN')),
    superseded_by_fact_id INTEGER REFERENCES extracted_facts(id)
);

-- ---------------------------------------------------------------------
-- 3.3 contract_timeline_events
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contract_timeline_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL REFERENCES contracts(id),
    event_date TEXT,
    observed_at TEXT NOT NULL DEFAULT (datetime('now')),
    event_type TEXT NOT NULL CHECK (event_type IN (
        'awarded','commenced','extension_option_exercised','extension_option_lapsed',
        'platform_duration_amendment','corrigendum_bid_deadline_only',
        'corrigendum_substantive','retendered','terminated_early','completed_early',
        'cancelled','conflict_flagged','conflict_resolved','manual_correction'
    )),
    document_id INTEGER REFERENCES documents(id),
    prior_expiry_estimate TEXT,
    new_expiry_estimate TEXT,
    narrative TEXT
);

-- ---------------------------------------------------------------------
-- 3.5 expiry_calculations — "show your work"
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS expiry_calculations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL REFERENCES contracts(id),
    computed_at TEXT NOT NULL DEFAULT (datetime('now')),
    calculation_type TEXT NOT NULL CHECK (calculation_type IN (
        'base','with_extension','revised_post_amendment'
    )),
    formula_text TEXT NOT NULL,
    input_fact_ids TEXT NOT NULL,     -- JSON array
    result_date TEXT,                 -- NULL means UNKNOWN
    confidence TEXT NOT NULL CHECK (confidence IN ('HIGH','MEDIUM','LOW','UNKNOWN')),
    superseded_by_calculation_id INTEGER REFERENCES expiry_calculations(id)
);

-- ---------------------------------------------------------------------
-- 3.6 confidence_assessment — six dimensions, never collapsed
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS confidence_assessment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL REFERENCES contracts(id),
    assessed_at TEXT NOT NULL DEFAULT (datetime('now')),
    award_date_confidence TEXT NOT NULL CHECK (award_date_confidence IN ('HIGH','MEDIUM','LOW','UNKNOWN')),
    duration_confidence TEXT NOT NULL CHECK (duration_confidence IN ('HIGH','MEDIUM','LOW','UNKNOWN')),
    start_date_confidence TEXT NOT NULL CHECK (start_date_confidence IN ('HIGH','MEDIUM','LOW','UNKNOWN')),
    source_reliability TEXT NOT NULL CHECK (source_reliability IN ('HIGH','MEDIUM','LOW','UNKNOWN')),
    cross_source_verified INTEGER NOT NULL DEFAULT 0,
    cross_source_detail TEXT,
    expiry_confidence TEXT NOT NULL CHECK (expiry_confidence IN ('HIGH','MEDIUM','LOW','UNKNOWN')),
    limiting_factor TEXT
);

-- ---------------------------------------------------------------------
-- Conflicts — explicit table so the review queue can render "both sides"
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fact_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL REFERENCES contracts(id),
    fact_type TEXT NOT NULL,
    fact_id_a INTEGER NOT NULL REFERENCES extracted_facts(id),
    fact_id_b INTEGER NOT NULL REFERENCES extracted_facts(id),
    detected_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved INTEGER NOT NULL DEFAULT 0,
    resolution_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_facts_contract ON extracted_facts(contract_id);
CREATE INDEX IF NOT EXISTS idx_facts_document ON extracted_facts(document_id);
CREATE INDEX IF NOT EXISTS idx_documents_contract ON documents(contract_id);
CREATE INDEX IF NOT EXISTS idx_timeline_contract ON contract_timeline_events(contract_id);
CREATE INDEX IF NOT EXISTS idx_calc_contract ON expiry_calculations(contract_id);
