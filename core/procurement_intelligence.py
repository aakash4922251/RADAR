"""Automated, source-agnostic procurement document ingestion.

This module is deliberately independent from the legacy contract-expiry parser.
It accepts PDFs/documents/ZIPs, hashes raw bytes, classifies documents, extracts
high-value procurement facts, and persists an auditable processing record.
It never bypasses CAPTCHA/authentication/anti-bot controls.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import sqlite3
import zipfile
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from html.parser import HTMLParser

from core.ingestion import ingest_uploaded_file

DOC_TYPES = (
    "NIT", "TENDER", "AOC", "AWARD", "WORK_ORDER", "CONTRACT", "AGREEMENT",
    "CORRIGENDUM", "BOQ", "TECHNICAL_DOCUMENT", "FINANCIAL_DOCUMENT", "OTHER",
)

@dataclass
class RadarFact:
    fact_type: str
    value: str
    evidence: str
    status: str = "OBSERVED"
    confidence: str = "MEDIUM"

@dataclass
class ProcessResult:
    filename: str
    sha256: str
    status: str
    document_type: str
    facts: list[RadarFact]
    error: str | None = None
    source_url: str | None = None

class _Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]
    def handle_starttag(self, tag, attrs):
        if tag.lower()=="a":
            d=dict(attrs); h=d.get("href")
            if h: self.links.append((h, ""))

_DATE = r"(?:\d{1,2}[\-/]\d{1,2}[\-/]\d{2,4}|\d{1,2}[\s\-/]+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[\s\-/]+\d{2,4}|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2},?\s+\d{4})"
_DATE_RE = re.compile(_DATE, re.I)

def _parse_date(s: str):
    from core.dateutils import parse_date_any
    return parse_date_any(s)

def _near(text, match, radius=180):
    return " ".join(text[max(0,match.start()-radius):min(len(text),match.end()+radius)].split())[:700]

def _first_date(patterns, text):
    for p in patterns:
        m=re.search(p+r"[^\n]{0,120}?"+f"({_DATE})", text, re.I)
        if m:
            d=_parse_date(m.group(1));
            if d: return d.isoformat(), m.group(0)
    return None

def classify_document(filename: str, text: str, source_hint: str|None=None) -> tuple[str,float]:
    hay=(filename+"\n"+(source_hint or "")+"\n"+text[:25000]).lower()
    scores={k:0 for k in DOC_TYPES}
    rules={
      "AOC":["award of contract","aoc","successful bidder","contract price"],
      "WORK_ORDER":["work order","work order no","date of work order","you are requested to commence"],
      "CONTRACT":["agreement between","contract agreement","contract period","commencement date","contractor"],
      "TENDER":["tender id","tender reference number","critical dates","bid submission","tender value"],
      "NIT":["notice inviting tender","nit","inviting authority","tender notice"],
      "AWARD":["letter of award","award letter","award of work","successful bidder"],
      "CORRIGENDUM":["corrigendum","amendment to tender","revised bid submission"],
      "BOQ":["bill of quantities","boq","item description","quantity","unit rate"],
      "AGREEMENT":["memorandum of agreement","agreement executed","executed agreement"],
      "TECHNICAL_DOCUMENT":["technical specification","technical requirements","technical bid"],
      "FINANCIAL_DOCUMENT":["financial bid","price schedule","financial offer"],
    }
    for typ,words in rules.items():
        for w in words:
            if w in hay: scores[typ]+=1
    # Explicit work-order evidence wins over generic contract wording.
    if scores["WORK_ORDER"]>=2: return "WORK_ORDER", min(.99,.72+.08*scores["WORK_ORDER"])
    if scores["AOC"]>=2: return "AOC", min(.99,.72+.08*scores["AOC"])
    best=max(scores,key=scores.get); value=scores[best]
    if value==0:
        return "OTHER", .25
    return best, min(.95,.45+.08*value)

def extract_facts(text: str, doc_type: str) -> list[RadarFact]:
    facts=[]
    def add(t,v,e,c="MEDIUM"):
        if v and not any(x.fact_type==t and x.value==v for x in facts): facts.append(RadarFact(t,str(v),e,"OBSERVED",c))
    # Tender/Award identifiers
    for label,typ in [("Tender ID","tender_id"),("Tender Reference Number","tender_reference"),("Tender Ref","tender_reference")]:
        m=re.search(re.escape(label)+r"\s*[:#-]?\s*([^\n|]{3,100})",text,re.I)
        if m: add(typ,m.group(1).strip(),m.group(0),"HIGH")
    # Work order date has priority over digital signature date. OCR often
    # places the printed date on the next line, so inspect a generous window
    # after the WORK ORDER heading rather than requiring same-line formatting.
    wo=re.search(r"\bWORK\s+ORDER\b",text,re.I)
    if wo:
        window=text[wo.start():wo.start()+900]
        for dm in _DATE_RE.finditer(window):
            d=_parse_date(dm.group(0))
            if d:
                add("work_order_date",d.isoformat(),window[max(0,dm.start()-120):dm.end()+80].strip(),"HIGH")
                break
    if not any(x.fact_type=="work_order_date" for x in facts):
        d=_first_date([r"date\s*of\s*work\s*order"],text)
        if d: add("work_order_date",d[0],d[1],"HIGH")
    d=_first_date([r"award(?:ed)?\s*(?:on|date)?",r"aoc(?:\s*date)?",r"letter\s*of\s*award"],text)
    if d: add("award_date",d[0],d[1],"HIGH")
    d=_first_date([r"commencement(?:\s*date)?",r"start(?:ing)?\s*date"],text)
    if d: add("commencement_date",d[0],d[1],"HIGH")
    d=_first_date([r"bid\s*(?:submission|closing|end)\s*(?:date)?",r"closing\s*date"],text)
    if d: add("tender_closing_date",d[0],d[1],"HIGH")
    # Values
    for m in re.finditer(r"(?:Rs\.?|INR|₹)\s*([0-9][0-9,]*(?:\.\d+)?)\s*(lakh|lakhs|crore|crores)?",text,re.I):
        add("value",m.group(0).strip(),_near(text,m,80),"MEDIUM")
        if len([x for x in facts if x.fact_type=="value"])>=3: break
    # Duration/completion. Supports "20 days from date of work order".
    for m in re.finditer(r"(?:completed?|completion|delivered?|delivery|work)[^\.\n]{0,100}?(?:within|in)\s+(\d+)\s+(days?|weeks?|months?|years?)([^\.\n]{0,100})",text,re.I):
        amount=int(m.group(1)); unit=m.group(2).lower(); tail=m.group(3)
        add("completion_period",f"{amount} {unit}",m.group(0).strip(),"HIGH")
        base=None
        wm=next((x for x in facts if x.fact_type=="work_order_date"),None)
        if wm: base=_parse_date(wm.value)
        if base:
            days=amount if unit.startswith("day") else amount*7 if unit.startswith("week") else amount*30 if unit.startswith("month") else amount*365
            deadline=base+timedelta(days=days)
            add("completion_deadline",deadline.isoformat(),f"Calculated from {wm.value} + {amount} {unit}","HIGH")
        break
    # Warranty is intentionally separate from completion/contract duration.
    m=re.search(r"warranty[^\.\n]{0,80}?(\d+)\s*(days?|months?|years?)",text,re.I)
    if m: add("warranty_period",f"{m.group(1)} {m.group(2)}",m.group(0).strip(),"HIGH")
    # Vendor / organisation cues. Prefer the structured party blocks common
    # in Indian government work orders before generic words such as
    # "vendor shall submit".
    wo_block=text[:8000]
    vm=re.search(r"WORK\s+ORDER.*?\n\s*([A-Z][A-Za-z0-9&.\- ]{3,80}?(?:Electrical|Engineering|Systems|Services|Enterprises|Industries|Agency|Pvt\.?\s*Ltd\.?))\s*,",wo_block,re.I|re.S)
    if vm: add("vendor",vm.group(1).strip(),vm.group(0),"HIGH")
    if not any(x.fact_type=="vendor" for x in facts):
        m=re.search(r"(?:M/s\.?|successful bidder)\s*[:\-]?\s*([^\n,|]{3,100})",text,re.I)
        if m: add("vendor",m.group(1).strip(),m.group(0),"MEDIUM")
    om=re.search(r"VISVESVARAYA\s+NATIONAL\s+INSTITUTE\s+OF\s+TECHNOLOGY[^\n]{0,120}",text,re.I)
    if om: add("organisation","Visvesvaraya National Institute of Technology (VNIT), Nagpur",om.group(0),"HIGH")
    else:
        m=re.search(r"(?:organisation|organization|department|institute|authority)\s*[:\-]?\s*([^\n|]{3,140})",text,re.I)
        if m: add("organisation",m.group(1).strip(),m.group(0),"MEDIUM")
    sm=re.search(r"(?:Sub\.|Subject\s*:).*?(?:work order for\s+)?(.{20,350}?)(?:\n|\.)",text,re.I|re.S)
    if sm: add("requirement", " ".join(sm.group(1).split()), sm.group(0), "HIGH")
    # Location
    m=re.search(r"(?:location|place of work|pincode)\s*[:\-]?\s*([^\n|]{3,140})",text,re.I)
    if m: add("location",m.group(1).strip(),m.group(0),"MEDIUM")
    return facts

def sha256_bytes(data: bytes)->str:
    return hashlib.sha256(data).hexdigest()

def init_intelligence_tables(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS radar_documents (
      id INTEGER PRIMARY KEY AUTOINCREMENT, sha256 TEXT NOT NULL UNIQUE, filename TEXT NOT NULL,
      source_url TEXT, document_type TEXT NOT NULL, classification_confidence REAL NOT NULL,
      raw_text TEXT, page_count INTEGER, used_ocr INTEGER NOT NULL DEFAULT 0,
      status TEXT NOT NULL DEFAULT 'PROCESSED', error TEXT, created_at TEXT NOT NULL DEFAULT(datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS radar_facts (
      id INTEGER PRIMARY KEY AUTOINCREMENT, document_id INTEGER NOT NULL REFERENCES radar_documents(id) ON DELETE CASCADE,
      fact_type TEXT NOT NULL, fact_value TEXT NOT NULL, evidence TEXT NOT NULL,
      fact_status TEXT NOT NULL, confidence TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT(datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS radar_batches (
      id INTEGER PRIMARY KEY AUTOINCREMENT, source_name TEXT, discovered INTEGER NOT NULL DEFAULT 0,
      processed INTEGER NOT NULL DEFAULT 0, duplicates INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
      started_at TEXT NOT NULL DEFAULT(datetime('now')), finished_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_radar_facts_doc ON radar_facts(document_id);
    CREATE INDEX IF NOT EXISTS idx_radar_docs_type ON radar_documents(document_type);
    """)
    conn.commit()

def process_bytes(conn, data: bytes, filename: str, source_url: str|None=None) -> ProcessResult:
    h=sha256_bytes(data)
    init_intelligence_tables(conn)
    existing=conn.execute("SELECT id FROM radar_documents WHERE sha256=?",(h,)).fetchone()
    if existing:
        return ProcessResult(filename,h,"DUPLICATE","OTHER",[] ,source_url=source_url)
    try:
        ing=ingest_uploaded_file(data,filename)
        if not ing.raw_text.strip(): raise ValueError("No extractable text")
        typ,conf=classify_document(filename,ing.raw_text,source_url)
        facts=extract_facts(ing.raw_text,typ)
        cur=conn.execute("INSERT INTO radar_documents(sha256,filename,source_url,document_type,classification_confidence,raw_text,page_count,used_ocr,status) VALUES(?,?,?,?,?,?,?,?,?)",
                         (h,filename,source_url,typ,conf,ing.raw_text,ing.page_count,1 if ing.used_ocr else 0,"PROCESSED"))
        did=cur.lastrowid
        for f in facts:
            conn.execute("INSERT INTO radar_facts(document_id,fact_type,fact_value,evidence,fact_status,confidence) VALUES(?,?,?,?,?,?)",
                         (did,f.fact_type,f.value,f.evidence,f.status,f.confidence))
        conn.commit()
        return ProcessResult(filename,h,"PROCESSED",typ,facts,source_url=source_url)
    except Exception as exc:
        conn.execute("INSERT OR IGNORE INTO radar_documents(sha256,filename,source_url,document_type,classification_confidence,status,error) VALUES(?,?,?,?,?,?,?)",
                     (h,filename,source_url,"OTHER",0.0,"FAILED",str(exc)))
        conn.commit()
        return ProcessResult(filename,h,"FAILED","OTHER",[],str(exc),source_url)

def iter_zip(data: bytes, prefix=""):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            if info.is_dir(): continue
            name=info.filename
            if name.startswith("__MACOSX/"): continue
            payload=z.read(info)
            if name.lower().endswith(".zip"):
                yield from iter_zip(payload, prefix+name+"/")
            elif Path(name).suffix.lower() in {".pdf",".txt",".docx",".doc",".xlsx",".xls",".csv"}:
                yield prefix+name,payload

def process_zip(conn,data:bytes,filename="upload.zip"):
    results=[]
    for name,payload in iter_zip(data): results.append(process_bytes(conn,payload,name))
    return results

def process_directory(conn, directory: str, recursive=True):
    root=Path(directory); results=[]
    if not root.exists(): return results
    pattern="**/*" if recursive else "*"
    for p in root.glob(pattern):
        if p.is_file() and p.suffix.lower()==".zip":
            try: results.extend(process_zip(conn,p.read_bytes(),p.name))
            except Exception as exc: results.append(ProcessResult(p.name,sha256_bytes(p.read_bytes()),"FAILED","OTHER",[],str(exc)))
        elif p.is_file() and p.suffix.lower() in {".pdf",".txt",".docx",".doc",".xlsx",".xls",".csv"}:
            results.append(process_bytes(conn,p.read_bytes(),p.name))
    return results

def fetch_and_process_url(conn,url:str,timeout=30):
    req=Request(url,headers={"User-Agent":"GovernmentProcurementRadar/1.0 (document acquisition)"})
    with urlopen(req,timeout=timeout) as r:
        data=r.read()
        ctype=(r.headers.get("Content-Type") or "").lower(); final=r.geturl()
    if "pdf" in ctype or final.lower().split("?")[0].endswith(".pdf"):
        return [process_bytes(conn,data,os.path.basename(final.split("?")[0]) or "document.pdf",final)]
    html=data.decode("utf-8",errors="replace")
    parser=_Links(); parser.feed(html)
    links=[]
    for href,_ in parser.links:
        absolute=urljoin(final,href)
        if absolute.lower().split("?")[0].endswith((".pdf",".doc",".docx",".xls",".xlsx",".zip")):
            links.append(absolute)
    results=[]
    for link in dict.fromkeys(links):
        try:
            results.extend(fetch_and_process_url(conn,link,timeout))
        except Exception as exc:
            results.append(ProcessResult(link,hashlib.sha256(link.encode()).hexdigest(),"FAILED","OTHER",[],str(exc),link))
    return results
