from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from acquisition.models import DiscoveryResult, DocumentCandidate, TenderRecord


class AcquisitionBlocked(RuntimeError):
    """A source requires human action or denies an automated operation."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class SourceConnector(ABC):
    source_id: str
    source_name: str
    capabilities: frozenset[str] = frozenset()

    @abstractmethod
    def discover(self, since: str | None = None) -> DiscoveryResult:
        raise NotImplementedError

    def fetch_details(self, record: TenderRecord) -> TenderRecord:
        return record

    def fetch_documents(self, record: TenderRecord) -> list[DocumentCandidate]:
        return list(record.document_urls)

    def health_check(self) -> dict[str, Any]:
        return {"source_id": self.source_id, "status": "UNKNOWN"}
