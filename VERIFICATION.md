# Verification — what was run, and what was not

Last run: 2026-10-19, Windows 11, Python 3.14, Streamlit 1.64, Tesseract 5.4.

## Public/internal split and deployment package — 2026-10-19

* `python -m pytest`: still **85 passed** (this round only touched `app.py`, the new
  `pages/01_Internal_Dashboard.py`, `ui_theme.py`, `ui_shared.py` — no `core`/`db` logic
  changed).
* Both pages driven end-to-end under real Streamlit (`AppTest`), against a real tracked
  document and a real backup/restore cycle, not just a page load:
  - **Public page**, empty session → upload one award letter → a result card renders
    with the correct computed end date and an evidence pill; no exceptions.
  - **Internal page**: no `ADMIN_PASSWORD` set → locked for everyone with a clear
    message, no password box offered at all (fails closed, not open). Wrong password
    → rejected. Correct password → full portfolio (every contract from every session,
    confirming the public page's one-session-only view is a real boundary, not just
    hidden UI) with correct KPI counts; opening a contract renders all three detail
    tabs and the Proof Packet.
  - **Backup**: produced real, non-empty bytes via SQLite's own `backup()` API (safe
    under WAL mode).
  - **Restore**: two real bugs were caught by testing the destructive path, not just
    the happy path, and both are fixed and re-verified:
    1. Uploading a non-database file crashed the whole page with an uncaught
       `sqlite3.DatabaseError` instead of a friendly message — now caught and shown
       as an error, live data left untouched.
    2. Replacing the live database file raised `PermissionError: file ... being used
       by another process` on Windows (a stale Python traceback object kept the old
       SQLite connection's file handle alive past `.close()`) — fixed with an explicit
       `gc.collect()` plus a short bounded retry before deleting/replacing files.
    After the fix: a valid backup file restores cleanly, the session stays logged in
    (no re-login forced by a data change), and the live `.db` on disk was independently
    opened afterward and confirmed to have the full correct schema and the restored
    (zero) contract count — not just "no exception raised".
* `ADMIN_PASSWORD` lookup checks `os.environ` then falls back to `st.secrets`, so it
  works however Streamlit Community Cloud exposes a Secrets-panel value, without
  betting on one specific undocumented-to-us behavior.

NOT verified: an actual Streamlit Community Cloud deployment (no account exists to
deploy to from this session — the steps in README → Deploy are correct to the best
of available knowledge but untested live); a real external Postgres/Neon database
(no live instance was reachable from this sandbox — no driver, no local server, and
Docker is installed but its engine isn't running here) — the backup/restore feature
above is the verified fix for data durability in this build, not a database swap.

## Previous round



* `python -m pytest`: **85 passed.** Real documents under `tests/fixtures/` pin each fix:

  | Document | Result |
  |---|---|
  | BSF Gandhinagar NIT (67 pages, CPWD boilerplate) | Tender stage, contract period 15 days ("Time allowed : 15 Days"), no false conflict from boilerplate periods, buyer "Assistant Commandant (Works), SHQ BSF Gandhinagar" |
  | Same NIT + an award letter quoting its N.I.T No. | Linked automatically; ends 2026-12-05 (award 20 Nov + 15 days) |
  | MoRTH NH-66 tender notice + portal tender page | Linked by tender reference; tender stage; buyer = MoRTH organisation chain; bid-download window not read as a contract period |
  | VNIT CPPP award summary | Ends 2026-10-21 (contract date 21 Sep + 30 days) |
  | VNIT work order, scanned PDF (OCR) and photos of it, upright and rotated 90° | Ends 2026-10-06 (work order 16 Sep + 20 days), supplier read from the addressee |
  | Same award summary + work order | Work order outranks the summary; replaced 30-day term stays visible |

* The app (`app.py`) driven under real Streamlit with these documents: upload, list,
  contract detail, re-read, remove.

## Not verified

* Real phone photos (glare, curled pages, perspective).
* Document layouts beyond the fixtures above.
* Hindi text (English OCR data only).

The full earlier build (public award-data screens, buyer profiles, repeat buyers,
backtests) is preserved on branch `backup/full-build-2026-10-18`.
