from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class DocumentCandidate:
    document_url: str
    filename: Optional[str] = None
    content_type: Optional[str] = None


@dataclass
class TenderRecord:
    source_id: str
    external_id: str
    title: Optional[str] = None
    organisation: Optional[str] = None
    published_at: Optional[str] = None
    closing_at: Optional[str] = None
    opening_at: Optional[str] = None
    tender_type: Optional[str] = None
    location: Optional[str] = None
    classification: Optional[str] = None
    estimated_value: Optional[str] = None
    detail_url: Optional[str] = None
    source_page_url: Optional[str] = None
    document_urls: list[DocumentCandidate] = field(default_factory=list)
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DiscoveryResult:
    records: list[TenderRecord] = field(default_factory=list)
    manual_action_required: Optional[str] = None


@dataclass
class ScanSummary:
    run_id: int
    source_id: str
    status: str = "RUNNING"
    records_seen: int = 0
    records_new: int = 0
    documents_downloaded: int = 0
    documents_skipped: int = 0
    documents_failed: int = 0
    manual_action_required: int = 0
    documents_processed: int = 0
    error: Optional[str] = None
