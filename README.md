# Government Procurement Re-Opportunity Radar

India-focused **procurement lifecycle intelligence**: turn official procurement documents (NIT, AOC, LOA, work order,
agreement, extension order, corrigendum...) into auditable, evidence-backed answers to two questions:

1. **When does this government contract actually end?** (expiry engine, extensions, confidence, Proof Packet)
2. **Which requirements are likely to be procured again, roughly when, and why?** (cycle detection + predicted windows)

> **Contract expiry != guaranteed new tender.**
> *Expiry* is **evidence**. A *recompete* is a **signal**. A *prediction* is a **forecast**. A *new tender* is an **event**.
> The three are kept separate in the schema, the code and the UI. A prediction is never stored or shown as a fact.

This is **not** a tender aggregator or a chatbot. The product is the lifecycle layer that connects documents over time.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate      # optional
pip install -r requirements.txt
python demo_data/demo_seed.py --reset --as-of 2026-09-20   # SYNTHETIC demo data (see below)
streamlit run app.py                                        # set the sidebar as-of date to 2026-09-20 for the intended demo
pytest                                                      # full test suite
```
Config: copy `.env.example` -> `.env` (only `CER_DB_PATH` matters; default `data/radar.db`).

## What the demo shows (all data is SYNTHETIC - invented organisations, vendors, dates, values)

| Scenario | Demonstrates |
|---|---|
| Fire Alarm AMC | HIGH confidence, WATCH, extension *option* not assumed (ceiling shown separately) |
| CCTV AMC (4 cycles) | recurring procurement -> **PREDICTED WINDOW** (MEDIUM), RECOMPETE_SIGNAL; older cycles show NEW_PROCUREMENT |
| Security Services | explicit calendar range -> HIGH with no award date needed |
| Housekeeping | duration but no start evidence -> expiry UNKNOWN, never invented |
| Vehicle Hiring | extension **exercised**: original expiry preserved, current expiry revised, EXTENDED |
| Canteen AMC | bid-deadline corrigendum is a non-event for expiry |
| Lift AMC | genuinely contradictory work orders -> Review queue |

## Pipeline

```
document -> text (+page offsets) -> clause-aware fact extraction -> facts (quote, page, rule, confidence)
 -> reconciliation (supersede / chain / conflict) -> expiry engine (original + revision chain)
 -> confidence (dependency-aware, explained) -> timeline -> requirement + procurement events
 -> prediction evaluate/refresh -> lifecycle status -> Proof Packet -> SQLite -> Streamlit
```

### Key design decisions
* **Semantic date roles.** Every date gets a role from a cue right next to it, inside one clause; a date is claimed once.
  So `extended vide this work order dated 15-Feb-2028; new end date shall be 31-Mar-2028` yields
  `extension_order_date=15-Feb-2028`, `new end=31-Mar-2028` (the original bug took the first date).
* **Inclusive end dates.** 12 months from 01-Apr-2026 ends **31-Mar-2027** (the earlier code produced 01-Apr-2027).
* **History is never deleted.** Superseded facts stay with `superseded_by_fact_id`; every recalculation is kept in
  `expiry_calculations`; the original expiry stays on the contract next to the current one.
* **Option vs exercised.** "may be extended" / "extendable" only raise the *ceiling*; only an exercised extension moves expiry.
  An extension stated as exercised whose end cannot be determined is flagged, not guessed.
* **Reject lookalikes** at clause level: bid validity, delivery, warranty/DLP, experience, PBG/EMD, payment, notice period,
  mobilisation, tender-schedule windows.
* **Confidence gates only on what the expiry depends on.** An explicit calendar range needs no award date; an award-date
  *proxy* caps confidence at MEDIUM and is labelled as an assumption. Confidence always comes with a plain-English reason.
* **Corrigenda** that only move a bid deadline / clarify administratively can never inject contract dates or durations.
* **Predictions** (`core/prediction.py`): one anchor per contract from one consistent event kind; intervals -> median ->
  window; confidence from number of intervals and regularity. **HIGH needs >= 4 intervals, low variance and expiry aligned
  with the window**; an exercised extension, a >35% shift in interval, or misaligned expiry cap it. Predictions are
  CONFIRMED (with timing error) or INVALIDATED when the loop closes, and are always labelled *PREDICTED PROCUREMENT*.
* **Requirements** are grouped per organisation by fuzzy title match; auto-link only above a strict threshold; contracts are
  **never** auto-merged (near-duplicates are a warning).

### Statuses
*Evidence status* (`contracts.status`): VERIFIED, HIGH_CONFIDENCE, MEDIUM_CONFIDENCE, NEEDS_REVIEW, CONFLICTING_EVIDENCE, INSUFFICIENT_EVIDENCE.
*Lifecycle status* (`lifecycle_status`): NEW_PROCUREMENT > EXPIRED / EXPIRED_UNVERIFIED > EXTENDED > RECOMPETE_SIGNAL > WATCH > UNKNOWN.
`EXPIRED` requires human verification; otherwise a passed expiry is `EXPIRED_UNVERIFIED` (an unpublished extension may exist).

## Layout
```
app.py                 Streamlit UI (Dashboard, Contract detail, Predictions, Review queue, Ingest, Guide)
core/                  textutils, dateutils, reject_patterns, evidence, duration, extraction, start_date, expiry_engine,
                       reconciliation, confidence, corrigendum, ingestion, pipeline, proof_packet, requirements,
                       prediction, lifecycle, review, queries, categorise
db/                    schema.sql (v3), database.py
demo_data/demo_seed.py synthetic scenarios
tests/                 pytest suite + fixtures/real_examples.jsonl (+ Streamlit stub, PDF factory)
tools/                 mini pytest stand-in used only where real pytest could not be installed
```

## Ingestion and access rules
PDF upload (PyMuPDF -> pdfplumber -> OCR fallback), text, and public URLs. The URL fetcher respects `robots.txt`, refuses
401/403/429 and CAPTCHA/login pages, and **never** tries to get around them - download the file and upload it instead.
Only use legally accessible public information and respect source terms.

## Known limitations / not implemented
* No live CPPP / GeM / state-portal connectors. **Real portals are untested.**
* Not implemented: start = award + mobilisation period; `NO_RECOMPETE_EVIDENCE` status; alert *generation* (tables exist);
  early-termination override of expiry (a termination fact is extracted and shown, not applied).
* Extraction is deterministic regex/rules: unusual phrasing will be missed (it errs towards UNKNOWN rather than guessing).
  Vendor/organisation extraction is lightweight. Requirement matching is title-based.
* No DB migration: an older-schema database is refused (delete it and re-ingest).
* Everything runs on Python + SQLite + Streamlit; no LLM is used. If one is added later, every derived fact still needs evidence.

See `VERIFICATION.md` for exactly what was and was not verified.
