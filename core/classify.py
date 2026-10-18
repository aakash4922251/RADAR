"""
What kind of procurement document is this, and what does it call the work?

Keyword scoring over the first part of the text decides the document type
(using the pipeline's doc_type values directly); the work's name comes from
the document's own "Name of Work" / "Subject" line. Nothing is inferred
beyond what the document says.
"""
from __future__ import annotations

import io
import re
import zipfile
from typing import Iterator, Optional

# (group, doc_type, phrases). Groups are scored separately: generic contract
# words and agreement words must not add up to outweigh a tender notice.
_GROUPS = (
    # CPPP "Tender Summary Report" after award: "Accepted-AOC", "Contract Date", "AOC document".
    ("aoc", "aoc", ["award of contract", "aoc", "successful bidder", "contract price", "accepted-aoc",
                    "contract date", "aoc document", "awarded bids list"]),
    ("work_order", "work_order", ["work order", "work order no", "date of work order", "you are requested to commence"]),
    ("contract", "signed_agreement", ["agreement between", "contract agreement", "contract period",
                                      "commencement date", "contractor"]),
    ("tender", "bid_document", ["tender id", "tender reference number", "critical dates", "bid submission",
                                "tender value"]),
    ("nit", "nit", ["notice inviting tender", "notice inviting e-tender", "nit", "inviting authority",
                    "tender notice"]),
    ("award", "loa", ["letter of award", "award letter", "award of work", "successful bidder"]),
    ("corrigendum", "corrigendum", ["corrigendum", "amendment to tender", "revised bid submission"]),
    ("agreement", "signed_agreement", ["memorandum of agreement", "agreement executed", "executed agreement"]),
)


def classify_document(filename: str, text: str) -> str:
    """Pipeline doc_type for a document; "other" when nothing matches."""
    # PDF line breaks and file-name underscores split phrases ("Notice\ninviting tender").
    hay = re.sub(r"[\s_]+", " ", (filename + "\n" + text[:25000]).lower())
    scores = {group: sum(phrase in hay for phrase in phrases) for group, _, phrases in _GROUPS}
    doc_type = {group: kind for group, kind, _ in _GROUPS}
    # Explicit work-order or award-of-contract wording wins over generic contract words.
    for group in ("work_order", "aoc"):
        if scores[group] >= 2:
            return doc_type[group]
    best = max(scores, key=scores.get)
    return doc_type[best] if scores[best] else "other"


_TITLE_PATTERNS = (
    # NIT cover sheet field "Name of Work : ..." (the colon tells a field from a table header)
    re.compile(r"Name\s*of\s*Work\s*:\s*([\s\S]{15,300}?)\n\s*(?:Estimated|Earnest|Approx|Tender|Period|Time)",
               re.I),
    re.compile(r"(?:Sub\.|Subject\s*:)[-\s]*(?:work order for\s+)?(.{20,350}?)(?:\n|\.)", re.I | re.S),
    re.compile(r"Tender\s*Title\s*:?\s*([\s\S]{15,300}?)\n\s*\n", re.I),          # CPPP tender summary
    re.compile(r"\bTitle\s+([\s\S]{15,300}?)\s+Work\s*Description\b", re.I),       # NIC tender details page
)


def work_title(text: str) -> Optional[str]:
    """The document's own name for the work, or None if it states none."""
    for pattern in _TITLE_PATTERNS:
        m = pattern.search(text)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip(" .,:;-")
            if len(title) >= 12:
                return title[:160]
    return None


def iter_zip(data: bytes, prefix: str = "") -> Iterator[tuple[str, bytes]]:
    """Files inside a ZIP (nested ZIPs included), as (path, bytes)."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            if info.is_dir() or info.filename.startswith("__MACOSX/"):
                continue
            payload = z.read(info)
            if info.filename.lower().endswith(".zip"):
                yield from iter_zip(payload, prefix + info.filename + "/")
            else:
                yield prefix + info.filename, payload
