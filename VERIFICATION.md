# VERIFICATION - what was actually run, and what was not

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
