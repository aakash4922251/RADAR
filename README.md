# Contract Expiry Radar

**Upload government procurement documents; see when each contract ends, with the
quote from the document that proves it.**

Accepted: tender notices (NIT), CPPP tender/award pages, award letters (LOA/AOC),
work orders, agreements, corrigenda and extension orders, as digital PDFs,
scanned PDFs, phone photos, Word, Excel, text, or a ZIP of these.

## Two pages, one app

| Page | URL | Who | What they see |
|---|---|---|---|
| **Public** | `/` (app.py) | Anyone with the link | A single upload box and their own results only — one browser session never sees what another session uploaded. No internal detail (extraction rules, raw document text, delete controls). |
| **Internal Dashboard** | `/Internal_Dashboard` | You, behind a password | Every contract from everyone, the full evidence breakdown, conflict resolution, contract removal, and database backup/restore. |

The public page never links to the internal one (its sidebar is hidden), so visitors
don't know it exists; the password is what actually protects it, not the hidden link.

## Run it locally

```bash
python -m pip install -r requirements.txt
winget install UB-Mannheim.TesseractOCR   # once, for scans and photos (Linux: apt-get install tesseract-ocr)
$env:ADMIN_PASSWORD = "pick-something"    # bash/Linux/macOS: export ADMIN_PASSWORD=pick-something
streamlit run app.py
```

Tests: `python -m pytest`

## Deploy it (free, ~5 minutes) — Streamlit Community Cloud

1. Push this repo to GitHub (already done if you're reading this from the repo).
2. Go to **share.streamlit.io**, sign in with GitHub, click **New app**.
3. Pick this repo, branch `main`, main file `app.py`. Deploy.
4. Before (or right after) it finishes building, open **Settings → Secrets** on the
   app and add:
   ```
   ADMIN_PASSWORD = "pick-something-only-you-know"
   ```
   Without this, the internal dashboard stays locked for everyone, including you.
5. `packages.txt` (already in the repo) tells Streamlit Cloud to install Tesseract,
   so OCR for scans and photos works on the deployed app too — nothing else to do.

Your repo stays the source of truth: redeploying (a new `git push`) always rebuilds
from GitHub, so your *code* is never at risk. What free hosts generally do **not**
guarantee is that the app's local disk — where the SQLite database lives — survives
a restart. That's what the next section is for.

### Storage: don't lose uploaded contracts

Streamlit Cloud (and most free hosts) can restart your app's container — after
inactivity, a redeploy, or routine maintenance — and the local filesystem may come
back empty when that happens. Your *code* is always safe (it's rebuilt from this
Git repo); what's at risk is the *data* — every contract anyone has uploaded.

The fix that ships in this repo: open the **internal dashboard → Database backup**,
click **Prepare a backup**, then **Download backup** — it's one `.db` file. If the
live app ever comes back with zero contracts, upload that same file back in under
**Restore from a backup file** and confirm. Get in the habit of backing up after a
batch of real uploads.

If you'd rather this happen automatically with no restart ever losing anything, the
real fix is an external database instead of a local file (e.g. a free Postgres on
neon.tech or supabase.com) — that's a larger change than this build includes today;
ask for it if you want it built out.

## What it tells you

For every contract, nearest end date first:

| Column | Meaning |
|---|---|
| Ends on / Days left | The end date the documents support. **"Not awarded yet"** for a tender: no contract exists until it is awarded. |
| Contract period | The term the documents state ("Time allowed: 15 days", "for a period of 2 years"), known even before the award. |
| Latest possible end | The end date if every extension option in the documents were used. |
| Evidence | Strong / Good / Weak, from the rule-based confidence check below. |
| Status | Plain-language state, e.g. "Documents disagree" when two documents contradict each other. |

Opening a contract shows the **Proof Packet** (every date with the sentence it came
from, the calculation and the confidence breakdown, downloadable), each extracted
fact, and the text read from every document.

## How an end date is worked out

1. **Read the text.** PDF text layer; OCR for scans and photos (pages turned upright).
2. **Type the document** (tender notice, award, work order, agreement…) and name the
   contract from its own "Name of Work" / "Subject" line.
3. **Group documents.** Files sharing a CPPP Tender ID or tender reference / NIT number
   join the same contract, so a tender notice uploaded today and its award letter
   uploaded next month are read together.
4. **Extract facts with evidence**: award / work-order / agreement dates, contract
   period, explicit "from … to …" ranges, extension options and exercised extensions,
   buyer and supplier. Look-alikes are rejected: bid validity, delivery and warranty
   periods, EMD/guarantee validity, bid-download windows, experience criteria.
5. **Start date**, most authoritative first: explicit start → commencement → site
   handover → work-order date (labelled assumption) → award date (labelled assumption).
   Nothing usable → **UNKNOWN**, never a guess.
6. **End date** = start + stated period, or the stated end of an explicit range.
   Unexercised extension options only move the *latest possible* end.
7. **Cross-check documents.** A stronger document (work order, agreement) replaces a
   weaker one's value and the replaced value stays visible; equal-rank disagreements
   are shown as "Documents disagree" for a person to settle.

## Known limits

- Rules, not AI: a wording the rules have not seen is reported as unknown rather than
  guessed. Uploading the same file again re-reads it with the latest rules
  ("Re-read all documents" does this for everything).
- OCR is English only; the Hindi part of bilingual letterheads is not read.
- Very low-resolution photos (under ~1,200 px) are flagged as unreliable.
