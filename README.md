# Contract Expiry Radar

**Upload government procurement documents; see when each contract ends, with the
quote from the document that proves it.**

Accepted: tender notices (NIT), CPPP tender/award pages, award letters (LOA/AOC),
work orders, agreements, corrigenda and extension orders, as digital PDFs,
scanned PDFs, phone photos, Word, Excel, text, or a ZIP of these.

## Run it

```bash
python -m pip install -r requirements.txt
winget install UB-Mannheim.TesseractOCR   # once, for scans and photos (Linux: apt-get install tesseract-ocr)
streamlit run app.py
```

Tests: `python -m pytest`

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
