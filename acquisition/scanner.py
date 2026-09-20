from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from dataclasses import asdict

from acquisition.base import AcquisitionBlocked, SourceConnector
from acquisition.downloader import DocumentDownloadError, download_document
from acquisition.http import PublicHttpClient
from acquisition.models import ScanSummary, TenderRecord
from core import pipeline
from core.radar import match_discovered_record, refresh_prediction
from db import database as db

logger = logging.getLogger(__name__)


def scan_source(cur, connector: SourceConnector, *, client: PublicHttpClient | None = None,
                storage_root: str = "data/acquired") -> ScanSummary:
    client = client or getattr(connector, "client", None) or PublicHttpClient()
    source = db.get_acquisition_source(cur, connector.source_id)
    if source is None:
        source_id = db.upsert_acquisition_source(
            cur, source_key=connector.source_id, name=connector.source_name,
            base_url=getattr(connector, "base_url", connector.source_id),
        )
        source = db.get_acquisition_source(cur, connector.source_id)
    else:
        source_id = source["id"]

    run_id = db.start_acquisition_run(cur, source_id)
    summary = ScanSummary(run_id=run_id, source_id=connector.source_id)
    try:
        # Do not hold SQLite's write lock while waiting on the public source.
        cur.connection.commit()
        result = connector.discover(since=source["last_success_at"] if source else None)
        discovery_block = result.manual_action_required
        if discovery_block and not result.records:
            summary.manual_action_required += 1
            summary.status = "MANUAL_ACTION_REQUIRED"
            summary.error = discovery_block
            db.update_acquisition_source_status(cur, source_id, status=summary.status, successful=False)
            db.finish_acquisition_run(cur, run_id, status=summary.status, manual_action_required=1, error=summary.error)
            logger.warning("acquisition source=%s run_id=%s manual_action=%s", connector.source_id, run_id, summary.error)
            return summary
        if discovery_block:
            summary.manual_action_required += 1

        for discovered in result.records:
            summary.records_seen += 1
            discovered = connector.fetch_details(discovered)
            record_id, is_new = db.upsert_discovered_record(
                cur, source_id=source_id, external_id=discovered.external_id,
                canonical_url=discovered.detail_url, title=discovered.title,
                organisation=discovered.organisation, published_at=discovered.published_at,
                closing_at=discovered.closing_at, opening_at=discovered.opening_at,
                tender_type=discovered.tender_type, location=discovered.location,
                classification=discovered.classification, estimated_value=discovered.estimated_value,
                detail_url=discovered.detail_url,
                source_page_url=discovered.source_page_url,
                raw_metadata=json.dumps(discovered.raw_metadata, sort_keys=True),
            )
            if is_new:
                summary.records_new += 1
            candidates = connector.fetch_documents(discovered)
            if not candidates:
                db.set_discovered_record_status(
                    cur, record_id, status="DISCOVERED",
                    acquisition_status="DISCOVERED_METADATA_ONLY",
                )
                cur.connection.commit()
                continue

            for candidate in candidates:
                acquired_id, _ = db.upsert_acquired_document(
                    cur, record_id=record_id,
                    source_url=discovered.source_page_url or connector.source_id,
                    document_url=candidate.document_url,
                    filename=candidate.filename,
                )
                existing = cur.execute("SELECT * FROM acquired_documents WHERE id = ?", (acquired_id,)).fetchone()
                if existing and existing["status"] in ("DOWNLOADED", "PROCESSED", "DUPLICATE"):
                    summary.documents_skipped += 1
                    continue
                try:
                    outcome = download_document(
                        client, candidate, storage_root=storage_root, source_id=connector.source_id,
                    )
                    duplicate = db.find_acquired_document_by_hash(cur, outcome["sha256"])
                    if duplicate and duplicate["id"] != acquired_id:
                        persisted = {key: value for key, value in outcome.items() if key not in ("body", "path")}
                        persisted["local_path"] = outcome["path"]
                        db.update_acquired_document(cur, acquired_id, **persisted, status="DUPLICATE")
                        summary.documents_skipped += 1
                        continue
                    persisted = {key: value for key, value in outcome.items() if key not in ("body", "path")}
                    persisted["local_path"] = outcome["path"]
                    db.update_acquired_document(cur, acquired_id, **persisted, status="DOWNLOADED")
                    summary.documents_downloaded += 1
                    ingest_result = _ingest_acquired_document(cur, discovered, candidate, outcome)
                    db.update_acquired_document(cur, acquired_id, status="PROCESSED", document_id=ingest_result.document_id)
                    if ingest_result.requirement_link and ingest_result.requirement_link.requirement_id:
                        refresh_prediction(cur, ingest_result.requirement_link.requirement_id)
                    match_discovered_record(cur, record_id)
                    summary.documents_processed += 1
                    db.set_discovered_record_status(cur, record_id, status="PROCESSED", acquisition_status="PROCESSED")
                except AcquisitionBlocked as exc:
                    db.update_acquired_document(cur, acquired_id, status="MANUAL_ACTION_REQUIRED", error_code="MANUAL_ACTION_REQUIRED", error_detail=exc.reason)
                    db.set_discovered_record_status(cur, record_id, acquisition_status="MANUAL_ACTION_REQUIRED", error=exc.reason)
                    summary.manual_action_required += 1
                except DocumentDownloadError as exc:
                    db.update_acquired_document(cur, acquired_id, status="FAILED", error_code=exc.code, error_detail=exc.detail)
                    db.set_discovered_record_status(cur, record_id, acquisition_status="FAILED", error=exc.detail)
                    summary.documents_failed += 1
                except (ValueError, OSError, RuntimeError) as exc:
                    db.update_acquired_document(cur, acquired_id, status="FAILED", error_code="PROCESSING_ERROR", error_detail=str(exc))
                    db.set_discovered_record_status(cur, record_id, acquisition_status="FAILED", error=str(exc))
                    summary.documents_failed += 1
                    logger.exception("acquisition processing failed source=%s record=%s", connector.source_id, discovered.external_id)
                # Make each record independently durable and release the write lock
                # before the next detail/document request.
            cur.connection.commit()

        summary.status = "SUCCESS" if not summary.documents_failed and not summary.manual_action_required else "PARTIAL"
        if discovery_block:
            summary.error = discovery_block
        db.update_acquisition_source_status(cur, source_id, status=summary.status, successful=summary.status == "SUCCESS")
        db.finish_acquisition_run(cur, run_id, status=summary.status, records_seen=summary.records_seen,
                                  records_new=summary.records_new, documents_downloaded=summary.documents_downloaded,
                                  documents_skipped=summary.documents_skipped, documents_failed=summary.documents_failed,
                                  manual_action_required=summary.manual_action_required,
                                  documents_processed=summary.documents_processed)
    except Exception as exc:
        summary.status = "FAILED"
        summary.error = str(exc)
        db.update_acquisition_source_status(cur, source_id, status=summary.status, successful=False)
        db.finish_acquisition_run(cur, run_id, status=summary.status, records_seen=summary.records_seen,
                                  records_new=summary.records_new, error=summary.error)
        cur.connection.commit()
        logger.warning("acquisition source=%s run_id=%s failed: %s", connector.source_id, run_id, summary.error)
    return summary


def _ingest_acquired_document(cur, record: TenderRecord, candidate, outcome):
    filename = outcome["filename"]
    if Path(filename).suffix.lower() not in {".pdf", ".txt", ".html", ".htm"}:
        raise DocumentDownloadError(
            "UNSUPPORTED_DOCUMENT_TYPE",
            f"Downloaded {Path(filename).suffix.lower() or 'unknown'}; automatic ingestion supports PDF/TXT/HTML only",
        )
    doc_type = "corrigendum" if "corrig" in (record.title or "").lower() or "corrig" in candidate.document_url.lower() else "nit"
    return pipeline.ingest_document(
        cur, raw_bytes=outcome["body"], filename=filename, doc_type=doc_type,
        contract_title=record.title or record.external_id,
        source_url=candidate.document_url, source_org=record.organisation,
        doc_title=filename, doc_date=_date_only(record.published_at),
    )


def _date_only(value):
    if not value:
        return None
    import re
    match = re.search(r"(\d{4})[-/]([01]?\d)[-/]([0-3]?\d)", value)
    return "-".join(match.groups()) if match else None
