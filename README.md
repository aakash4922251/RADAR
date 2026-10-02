# Contract Expiry Radar

An evidence-first system that analyzes Indian government procurement documents
(NITs, corrigenda, AOCs, work orders, agreements) and determines, **with
explicit evidence and confidence**, when an awarded contract is expected to
become commercially contestable again.

Built to the **Trust & Evidence Architecture v2.0** specification: accuracy
and evidence over coverage. If the system cannot reliably determine an expiry
date, it says **UNKNOWN** instead of guessing.

```
document → extract text → extract facts → capture evidence →
resolve start date → resolve duration → calculate expiry →
calculate confidence → generate Proof Packet → save to SQLite →
display in Streamlit
```

## Product layers

**Layer 1 — Contract Intelligence** (prior build, unchanged in this update):
what government contracts exist, who holds them, their terms, evidence-backed
expiry, and confidence. See the "What's implemented" section below.

**Layer 2 — Procurement Cycle Intelligence**: does a requirement appear to be
procured repeatedly, how often, and what's the historical cycle?

**Layer 3 — Procurement Radar**: conservative prediction windows are computed
from Layer 2's measured historical cycles. They show the anchor event, median
interval, dispersion, date window, and confidence; they do not claim a tender
is certain to occur.

## What's implemented — Layer 3 (Future Procurement Prediction)

Layer 3 keeps the same explainable, rule-based philosophy as Layer 1 and Layer
2: no machine-learning guesswork, no fake probabilities, no hidden logic.
The system estimates a future procurement window from historical cycle stats,
current contract state, and observed tender-before-expiry relationships, then
stores the evidence behind the decision in the database.

Key behavior:
- `core/prediction_engine.py` computes explicit signals for interval regularity,
  expiry offset, historical tender-to-expiry relationship, extension behaviour,
  recurrence strength, timing stability, and lifecycle state.
- Every prediction and explanation are persisted in `predictions` and
  `prediction_evidence`, with an explicit status lifecycle.
- `core/prediction_matching.py` matches a new tender against an open prediction
  only when multiple supporting signals line up; same-org + same-word alone is
  not enough.
- `core/backtesting.py` simulates a historical as-of date and verifies the
  prediction window against the actual next event without leaking future data.

> Predictions are estimates based on historical procurement behaviour and
> available official-source evidence. They do not guarantee that a
> government tender will be issued.
>
> Absence of a detected tender does not prove that no procurement occurred.

The app now includes a "Future Procurement" page that shows the live prediction
window, confidence, and evidence for each requirement.

## Quick start

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
# Debian/Ubuntu: sudo apt-get install poppler-utils tesseract-ocr
cp .env.example .env              # optional, defaults work out of the box

# seed a few demo contracts built from the spec's real research examples
python demo_data/demo_seed.py

streamlit run app.py

# one compliant public-source scan
python -m acquisition.scan --source cppp

# continuous polling in a separate worker process
python -m acquisition.worker --source cppp

# one-request source health check
python -m acquisition.health --source cppp
```

Set `CPPP_SEARCH_URL` in the environment to override the CPPP listing/search
URL used by the app, scanner, worker, and health check. It defaults to
`https://eprocure.gov.in/eprocure/app`.

Automated discovery keeps tender records and downloaded tender documents
separate from contracts. Dated tenders with a recognized service category are
linked to a requirement with auditable match factors and can contribute to an
inferred historical cycle; they do not create vendor or contract-expiry data.

Run the test suite:

```bash
pytest
```

(If `pytest` isn't installed in your environment for some reason, the same
suite can be exercised without it via `python -m tests._manual_runner` — this
is only a fallback used during development in a sandboxed, offline
environment and is not the intended way to run the tests day to day.)

## What's implemented — Layer 1 (Contract Intelligence)

### Ingestion (`core/ingestion.py`)
- PDF upload via PyMuPDF → pdfplumber → OCR (pytesseract) fallback chain
- Plain-text ingestion (pasted clauses, OCR output)
- SHA-256 hashing of full document text for deduplication — an identical
  document uploaded twice is recognized and not double-processed
- Manual upload remains available for unsupported or blocked sources. Automated
  public acquisition is implemented in `acquisition/`, with CPPP discovery,
  bounded retries, rate limiting, SHA-256 deduplication, provenance, and a
  separate worker process. CAPTCHA/authentication/anti-bot blocks become
  `MANUAL_ACTION_REQUIRED`; they are never bypassed.

### Extraction (`core/extraction.py`, `core/duration.py`, `core/reject_patterns.py`)
- Atomic, evidence-backed facts: award/sanction/LOA/work-order/agreement/
  site-handover dates, explicit commencement dates, base duration, extension
  options (count + months, kept **separate**, never collapsed), explicit
  calendar date ranges, absolute expiry ceilings, exercised extensions,
  contract value, vendor/organisation names.
- Every fact carries its evidence quote, character offsets, extraction rule
  ID (versioned), and its own fact-level confidence.
- **Reject-context classifier**: bid validity, delivery period, warranty/DLP,
  experience/turnover criteria, and PBG/EMD validity are recognized and
  rejected as duration lookalikes — each backed by a real example from the
  spec's research (Part 1, examples #3, #18, #19).
- Composite clauses ("1 year, extendable yearly up to 2 more years") produce
  **three separate facts** (base, extension-months, extension-count), never
  a single collapsed number — verified in `tests/test_extension_representation.py`.

### Start-date resolution (`core/start_date.py`)
Implements the exact hierarchy from spec §4.1. `award_date` is **never**
silently treated as `commencement_date`. If used as a proxy, that is stated
explicitly and confidence is capped at MEDIUM. If nothing usable exists,
resolution is UNKNOWN — the system does not guess.

### Expiry engine (`core/expiry_engine.py`)
- Explicit calendar start/end dates take precedence over arithmetic.
- `current_expiry_estimate` is **always** the conservative base term unless
  direct evidence of an *exercised* extension exists — an unexercised option
  never leaks into the customer-facing estimate.
- `current_expiry_ceiling` is the "if every extension were exercised"
  maximum, capped by any `absolute_expiry_ceiling` fact if present.
- Every calculation is stored (not overwritten) in `expiry_calculations`,
  with its formula, input fact IDs, and confidence.

### Corrigendum handling (`core/corrigendum.py`)
Classifies every corrigendum into `bid_deadline_extension`,
`substantive_duration_change`, or `administrative_clarification`. Only a
substantive duration change feeds `contract_timeline_events` and can alter
`current_expiry_estimate` — matching the spec's 7-for-7 real-world finding
that corrigenda essentially never touch contract duration.

### Cross-document reconciliation & conflicts (`core/reconciliation.py`)
- A new document's facts are compared against existing facts of the same
  type. Compatible values corroborate; contradictory values are **never**
  silently overwritten.
- "Later document wins" only applies when the new document's type **ranks
  at or above** the existing fact's document type on the evidence hierarchy
  — recency alone is never authority. A LOW-ranked department page dated
  later than a HIGH-ranked NIT does not override it.
- Equal-rank contradictions, and any case where a lower-ranked document
  disagrees with a higher-ranked one, are recorded in `fact_conflicts` and
  surfaced in the Review Queue, never auto-resolved.

### Confidence engine (`core/confidence.py`)
Six explainable dimensions (award-date, duration, start-date, source
reliability, cross-source verification, unresolved conflicts), collapsed
into `expiry_confidence` via a **rule-based floor, not a weighted sum** —
per spec §4.5's own reasoning: a weighted score could report a confident
date built on an unknown start; a floor can veto it. A missing or LOW
dimension always caps the result; nothing can be averaged away.

### Proof Packet (`core/proof_packet.py`)
Renders the exact block structure from spec Part 7 — facts + evidence +
calculation + confidence breakdown + exceptions + status + recommended
action — from the database, every time, never hand-written. Reproduces the
spec's own worked examples (Part 7 and Part 5.2) to the day.

### Database (`db/schema.sql`, `db/database.py`)
Implements the v2.0 evidence-first schema: `documents` (evidence ledger),
`extracted_facts` (one row per atomic claim, never flattened),
`contract_timeline_events`, `expiry_calculations` (append-only, "show your
work"), `confidence_assessment` (six dimensions, never collapsed elsewhere),
and `fact_conflicts`.

### Streamlit UI (`app.py`)
Dashboard · Document upload/ingestion · Contract search · Contract detail ·
Evidence view · Timeline · Proof Packet (downloadable) · Review queue ·
CSV export.

### Tests (`tests/`)
Fixture-driven from the spec's own real documents
(`tests/fixtures/real_examples.jsonl`), not synthetic strings alone:
- `test_context_reject.py` — one test per REJECT class against the actual
  sentence that proves it exists
- `test_extension_representation.py` — asserts composite clauses produce
  separate facts, never a collapsed number
- `test_start_date_unknown.py` — asserts the site-handover clause produces
  UNKNOWN, never a silent fallback to award date
- `test_hierarchy_precedence.py` — explicit dates outrank arithmetic;
  recency alone never overrides document-type authority
- `test_corrigendum_classifier.py` — real corrigenda classify as non-events;
  a synthetic substantive one is correctly flagged
- `test_conflict_flagging.py` — contradictory facts are flagged, never
  auto-resolved; compatible values corroborate
- `test_confidence.py` — the rule-based floor, not a weighted score
- `test_extension_handling.py` — ceiling never leaks into the current
  estimate without exercise evidence; absolute ceilings cap correctly
- `test_proof_packet.py`, `test_pipeline_end_to_end.py` — full pipeline,
  dedup, corrigendum non-events, extension exercise lifecycle

- `test_ingestion.py` — real PDF extraction (via reportlab-generated PDFs)
  through the actual PyMuPDF→pdfplumber→OCR fallback chain, hashing/dedup,
  and the public-URL-ingestion-is-not-faked guarantee
- `test_contract_matching.py` — near-duplicate contract titles are
  surfaced as a warning, never silently auto-merged

51 Layer 1 tests (unchanged, still all passing).

## What's implemented — Layer 2 (Procurement Cycle Intelligence)

Layer 2 answers: *does this government requirement appear to be procured
repeatedly, how often, and what's the historical cycle?* It is purely
descriptive history — it does not predict anything (that's Layer 3, not yet
built) and it never alters a Layer 1 contract's own expiry/status result.

### Requirement identity (`core/requirement_identity.py`)
A **requirement** is the stable underlying need (e.g. "CCTV AMC — XYZ
Hospital") that survives changes in tender title, tender number, incumbent
vendor, and document wording across multiple contracts over time. Identity
is built from two deterministic, auditable signals — no LLM, no free-text
NLP:
- **asset_keyword** — which of ~15 canonical taxonomy buckets (CCTV,
  housekeeping, security, fire safety, vehicle hiring, facility management,
  generator AMC, IT support, electrical maintenance, elevator AMC, pest
  control, canteen/catering, horticulture, water treatment, air
  conditioning) the text falls into, detected via a fixed synonym list. The
  reason for a match is always inspectable: "matched because the text
  contained the substring 'cctv'."
- **location** — a normalized token from a small gazetteer of ~45 common
  Indian city/administrative names, only when present in the text at all
  (most procurement text won't mention a city, and that must not be treated
  as a mismatch).

### Requirement matching (`core/requirement_matching.py`)
Every match is explainable — never a bare `MATCH = TRUE`. Statuses:
`STRONG_MATCH`, `PROBABLE_MATCH`, `REVIEW_REQUIRED`, `NOT_MATCHED`, each
returned with a `factors` dict (organisation / asset / location / title
similarity) and a human-readable `explanation`.

- **asset_keyword mismatch is always a hard gate** — housekeeping can never
  match CCTV, regardless of organisation, location, or title similarity.
- **organisation**: a *confirmed* conflict (both sides resolved to a
  different `org_id`) is a hard gate — two contracts known to belong to
  different organisations are never merged. Organisation being *unresolved*
  on one or both sides is explicitly **not** treated the same as a
  conflict — it earns no bonus, but the decision falls to the remaining
  signals, since demanding organisation resolution as a second hard gate
  would make Layer 2 useless whenever Layer 1's lightweight NER (see Layer
  1 limitations) hasn't fired, which is common.
- **vendor identity is never a factor** — per the spec, a vendor changing
  must never by itself create or block a requirement match.
- Location and normalized-title similarity (via stdlib `difflib`, same
  approach as the Layer 1 near-duplicate-contract check) are soft,
  score-adjusting factors only.

### Procurement events (`core/procurement_events.py`)
Derives a normalized event abstraction (`tender_published`, `tender_awarded`,
`contract_started`, `contract_extended`, `contract_ended`) **entirely from
evidence Layer 1 already produced** — `documents.doc_type`/`doc_date` and
specific `extracted_facts` rows (`award_date`, `commencement_date_explicit`,
`extension_exercised_new_end_date`, `termination_date`, etc.). Nothing is
re-extracted from raw text; every event carries its source `document_id`
and, where applicable, `source_fact_id`. Derivation is idempotent — running
it again on an unchanged contract creates no duplicates.

### Procurement cycles (`core/procurement_cycles.py`)
Computes measurable historical features — **not a single average presented
as a prediction**: `n_cycles`, the full `interval_days` list, median, mean,
min, max, and standard deviation of the gaps between consecutive
occurrences of one **anchor event type**. The anchor is chosen by a fixed
priority (`tender_published` > `tender_awarded` > `contract_started`),
picking the first type with at least 2 dated occurrences — this choice is
deterministic and stated in the result's `note`, not silently mixed across
event types with different real-world timing semantics. Fewer than 2 dated
occurrences of any anchorable type yields `insufficient_data = True`, never
a fabricated statistic. Every computation is stored append-only (same
"show your work" pattern as Layer 1's `expiry_calculations`), so a later
recomputation supersedes but never erases the previous one.

### Orchestration (`core/requirements_pipeline.py`)
`link_contract_to_requirement(cur, contract_id)` — called automatically at
the end of `core.pipeline.ingest_document` — builds the contract's
requirement signature, finds the best-scoring existing requirement (if any)
among those sharing the same `org_id` + `asset_keyword`, creates a new
requirement only when nothing matches, records the contract's title as an
alias (deduplicated), derives its procurement events, links them to the
requirement with full match evidence, and recomputes that requirement's
cycle statistics. A contract whose title/organisation doesn't match any
taxonomy entry simply isn't linked to a requirement (`reason_unlinked` is
set) — its events are still recorded for when/if a future document lets it
be classified.

### Database (`db/schema.sql`)
Purely additive: `requirements`, `requirement_aliases`, `procurement_events`,
`requirement_event_links` (the linkage's own match evidence — never a bare
foreign key), and `procurement_cycles`. No existing Layer 1 table was
modified.

### UI (`app.py` — "Requirements & Cycles" page)
Lists every identified requirement with its known aliases (original tender
titles), its derived procurement events, and its historical cycle
statistics (or an honest `INSUFFICIENT_DATA` message).

### Tests (31 new, on top of the original 51 — 82 total)
- `test_requirement_identity.py` — asset taxonomy across differently-worded
  titles, location gazetteer, signature building
- `test_requirement_matching.py` — hard gates (asset mismatch, confirmed
  org conflict), unresolved-org-is-not-a-conflict, every result carries an
  explanation, conflicting location lowers but doesn't block a score
- `test_procurement_cycles.py` — single-event and no-event
  `INSUFFICIENT_DATA`, strong annual recurrence (low dispersion), irregular
  cycles (high dispersion), anchor-priority selection and fallback,
  append-only supersession
- `test_procurement_events.py` — events derived from NIT/AOC/work-order
  evidence with correct provenance, idempotent re-derivation
- `test_requirements_pipeline.py` — **the directive's own motivating
  example**: four differently-worded NIT titles for the same underlying
  CCTV AMC requirement merge into exactly one requirement with all four
  aliases preserved and correct cycle statistics; an unrelated
  (housekeeping) tender at the same organisation does **not** merge; a
  confirmed different organisation with the same asset does **not** merge;
  Layer 2 linking never changes Layer 1's own expiry/status result

**82/82 tests passing** (51 Layer 1 + 31 Layer 2), verified together in one
run — Layer 2 is wired live into `pipeline.ingest_document`, not bolted on
as a separate code path.

### Known limitations — Layer 2 (by design, honestly stated)
- **No fuzzy title matching drives the asset/location signals** — detection
  is a fixed synonym/gazetteer lookup, not statistical NLP, by design (see
  spec: "do not introduce an unexplained LLM black box"). A requirement
  category or city spelled in a way not in these lists won't be detected;
  extend `ASSET_TAXONOMY`/`LOCATION_GAZETTEER` in
  `core/requirement_identity.py` as real documents reveal gaps.
- **Organisation resolution depends on Layer 1's lightweight NER**
  (`M/s <Name>`, `issued by <Org>` patterns) — when it doesn't fire,
  organisation is "unresolved" (not a conflict, per above) and the match
  decision falls more heavily on asset + location + title similarity, which
  is weaker evidence. A single organisation with many genuinely distinct
  sub-locations for the same asset type (e.g. a national authority managing
  several airports' CCTV separately) risks being under-split if
  organisation and location both fail to resolve — a known edge case, not
  silently hidden.
- **No Layer 3.** Nothing here predicts a future procurement window,
  matches an incoming tender against a prediction, or backtests anything.
  The cycle statistics this layer produces are exactly the structured input
  Layer 3 would need, but no prediction logic exists yet.

## Known limitations (MVP scope, by design)

- **No LLM in the core pipeline** — extraction, duration classification,
  start-date resolution, and expiry calculation are all deterministic
  regex/rule-based logic, per the build's explicit requirement. This trades
  some recall for explainability and determinism.
- **Contract matching is exact-title-only for auto-linking**, but the
  pipeline now runs a stdlib `difflib`-based near-duplicate check
  (`core/matching.py`) whenever a document creates a *new* contract: if an
  existing contract's title is ≥82% similar, `IngestResult` carries a
  `similar_contracts_warning` and the Streamlit upload page surfaces it —
  but nothing is ever auto-merged. `rapidfuzz` remains in `requirements.txt`
  for a future, more sophisticated matcher if needed; the stdlib approach
  was chosen so this feature works with zero extra dependencies.
- **No live government-portal scraping.** See "Source access" above —
  this is a deliberate compliance decision, not an oversight.
- **Vendor/organisation extraction is lightweight** (`M/s <Name>`,
  `issued by <Org>` patterns) — real NER was out of scope for the MVP.
- **Simple bare-number durations** ("12 months" with no nearby duration
  keyword) are extracted at MEDIUM confidence by design; the same number
  next to a duration keyword ("period of", "AMC for", "tenure") is treated
  as HIGH. This is a precision/recall tradeoff documented in
  `core/extraction.py::_duration_fact_confidence`.
- **OCR is a fallback, not a primary path** — it requires system packages
  (`poppler-utils`, `tesseract-ocr`) not installed by pip alone; without
  them, scanned/image-only PDFs with no text layer will fail ingestion with
  a clear error rather than silently producing empty text.

## Project structure

```
contract_expiry_radar/
├── app.py                       # Streamlit UI
├── core/
│   ├── ingestion.py              # PDF/text extraction, hashing, dedup
│   ├── extraction.py             # top-level fact extraction
│   ├── duration.py                # duration candidates + reject filtering
│   ├── reject_patterns.py        # bid-validity/DLP/PBG/etc. classifiers
│   ├── dateutils.py               # deterministic date parsing
│   ├── start_date.py              # start-date resolution hierarchy
│   ├── expiry_engine.py           # expiry calculation + extension logic
│   ├── corrigendum.py             # corrigendum classifier
│   ├── reconciliation.py          # cross-document conflict detection
│   ├── confidence.py              # six-dimension confidence engine
│   ├── proof_packet.py            # Proof Packet renderer
│   ├── matching.py                # near-duplicate CONTRACT title detection (Layer 1)
│   ├── requirement_identity.py    # Layer 2: asset taxonomy + location gazetteer
│   ├── requirement_matching.py    # Layer 2: explainable requirement matching
│   ├── procurement_events.py      # Layer 2: event derivation from Layer-1 evidence
│   ├── procurement_cycles.py      # Layer 2: historical cycle statistics
│   ├── requirements_pipeline.py   # Layer 2: orchestrator
│   └── pipeline.py                # orchestrates the full Layer 1 + Layer 2 pipeline
├── db/
│   ├── schema.sql                 # v2.0 evidence-first schema
│   └── database.py                # data access layer
├── demo_data/
│   └── demo_seed.py               # seeds demo contracts from the spec's own examples
├── tests/
│   ├── fixtures/real_examples.jsonl
│   └── test_*.py
├── requirements.txt
├── .env.example
└── README.md
```
