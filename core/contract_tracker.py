"""
Contract tracker: drop in award letters, work orders, agreements (PDF, text,
Office files or ZIPs of them) and get every contract's evidence-backed end
date, nearest first.

This is a thin, automatic front door to the existing evidence pipeline
(core.pipeline.ingest_document). It only decides three things the user
would otherwise type by hand: the document type (core.procurement_intelligence
classifier), the contract name (the document's subject line), and whether
several files belong to one contract (the user says so explicitly). All
dates, confidence and Proof Packets still come from the pipeline itself.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

from core import pipeline
from core.ingestion import (
    IMAGE_EXTENSIONS, LEGACY_OFFICE_EXTENSIONS, SUPPORTED_EXTENSIONS, hash_text, ingest_uploaded_file,
    looks_scanned, ocr_available,
)
from core.procurement_intelligence import classify_document, extract_facts, iter_zip
from db import database as db

# Classifier labels -> pipeline document types (db/schema.sql doc_type values).
DOC_TYPE_MAP = {
    "AOC": "aoc",
    "AWARD": "loa",
    "WORK_ORDER": "work_order",
    "CONTRACT": "signed_agreement",
    "AGREEMENT": "signed_agreement",
    "CORRIGENDUM": "corrigendum",
    "NIT": "nit",
    "TENDER": "bid_document",
}


AWARDED_DOC_TYPES = {"aoc", "loa", "work_order", "signed_agreement", "site_handover_letter", "sla_stc"}
TENDER_DOC_TYPES = {"nit", "bid_document", "corrigendum"}

# Identifiers that tie documents of one tender together.
_TENDER_ID_RE = re.compile(r"\b(20\d{2}_[A-Za-z0-9]+_\d+_\d+)\b")
_TENDER_REF_RE = re.compile(
    r"(?:Tender\s*Ref(?:erence)?\.?\s*(?:Number|No\.?)?\s*:?\s*|^\s*No\.\s*)"
    r"([A-Za-z0-9][^\n]{5,80}?)(?=\s+Dated\b|\s*$)",
    re.I | re.M,
)
_TITLE_PATTERNS = (
    re.compile(r"Tender\s*Title\s*:?\s*([\s\S]{15,300}?)\n\s*\n", re.I),          # CPPP tender summary
    re.compile(r"\bTitle\s+([\s\S]{15,300}?)\s+Work\s*Description\b", re.I),       # NIC tender details page
)


def _normalise_key(value: str) -> str:
    key = re.sub(r"\s+", "", value.lower())
    return re.sub(r"^no\.?", "", key)


def tender_keys(text: str) -> list[str]:
    """CPPP Tender IDs and tender reference numbers in a document, normalised
    (lower case, no spaces, no leading "No.") so the same reference matches
    however it was typeset. Short or digit-free tokens are ignored."""
    keys = [_normalise_key(m.group(1)) for m in _TENDER_ID_RE.finditer(text)]
    for m in _TENDER_REF_RE.finditer(text):
        key = _normalise_key(m.group(1))
        if len(key) >= 8 and re.search(r"[/_]", key) and len(re.findall(r"\d", key)) >= 2:
            keys.append(key)
    return list(dict.fromkeys(keys))


def find_contract_for_keys(cur, keys: list[str]) -> tuple[Optional[int], Optional[str]]:
    """The one tracked contract whose documents mention any of ``keys``.
    Ambiguous matches (several contracts) link nothing."""
    for key in keys:
        rows = cur.execute(
            """SELECT DISTINCT contract_id, raw_text FROM documents
               WHERE contract_id IS NOT NULL AND instr(replace(replace(replace(replace(replace(lower(raw_text),
                     ' ', ''), char(10), ''), char(13), ''), char(9), ''), char(160), ''), ?) > 0""",
            (key,),
        ).fetchall()
        # ".../1" must not match ".../13"; letters may follow ("...2026-27Dated").
        boundary = re.compile(re.escape(key) + r"(?!\d)")
        ids = {r["contract_id"] for r in rows if boundary.search(re.sub(r"\s+", "", r["raw_text"].lower()))}
        if len(ids) == 1:
            return ids.pop(), key
    return None, None


@dataclass
class TrackedFile:
    filename: str
    status: str                       # ADDED | ALREADY_TRACKED | FAILED | SKIPPED
    doc_type: Optional[str] = None
    contract_id: Optional[int] = None
    contract_title: Optional[str] = None
    end_date: Optional[str] = None
    expiry_confidence: Optional[str] = None
    message: str = ""


def expand_uploads(files: Iterable[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    """Unpack ZIPs (nested too) and keep supported document types only."""
    out = []
    for name, data in files:
        ext = os.path.splitext(name)[1].lower()
        if ext == ".zip":
            out.extend(iter_zip(data, prefix=name + "/"))
        else:
            out.append((name, data))
    return out


def content_title(text: str) -> Optional[str]:
    """The document's own name for the work: its subject line, or the tender
    title printed by the portal. None when the document states neither."""
    for fact in extract_facts(text, "OTHER"):
        if fact.fact_type == "requirement":
            title = re.sub(r"\s+", " ", fact.value).strip(" .,:;-")
            if len(title) >= 12:
                return title[:160]
    for pattern in _TITLE_PATTERNS:
        m = pattern.search(text)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip(" .,:;-")
            if len(title) >= 12:
                return title[:160]
    return None


def filename_title(filename: str) -> str:
    stem = os.path.splitext(os.path.basename(filename))[0]
    stem = re.sub(r"[_\-]+", " ", stem).strip()
    return stem[:160] or "Untitled contract"


def contract_title_for(filename: str, text: str) -> str:
    """Name a new contract after the document's own title; fall back to the
    file name. Never invents wording that is not in the document or its name."""
    return content_title(text) or filename_title(filename)


def _upgrade_fallback_title(cur, contract_id: int, title: Optional[str]) -> None:
    """A contract first named after a file takes the real title as soon as a
    linked document states one."""
    if not title:
        return
    contract = db.get_contract(cur, contract_id)
    fallbacks = {filename_title(d["doc_title"] or "") for d in db.list_documents_for_contract(cur, contract_id)}
    if contract and contract["title"] in fallbacks:
        cur.execute("UPDATE contracts SET title = ? WHERE id = ?", (title, contract_id))


def track_documents(cur, files: Iterable[tuple[str, bytes]], *, same_contract: bool = False,
                    contract_id: Optional[int] = None) -> list[TrackedFile]:
    """Add each file to the tracker. With ``same_contract`` (or an explicit
    ``contract_id``) every file is attached to one contract; otherwise each
    file starts its own contract unless its text was already tracked."""
    results: list[TrackedFile] = []
    target = contract_id
    for name, data in expand_uploads(files):
        ext = os.path.splitext(name)[1].lower()
        if ext not in SUPPORTED_EXTENSIONS | LEGACY_OFFICE_EXTENSIONS:
            results.append(TrackedFile(name, "SKIPPED", message="Unsupported file type."))
            continue
        if ext in IMAGE_EXTENSIONS and not ocr_available():
            results.append(TrackedFile(name, "FAILED", message="Photos need text recognition (OCR), which is not installed."))
            continue
        try:
            ingested = ingest_uploaded_file(data, name)
            text = ingested.raw_text
            if name.lower().endswith(".pdf") and not ingested.used_ocr and looks_scanned(text):
                raise ValueError(
                    "Scanned image: this PDF has no readable text, only a signature stamp. "
                    + ("Text recognition (OCR) could not read it."
                       if ocr_available() else
                       "Install text recognition (OCR) to read scanned documents.")
                )
            if not text.strip():
                raise ValueError("No readable text in this file.")

            existing = db.find_document_by_hash(cur, hash_text(text.strip()))
            if existing is not None and existing["contract_id"] is not None:
                # Same document uploaded again: read it (and its contract) again with
                # the current rules, so improvements reach documents added earlier.
                pipeline.reread_contract(cur, existing["contract_id"], doc_type_for=_doc_type_for_row)
                if same_contract and target is None:
                    target = existing["contract_id"]
                contract = db.get_contract(cur, existing["contract_id"])
                conf = db.get_latest_confidence(cur, existing["contract_id"])
                results.append(TrackedFile(
                    filename=name, status="ALREADY_TRACKED",
                    doc_type=db.get_document(cur, existing["id"])["doc_type"],
                    contract_id=existing["contract_id"], contract_title=contract["title"],
                    end_date=contract["current_expiry_estimate"],
                    expiry_confidence=conf["expiry_confidence"] if conf else None,
                    message="Added earlier; read again with the latest rules.",
                ))
                continue

            label, _ = classify_document(name, text)
            doc_type = DOC_TYPE_MAP.get(label, "other")
            own_title = content_title(text)
            title = own_title or filename_title(name)

            link_id, link_key, message = target, None, ""
            if link_id is None:
                link_id, link_key = find_contract_for_keys(cur, tender_keys(text))
                if link_id is not None:
                    message = f"Linked to an already tracked contract by tender reference {link_key}."
            if link_id is None and doc_type == "other" and not _has_contract_terms(text):
                # e.g. a bid-opening committee list: nothing to track on its own.
                results.append(TrackedFile(
                    name, "SKIPPED", doc_type=doc_type,
                    message="No contract information found (no award, dates or contract period); not added. "
                            "To keep it with a contract, open that contract and add it there.",
                ))
                continue
            result = pipeline.ingest_document(
                cur, raw_text=text, filename=name, doc_type=doc_type,
                contract_id=link_id, contract_title=None if link_id else title, doc_title=name,
            )
            if same_contract and target is None:
                target = result.contract_id
            if link_id is not None:
                _upgrade_fallback_title(cur, result.contract_id, own_title)
            if result.similar_contracts_warning:
                names = "; ".join(f"“{m['title']}”" for m in result.similar_contracts_warning[:2])
                message = (f"Looks like the same work as {names}. If so, open that contract and add this "
                           "file there so the dates are checked together.")
            contract = db.get_contract(cur, result.contract_id)
            results.append(TrackedFile(
                filename=name,
                status="ALREADY_TRACKED" if result.deduplicated else "ADDED",
                doc_type=doc_type,
                contract_id=result.contract_id,
                contract_title=contract["title"],
                end_date=contract["current_expiry_estimate"],
                expiry_confidence=result.expiry_confidence,
                message=" ".join(m for m in (ingested.warning, message) if m),
            ))
        except Exception as exc:  # one bad file must not stop a batch
            results.append(TrackedFile(name, "FAILED", message=str(exc)))
    return results


_CONTRACT_TERM_FACTS = {
    "award_date", "loa_date", "work_order_date", "agreement_signed_date", "site_handover_date",
    "commencement_date_explicit", "base_duration_months", "base_duration_explicit_end_date",
    "extension_exercised_new_end_date", "absolute_expiry_ceiling", "termination_date",
}


def _has_contract_terms(text: str) -> bool:
    from core.extraction import extract_all_facts
    return any(f.fact_type in _CONTRACT_TERM_FACTS for f in extract_all_facts(text))


def _doc_type_for_row(doc) -> str:
    label, _ = classify_document(doc["doc_title"] or "", doc["raw_text"] or "")
    return DOC_TYPE_MAP.get(label, "other")


def reread_all(cur) -> int:
    """Re-read every tracked contract with the current rules; returns how many."""
    ids = [r["id"] for r in cur.execute("SELECT id FROM contracts ORDER BY id").fetchall()]
    for contract_id in ids:
        pipeline.reread_contract(cur, contract_id, doc_type_for=_doc_type_for_row)
        for doc in db.list_documents_for_contract(cur, contract_id):
            _upgrade_fallback_title(cur, contract_id, content_title(doc["raw_text"] or ""))
    return len(ids)


def contract_stage(doc_types) -> str:
    """AWARDED once any award-stage document is on file; TENDER when only
    tender-stage documents are (no contract exists yet, so no end date)."""
    types = set((doc_types or "").split(",")) if isinstance(doc_types, str) else set()
    if types & AWARDED_DOC_TYPES:
        return "AWARDED"
    if types & TENDER_DOC_TYPES:
        return "TENDER"
    return "UNCLEAR"


def portfolio(cur, today: Optional[date] = None):
    """Every tracked contract with its end date and evidence strength,
    nearest end date first and unknown end dates last."""
    import pandas as pd

    today = today or date.today()
    rows = cur.execute("""
        SELECT c.id, c.title, c.status, c.current_expiry_estimate, c.current_expiry_ceiling,
               c.last_human_verified_at, o.name AS buyer, v.name AS vendor,
               (SELECT COUNT(*) FROM documents d WHERE d.contract_id = c.id) AS documents,
               (SELECT group_concat(DISTINCT d.doc_type) FROM documents d WHERE d.contract_id = c.id) AS doc_types,
               (SELECT ca.expiry_confidence FROM confidence_assessment ca WHERE ca.contract_id = c.id
                 ORDER BY ca.assessed_at DESC, ca.id DESC LIMIT 1) AS expiry_confidence,
               (SELECT ca.limiting_factor FROM confidence_assessment ca WHERE ca.contract_id = c.id
                 ORDER BY ca.assessed_at DESC, ca.id DESC LIMIT 1) AS limiting_factor,
               (SELECT COUNT(*) FROM fact_conflicts fc WHERE fc.contract_id = c.id AND fc.resolved = 0) AS open_conflicts
        FROM contracts c
        LEFT JOIN organisations o ON o.id = c.org_id
        LEFT JOIN vendors v ON v.id = c.vendor_id
    """).fetchall()
    df = pd.DataFrame([dict(r) for r in rows], columns=[
        "id", "title", "status", "current_expiry_estimate", "current_expiry_ceiling", "last_human_verified_at",
        "buyer", "vendor", "documents", "doc_types", "expiry_confidence", "limiting_factor", "open_conflicts"])
    df["stage"] = df["doc_types"].map(contract_stage)
    end = pd.to_datetime(df["current_expiry_estimate"], errors="coerce")
    df["days_left"] = (end - pd.Timestamp(today)).dt.days
    df["_unknown"] = end.isna()
    df = df.sort_values(["_unknown", "days_left"]).drop(columns=["_unknown"]).reset_index(drop=True)
    return df
