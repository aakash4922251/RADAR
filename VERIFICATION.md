# Verification — what was run, and what was not

Last run: 2026-10-18, Windows 11, Python 3.14, Streamlit 1.64, Tesseract 5.4.

## Verified

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
