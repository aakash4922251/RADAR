# VERIFICATION - what was actually run, and what was not

## OCR, photos and Office files — 2026-10-18

* Tesseract 5.4.0 installed (winget, UB-Mannheim build: English + orientation data). Pages render
  with PyMuPDF, so Poppler is not needed. **168 passed, 0 skipped** (the VNIT OCR regression now runs).
* Real scanned VNIT work order (5 pages, ~11 s): work-order date 16 Sep 2026 from the heading,
  "completed within 20 Days from the date of work order", supplier from the addressee block, buyer from
  the letterhead -> ends 2026-10-06 (MEDIUM: work-order date as stated start proxy).
* Photos (JPEG from page 1 of that scan): upright and rotated 90° both give 2026-10-06 and the
  supplier (~2-3 s); a 621x877 px, quality-40 photo cannot be read reliably and now carries a
  "resolution is low" warning instead of failing silently.
* Fixed: .docx/.xlsx were decoded as raw text (garbage); now read with python-docx/openpyxl. Old
  .doc/.xls are refused with a clear message.
* Bid-opening committee lists (CPPP fin/tech summaries) contain no contract terms and are no longer
  added as contracts.
* NOT verified: real phone photos (glare, curled pages, perspective), Hindi text (only English OCR
  data installed).

## Real-document fixes — 2026-10-18

Four real CPPP documents uploaded by the user exposed six extraction bugs; each is now pinned by
`tests/test_real_documents.py` (fixtures in `tests/fixtures/real_docs/`). **158 passed, 1 skipped**
(the skip is the OCR-only VNIT regression; Tesseract is not installed on this machine).

| Document | Before | After |
|---|---|---|
| MoRTH NH-66 tender notice | End 2026-09-28, HIGH confidence (bid-download window read as contract period) | No end date; stage Tender |
| Same tender, portal details page | CONFLICTING (LOA read inside "Download"; fee/EMD/GST and "Covers" read as values; portal banner as buyer) | Linked to the notice by tender reference; buyer = MoRTH organisation chain; value ₹36,25,764 only |
| VNIT AOC summary | No end date; typed as a tender notice | AOC; 2026-09-21 + 30 days = 2026-10-21 (MEDIUM, contract date as start proxy) |
| VNIT work order (scanned) | Empty contract saved (only signature text) | Reported as "Scanned image, OCR needed"; nothing saved |

Rules added: bid-schedule guard for date ranges; portal work-period fields; "completed within N
days"; day-based terms counted in days; work-order date as a labelled start proxy; cross-document
only reconciliation; stronger name evidence supersedes weaker; tender-ID/reference linking.

## Contract tracker ("My contracts") — 2026-10-18

* `python -m pytest`: **151 passed, 1 skipped** (7 tracker tests: auto document typing, subject-line
  naming, ZIP unpacking, duplicate detection, one-contract option, bad files not stopping a batch,
  ordering by end date, letterhead buyer detection; plus a NaN-safe buyer label test).
* Driven under real Streamlit with a reportlab-generated award-letter PDF and a work order: the PDF
  gave end 2027-04-02 with a latest possible end of 2028-04-02 (the "extendable yearly up to 2 more
  years" clause), and the work order gave 2026-11-30 at HIGH confidence. Proof Packet rendered.
* NOT verified: real scanned PDFs (OCR needs Tesseract + Poppler, not installed here) and real-world
  document layouts beyond the fixtures.

## Data-refresh feasibility — 2026-10-18 (6 polite requests to CPPP/NIC portals)

* `eprocure.gov.in/robots.txt`: 404 (no declared crawl rules; not the same as permission).
* CPPP Terms of Use / Copyright Policy: content is owned by each buyer organisation and "may be
  reproduced free of charge after taking proper permission from the respective Organisation /
  Department", accurately, with the source prominently acknowledged.
* CPPP "Result of Tenders" (`/cppp/resultoftendersnew`) requires an image CAPTCHA to search; no
  award rows are listed without one. This system never bypasses CAPTCHAs, so automated AOC
  collection from that page is not possible.
* AOC detail links stored in the dataset (`/cppp/aocfullview/...`) carry an embedded timestamp and
  now answer "Invalid Url"; NIC GePNIC tender links answer "Unauthorized Page … Re-Login". Both are
  session-bound. The app therefore shows Tender IDs (searchable on Result of Tenders) instead of links.
* The bundled history comes from a third-party scrape (tender.sarthaksidhant.com, snapshot
  2026-06-26) re-published on GitHub (one release, 2026-06-27, no update schedule, no data licence).
* No official open-data feed or public API for CPPP awards was found (data.gov.in, CPPP, GeM).


Verified (executed):
* `python -m pytest`: **143 passed, 1 skipped** (17 new tests in `tests/test_contract_expiry.py`:
  window and as-of logic, prior-award counts, short-job and 01-Jan-1900 placeholder exclusion,
  SQL-safe filters, coverage, backtest scoring, buyer profile aggregation, placeholder suppliers
  and same-day lots, PDF rendering, INR formatting).
* Real data: CPPP `aoc_details` 2021–2026 (618,988 award records). 23,791 contracts carry a usable
  published completion date, from 12 buyers. As of 18 Oct 2026: 1,690 contracts end within 6 months,
  ₹3,020.78 Cr combined, 300 previously awarded by the same buyer.
* Expiry backtest (`core.contract_expiry.backtest_contract_expiry`, 6-month horizon, ±90 days):

  | as of      | tested | re-awarded later | of those, within 90 d of end | award near end | wrong-time window |
  |------------|-------:|-----------------:|-----------------------------:|---------------:|------------------:|
  | 2023-07-01 |  1,595 |              12% |                          38% |           4.5% |              4.5% |
  | 2024-01-01 |  1,687 |              12% |                          47% |           5.6% |              3.9% |
  | 2024-07-01 |  1,345 |              11% |                          58% |           6.6% |              3.0% |
  | 2025-01-01 |  1,808 |              13% |                          62% |           8.1% |              3.9% |

* Every page (Contracts ending, Buyer profiles, Repeat buyers, Check a contract, Live tenders,
  Data Center, Search, Radar, Home) driven under real Streamlit 1.64 via `AppTest`, including
  filters, the accuracy panels, buyer search and PDF generation: no exceptions.
* The BPCL Opportunity Brief PDF was rendered to images and reviewed page by page.
* Fixed: pages left SQLite write transactions open (leaked connections), which made a second
  session or process fail with "database is locked". Every page run now closes its connections.

NOT verified: real users; a browser review of the new screens by a human; supplier-name merging.

## MVP pass — 2026-10-18 (Windows 11, Python 3.14, Streamlit 1.64, real pytest)

Verified (executed):
* `python -m pytest`: **123 passed, 1 skipped.** The skip is the VNIT work-order regression,
  whose sample PDF was never committed; drop it into `tests/fixtures/` to run it.
  (The "200 tests" and `tools/mini_pytest_runner.py` described below are not in this repo.)
* Every primary page (Opportunity Finder, Live tenders, Check a contract, Search, Data Center,
  Radar, Home) executed under real Streamlit via `streamlit.testing.v1.AppTest` with no exceptions.
* Opportunity Finder on the real CPPP award history (2,672,343 AOC records, 2021–2026):
  2,106 repeat requirements with medium/strong evidence across 232 buyers.
  First search ~20–25 s, then cached per filter combination.
* **Backtest** (`scripts/backtest_opportunities.py`, leak-free: only awards before the as-of
  date are used; scored against awards up to 2026-06-16, the 99.9th-percentile date, because
  the source has a typo tail after its real June 2026 cut-off). 6-month horizon:

  | as of      | evidence | tested | bought again later | hit in predicted window | hit in window half a cycle off |
  |------------|----------|-------:|-------------------:|------------------------:|-------------------------------:|
  | 2024-01-01 | Strong   |     54 |                85% |                     44% |                            46% |
  | 2024-01-01 | Medium   |    314 |                70% |                     28% |                            32% |
  | 2024-07-01 | Strong   |    105 |                80% |                     27% |                            26% |
  | 2024-07-01 | Medium   |    359 |                60% |                     19% |                            23% |
  | 2025-01-01 | Strong   |    103 |                77% |                     38% |                            36% |
  | 2025-01-01 | Medium   |    435 |                49% |                     25% |                            27% |
  | any        | Early    |  ~3.8k |             20–26% |                   9–10% |                           7–8% |

  Conclusion: recurrence ("this buyer will buy this again") is real for Strong/Medium; timing
  from award history is no better than chance. Using bid `closing_date` instead of `aoc_date`
  gave the same picture. The Finder therefore shows repeat buyers and no date windows.
* Title normalisation fix (strip the per-cycle reference after "/ ", digits, HTML entities,
  filler words; drop titles with < 2 meaningful words) raised recurring signals from 530 to
  6,568 over 12 months, and from 0 to 10 for "housekeeping" / 18 for "CCTV".

NOT verified:
* Whether contract end dates from "Check a contract" predict the next tender (needs real contract documents).
* The live CPPP scan against the real portal.
* The UI in a browser by a human.

Environment: sandbox with **no network**, Python 3.12.

## Verified (executed)
* **200 tests pass** (`python tools/mini_pytest_runner.py .`).
  CAVEAT: pytest could not be installed offline, so these were run with a small pytest-compatible runner in `tools/`
  (supports the fixtures / parametrize / raises / approx / importorskip used here). The test files are standard pytest;
  **run `pytest` yourself** to confirm under the real thing.
* **Mutation check:** re-introducing 5 bugs (off-by-one expiry, work-order date single-valued [original bug #2], new-end cue
  disabled [original bug #1], HIGH prediction without 4 intervals, corrigendum filter off) made 17 / 2 / 20 / 3 / 1 tests fail.
* Both original known bugs are fixed and regression-tested; the "Extension work order dated 15-Feb-2028. New contractual end
  date is 31-Mar-2028." case yields extension_order_date=2028-02-15, new end=2028-03-31, current expiry 2028-03-31.
* Real PDFs (generated with reportlab) through the **pdfplumber** path incl. page-number citation.
* **OCR** on an image-only PDF (tesseract + poppler were available here); the test skips itself where they are absent.
  OCR accuracy on real scanned tenders is unknown - only a clean synthetic image was tried.
* URL fetcher against a **local** HTTP server: text/PDF/HTML success; robots.txt, 403, 429, CAPTCHA and login refusals.
* SQLite schema v3 initialises; constraints reject `is_prediction=0` and non-prediction `kind`.
* End-to-end demo: `python demo_data/demo_seed.py --reset --as-of 2026-09-20` produces the scenarios in the README.
* `app.py` compiles and every page executes against seeded data **under a stub Streamlit** (`tests/streamlit_stub.py`).

## NOT VERIFIED
* **Real Streamlit was never started** (not installable offline). The stub proves the code runs; it does not prove real
  widget behaviour or layout. Run `streamlit run app.py` and look.
* **PyMuPDF (fitz) path**: not installed here, so not exercised (the pdfplumber fallback is).
* **Real government portals** (CPPP, GeM, state sites): no live access attempted; extraction was tested on synthetic text and
  the small `real_examples.jsonl` fixture only. Accuracy on real, messy tender PDFs is **unknown**.
* Prediction quality on real data is untested; thresholds (CV cut-offs, 90-day merge gap, +/-30d window floor) are reasoned
  defaults, not calibrated.
* The earlier session's claims of "37/51 tests" and a "FINAL zip" could not be reconciled with any file provided; this build
  was made from the PRE_BUGFIX zip.
