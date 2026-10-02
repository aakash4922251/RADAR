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

-- contracts_needing_review (3.7) — view, not a table
CREATE VIEW IF NOT EXISTS contracts_needing_review AS
SELECT c.* FROM contracts c
LEFT JOIN confidence_assessment ca ON ca.contract_id = c.id
  AND ca.assessed_at = (SELECT MAX(assessed_at) FROM confidence_assessment WHERE contract_id = c.id)
WHERE c.status IN ('NEEDS_REVIEW','CONFLICTING_EVIDENCE')
   OR (ca.duration_confidence = 'LOW' AND c.selected_for_paid_report = 1);


-- =======================================================================
-- LAYER 2 — PROCUREMENT CYCLE INTELLIGENCE
-- Purely additive: no existing table is modified. A "requirement" is the
-- underlying recurring need (e.g. "CCTV AMC — XYZ Hospital") that survives
-- changes in tender title, incumbent vendor, or document wording across
-- multiple contracts over time.
-- =======================================================================

-- ---------------------------------------------------------------------
-- requirements — the stable underlying identity
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS requirements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id INTEGER REFERENCES organisations(id),
    asset_keyword TEXT NOT NULL,        -- canonical taxonomy key, e.g. 'cctv', 'housekeeping'
    location TEXT,                      -- normalized location token, nullable (often unknown)
    normalized_title TEXT NOT NULL,     -- canonical display description
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_requirements_org_asset ON requirements(org_id, asset_keyword);

-- ---------------------------------------------------------------------
-- requirement_aliases — every raw title/description ever seen for this
-- requirement, with provenance, so a requirement can survive wording
-- drift without losing the original evidence.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS requirement_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requirement_id INTEGER NOT NULL REFERENCES requirements(id),
    alias_text TEXT NOT NULL,
    source_contract_id INTEGER REFERENCES contracts(id),
    source_discovered_record_id INTEGER REFERENCES discovered_records(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_aliases_requirement ON requirement_aliases(requirement_id);

-- ---------------------------------------------------------------------
-- procurement_events — a normalized event abstraction, derived from
-- existing Layer-1 evidence (documents / extracted_facts /
-- contract_timeline_events), never invented.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS procurement_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER REFERENCES contracts(id),
    discovered_record_id INTEGER REFERENCES discovered_records(id),
    event_type TEXT NOT NULL CHECK (event_type IN (
        'tender_published', 'bid_submission', 'tender_awarded',
        'contract_started', 'contract_extended', 'contract_ended',
        'new_tender_published'
    )),
    event_date TEXT,                      -- ISO date; NULL if genuinely unknown
    document_id INTEGER REFERENCES documents(id),      -- provenance
    source_fact_id INTEGER REFERENCES extracted_facts(id),  -- provenance, nullable
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (contract_id, event_type, event_date, document_id)  -- idempotent re-derivation
);
CREATE INDEX IF NOT EXISTS idx_events_contract ON procurement_events(contract_id);

-- ---------------------------------------------------------------------
-- requirement_event_links — WHY an event belongs to a requirement.
-- Deliberately separate from a plain FK so every linkage carries its own
-- match evidence, never a bare MATCH=TRUE.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS requirement_event_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requirement_id INTEGER NOT NULL REFERENCES requirements(id),
    event_id INTEGER NOT NULL REFERENCES procurement_events(id),
    match_status TEXT NOT NULL CHECK (match_status IN (
        'STRONG_MATCH', 'PROBABLE_MATCH', 'REVIEW_REQUIRED'
    )),
    match_score REAL NOT NULL,
    match_evidence TEXT NOT NULL,     -- JSON: {factor: detail}
    linked_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (requirement_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_relinks_requirement ON requirement_event_links(requirement_id);

-- ---------------------------------------------------------------------
-- procurement_cycles — computed historical-cycle statistics, append-only
-- ("show your work", same pattern as expiry_calculations).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS procurement_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requirement_id INTEGER NOT NULL REFERENCES requirements(id),
    computed_at TEXT NOT NULL DEFAULT (datetime('now')),
    anchor_event_type TEXT,             -- which event_type the intervals were measured between
    cycle_dates TEXT,                   -- JSON array of ISO dates used, chronological
    interval_days TEXT,                 -- JSON array of day-gaps between consecutive cycle_dates
    n_cycles INTEGER NOT NULL,          -- number of cycle_dates observed (not intervals)
    median_interval_days REAL,
    mean_interval_days REAL,
    min_interval_days REAL,
    max_interval_days REAL,
    stddev_interval_days REAL,
    last_event_date TEXT,
    last_event_type TEXT,
    superseded_by_cycle_id INTEGER REFERENCES procurement_cycles(id)
);
CREATE INDEX IF NOT EXISTS idx_cycles_requirement ON procurement_cycles(requirement_id);

-- =======================================================================
-- LAYER 3 — FUTURE PROCUREMENT PREDICTION
-- Append-only, evidence-first. Predictions are estimates derived from
-- historical cycle statistics and current contract state; they are never
-- a claim that a tender definitely exists.
-- =======================================================================

CREATE TABLE IF NOT EXISTS predictions (
    prediction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    requirement_id INTEGER NOT NULL REFERENCES requirements(id),
    predicted_window_start TEXT NOT NULL,
    predicted_window_end TEXT NOT NULL,
    prediction_basis TEXT NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('HIGH','MEDIUM','LOW','INSUFFICIENT_DATA')),
    current_contract_state TEXT,
    prediction_status TEXT NOT NULL CHECK (prediction_status IN (
        'PREDICTED','WATCHING','TENDER_DETECTED','CONFIRMED','INVALIDATED','EXPIRED_WITHOUT_DETECTION'
    )) DEFAULT 'PREDICTED',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    superseded_by INTEGER REFERENCES predictions(prediction_id)
);
CREATE INDEX IF NOT EXISTS idx_predictions_requirement ON predictions(requirement_id, created_at);

CREATE TABLE IF NOT EXISTS prediction_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER NOT NULL REFERENCES predictions(prediction_id),
    signal_name TEXT NOT NULL,
    signal_value TEXT,
    signal_detail TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_prediction_evidence_prediction ON prediction_evidence(prediction_id);

CREATE TABLE IF NOT EXISTS prediction_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER NOT NULL REFERENCES predictions(prediction_id),
    event_id INTEGER NOT NULL REFERENCES procurement_events(id),
    match_status TEXT NOT NULL CHECK (match_status IN (
        'CONFIRMED_MATCH','PROBABLE_MATCH','UNRELATED_TENDER','REVIEW_REQUIRED'
    )),
    match_score REAL NOT NULL,
    match_evidence TEXT NOT NULL,
    matched_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_prediction_matches_prediction ON prediction_matches(prediction_id);

CREATE TABLE IF NOT EXISTS prediction_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER NOT NULL REFERENCES predictions(prediction_id),
    outcome_status TEXT NOT NULL CHECK (outcome_status IN (
        'PREDICTED','WATCHING','TENDER_DETECTED','CONFIRMED','INVALIDATED','EXPIRED_WITHOUT_DETECTION'
    )),
    resolved_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolution_note TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prediction_outcomes_prediction ON prediction_outcomes(prediction_id);

CREATE TABLE IF NOT EXISTS evaluation_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL DEFAULT (datetime('now')),
    dataset_size INTEGER NOT NULL,
    predictions_evaluated INTEGER NOT NULL,
    confirmed_matches INTEGER NOT NULL,
    probable_matches INTEGER NOT NULL,
    unmatched INTEGER NOT NULL,
    precision REAL,
    recall REAL,
    average_lead_time_days REAL,
    notes TEXT NOT NULL
);

-- =======================================================================
-- AUTOMATED PUBLIC-SOURCE ACQUISITION
-- Additive state for compliant source monitoring. Acquisition never bypasses
-- authentication, CAPTCHA, or anti-bot controls; blocked work is persisted.
-- =======================================================================

CREATE TABLE IF NOT EXISTS acquisition_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    discovery_enabled INTEGER NOT NULL DEFAULT 1,
    document_download_enabled INTEGER NOT NULL DEFAULT 1,
    poll_interval_minutes INTEGER NOT NULL DEFAULT 15,
    last_scan_at TEXT,
    last_success_at TEXT,
    status TEXT NOT NULL DEFAULT 'NOT_CONFIGURED',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS discovered_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES acquisition_sources(id),
    external_id TEXT NOT NULL,
    canonical_url TEXT,
    title TEXT,
    organisation TEXT,
    published_at TEXT,
    closing_at TEXT,
    opening_at TEXT,
    tender_type TEXT,
    location TEXT,
    classification TEXT,
    estimated_value TEXT,
    detail_url TEXT,
    source_page_url TEXT,
    raw_metadata TEXT,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    status TEXT NOT NULL DEFAULT 'DISCOVERED',
    acquisition_status TEXT NOT NULL DEFAULT 'PENDING',
    last_error TEXT,
    UNIQUE (source_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_discovered_source ON discovered_records(source_id, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_discovered_status ON discovered_records(acquisition_status);

CREATE TABLE IF NOT EXISTS acquired_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL REFERENCES discovered_records(id),
    source_url TEXT NOT NULL,
    document_url TEXT NOT NULL,
    filename TEXT,
    local_path TEXT,
    sha256 TEXT,
    content_type TEXT,
    content_length INTEGER,
    http_status INTEGER,
    discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
    downloaded_at TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',
    error_code TEXT,
    error_detail TEXT,
    document_id INTEGER REFERENCES documents(id),
    UNIQUE (record_id, document_url)
);
CREATE INDEX IF NOT EXISTS idx_acquired_hash ON acquired_documents(sha256);
CREATE INDEX IF NOT EXISTS idx_acquired_status ON acquired_documents(status);

CREATE TABLE IF NOT EXISTS acquisition_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES acquisition_sources(id),
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    records_seen INTEGER NOT NULL DEFAULT 0,
    records_new INTEGER NOT NULL DEFAULT 0,
    documents_downloaded INTEGER NOT NULL DEFAULT 0,
    documents_skipped INTEGER NOT NULL DEFAULT 0,
    documents_failed INTEGER NOT NULL DEFAULT 0,
    manual_action_required INTEGER NOT NULL DEFAULT 0,
    documents_processed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_acquisition_runs_source ON acquisition_runs(source_id, started_at);

CREATE TABLE IF NOT EXISTS acquisition_watchlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER REFERENCES acquisition_sources(id),
    field TEXT NOT NULL CHECK (field IN ('organisation', 'category', 'location', 'title')),
    value TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (source_id, field, value)
);

CREATE TABLE IF NOT EXISTS procurement_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requirement_id INTEGER NOT NULL UNIQUE REFERENCES requirements(id),
    anchor_event_type TEXT NOT NULL,
    predicted_date TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    interval_days REAL NOT NULL,
    dispersion_days REAL NOT NULL,
    confidence TEXT NOT NULL CHECK (confidence IN ('LOW', 'MEDIUM', 'HIGH')),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'MATCHED', 'EXPIRED')),
    matched_record_id INTEGER REFERENCES discovered_records(id),
    computed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_predictions_window ON procurement_predictions(window_start, window_end);

