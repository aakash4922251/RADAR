from __future__ import annotations

import json
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin

from acquisition.base import AcquisitionBlocked, SourceConnector
from acquisition.models import DiscoveryResult, DocumentCandidate, TenderRecord
from acquisition.http import PublicHttpClient


CPPP_BASE_URL = "https://eprocure.gov.in/eprocure/app"
CPPP_HOME_URL = CPPP_BASE_URL
CPPP_LATEST_TENDERS_URL = CPPP_HOME_URL
CPPP_LATEST_CORRIGENDA_URL = CPPP_BASE_URL + "?page=FrontEndLatestActiveCorrigendums&service=page"


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.headers: list[str] = []
        self.rows: list[tuple[list[str], list[tuple[str, str]]]] = []
        self._row_cells: list[str] | None = None
        self._row_links: list[tuple[str, str]] = []
        self._cell_text: list[str] | None = None
        self._cell_link: str | None = None
        self._cell_link_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "tr":
            self._row_cells, self._row_links = [], []
        elif self._row_cells is not None and tag in ("td", "th"):
            self._cell_text = []
            self._cell_link = None
            self._cell_link_text = []
        elif self._cell_text is not None and tag == "a" and attrs_dict.get("href"):
            self._cell_link = attrs_dict["href"]

    def handle_data(self, data):
        if self._cell_text is not None and data.strip():
            value = " ".join(data.split())
            self._cell_text.append(value)
            if self._cell_link is not None:
                self._cell_link_text.append(value)

    def handle_endtag(self, tag):
        if self._cell_text is not None and tag in ("td", "th"):
            value = " ".join(self._cell_text).strip()
            if self._row_cells is not None:
                self._row_cells.append(value)
                if self._cell_link:
                    self._row_links.append((self._cell_link, " ".join(self._cell_link_text)))
            self._cell_text = None
            self._cell_link = None
            self._cell_link_text = []
        elif tag == "tr" and self._row_cells is not None:
            if self._row_cells and not self.headers:
                self.headers = [self._normalise_header(value) for value in self._row_cells]
            elif self._row_cells:
                self.rows.append((self._row_cells, self._row_links))
            self._row_cells = None

    @staticmethod
    def _normalise_header(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


class _CPPPHomepageParser(HTMLParser):
    """Parse the homepage's table#activeTenders rows.

    CPPP does not render headers inside this nested table. Each row is:
    title/detail link, reference number, closing date, bid opening date.
    """

    def __init__(self):
        super().__init__()
        self.rows: list[tuple[list[str], list[tuple[str, str]]]] = []
        self._active_depth = 0
        self._row_cells: list[str] | None = None
        self._row_links: list[tuple[str, str]] = []
        self._cell_text: list[str] | None = None
        self._cell_link: str | None = None
        self._cell_link_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "table" and attrs_dict.get("id") == "activeTenders":
            self._active_depth = 1
            return
        if not self._active_depth:
            return
        if tag == "table":
            self._active_depth += 1
        elif tag == "tr" and self._active_depth == 1:
            self._row_cells, self._row_links = [], []
        elif self._row_cells is not None and tag == "td":
            self._cell_text, self._cell_link, self._cell_link_text = [], None, []
        elif self._cell_text is not None and tag == "a" and attrs_dict.get("href"):
            self._cell_link = attrs_dict["href"]

    def handle_data(self, data):
        if self._cell_text is not None and data.strip():
            value = " ".join(data.split())
            self._cell_text.append(value)
            if self._cell_link is not None:
                self._cell_link_text.append(value)

    def handle_endtag(self, tag):
        if not self._active_depth:
            return
        if self._cell_text is not None and tag == "td":
            if self._row_cells is not None:
                self._row_cells.append(" ".join(self._cell_text).strip())
                if self._cell_link:
                    self._row_links.append((self._cell_link, " ".join(self._cell_link_text)))
            self._cell_text, self._cell_link, self._cell_link_text = None, None, []
        elif tag == "tr" and self._active_depth == 1 and self._row_cells is not None:
            if len(self._row_cells) >= 4:
                self.rows.append((self._row_cells, self._row_links))
            self._row_cells = None
        elif tag == "table":
            self._active_depth -= 1


class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self._href = href
                self._text = []

    def handle_data(self, data):
        if self._href is not None and data.strip():
            self._text.append(" ".join(data.split()))

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._text)))
            self._href = None
            self._text = []


class CPPPConnector(SourceConnector):
    source_id = "cppp"
    source_name = "Central Public Procurement Portal"
    capabilities = frozenset({"discovery", "metadata", "document_download"})

    def __init__(self, client: PublicHttpClient | None = None, *, base_url=CPPP_BASE_URL, search_url: str | None = None):
        self.client = client or PublicHttpClient()
        self.base_url = base_url.rstrip("/")
        self.latest_tenders_url = (search_url or self.base_url).strip()
        self.latest_corrigenda_url = self.base_url + "?page=FrontEndLatestActiveCorrigendums&service=page"

    def discover(self, since: str | None = None) -> DiscoveryResult:
        response = self.client.get(self.latest_tenders_url)
        if response.status >= 400:
            raise RuntimeError(f"CPPP listing returned HTTP {response.status}")
        html = response.body.decode("utf-8", errors="replace")
        records = self.parse_listing(html, response.url)
        if not records and self._has_captcha_form(html):
            return DiscoveryResult(manual_action_required="CAPTCHA_REQUIRED for CPPP listing search")
        return DiscoveryResult(records=records)

    def fetch_details(self, record: TenderRecord) -> TenderRecord:
        if not record.detail_url:
            return record
        response = self.client.get(record.detail_url)
        if response.status >= 400:
            return record
        html = response.body.decode("utf-8", errors="replace")
        record.organisation = self._detail_value(html, "Organisation Chain") or record.organisation
        record.published_at = self._detail_value(html, "Published Date") or record.published_at
        record.estimated_value = self._detail_value(html, "Tender Value in") or record.estimated_value
        record.location = self._detail_value(html, "Location") or record.location
        record.document_urls = self._merge_documents(record.document_urls, self.parse_document_links(html, response.url))
        return record

    def fetch_documents(self, record: TenderRecord) -> list[DocumentCandidate]:
        return self._merge_documents(record.document_urls, [])

    def health_check(self) -> dict:
        try:
            response = self.client.get(self.latest_tenders_url)
            return {"source_id": self.source_id, "status": "HEALTHY" if response.status < 400 else "DEGRADED", "http_status": response.status}
        except Exception as exc:
            return {"source_id": self.source_id, "status": "DOWN", "error": str(exc)}

    @classmethod
    def parse_listing(cls, html: str, source_page_url: str) -> list[TenderRecord]:
        homepage_parser = _CPPPHomepageParser()
        homepage_parser.feed(html)
        if homepage_parser.rows:
            return cls._parse_homepage_rows(homepage_parser.rows, source_page_url)

        parser = _TableParser()
        parser.feed(html)
        header_index = {header: index for index, header in enumerate(parser.headers)}
        records = []
        for cells, links in parser.rows:
            if len(cells) < 2:
                continue
            joined = " | ".join(cells)
            external_id = cls._value(cells, header_index, ("tender_id", "reference_no", "tender_reference", "reference_number"))
            external_id = external_id or cls._find_reference(joined)
            title = cls._value(cells, header_index, ("tender_title", "title", "corrigendum_title"))
            if not external_id or not title:
                continue
            detail_url = cls._find_detail_link(links)
            document_urls = [
                DocumentCandidate(urljoin(source_page_url, href), text or None)
                for href, text in links
                if cls._looks_like_document(href, text)
            ]
            records.append(TenderRecord(
                source_id=cls.source_id,
                external_id=external_id,
                title=title,
                organisation=cls._value(cells, header_index, ("organisation", "department", "organisation_name")),
                published_at=cls._value(cells, header_index, ("published_date", "publication_date", "published")),
                closing_at=cls._value(cells, header_index, ("closing_date", "bid_submission_closing_date", "closing")),
                opening_at=cls._value(cells, header_index, ("bid_opening_date", "opening_date", "opening")),
                tender_type=cls._value(cells, header_index, ("tender_type", "type")),
                location=cls._value(cells, header_index, ("location",)),
                classification=cls._value(cells, header_index, ("classification", "category")),
                estimated_value=cls._value(cells, header_index, ("estimated_value", "tender_value", "value", "work_value")),
                detail_url=urljoin(source_page_url, detail_url) if detail_url else None,
                source_page_url=source_page_url,
                document_urls=document_urls,
                raw_metadata={"headers": parser.headers, "cells": cells, "links": links},
            ))
        return records

    @classmethod
    def _parse_homepage_rows(cls, rows, source_page_url: str) -> list[TenderRecord]:
        records = []
        for cells, links in rows:
            title = cells[0]
            reference = cells[1].strip()
            detail_url = None
            for href, text in links:
                if text.strip():
                    detail_url = urljoin(source_page_url, href)
                    break
            if not title or not reference:
                continue
            records.append(TenderRecord(
                source_id=cls.source_id,
                external_id=reference,
                title=re.sub(r"^\d+\.\s*", "", title).strip(),
                closing_at=cells[2].strip() or None,
                opening_at=cells[3].strip() or None,
                detail_url=detail_url,
                source_page_url=source_page_url,
                raw_metadata={
                    "listing_route": "homepage#activeTenders",
                    "cells": cells,
                    "links": links,
                    "organisation_status": "NOT_EXPOSED_ON_PUBLIC_LISTING",
                    "published_status": "NOT_EXPOSED_ON_PUBLIC_LISTING",
                },
            ))
        return records

    @staticmethod
    def parse_document_links(html: str, source_url: str) -> list[DocumentCandidate]:
        parser = _LinkParser()
        parser.feed(html)
        found = []
        for href, text in parser.links:
            value = f"{href} {text}".lower()
            if any(token in value for token in ("docdownoad", "download as zip", ".pdf", ".doc", ".docx", ".zip")):
                found.append(DocumentCandidate(urljoin(source_url, href), text or None))
        return CPPPConnector._merge_documents(found, [])

    @staticmethod
    def _detail_value(html: str, label: str) -> str | None:
        pattern = (
            r"<td[^>]*class=[\"']td_caption[\"'][^>]*>.*?"
            + r"<b>\s*"
            + re.escape(label)
            + r"(?:\s+[^<]*)?\s*</b>.*?</td>\s*<td[^>]*class=[\"']td_field[\"'][^>]*>(.*?)</td>"
        )
        match = re.search(pattern, html, re.I | re.S)
        if not match:
            return None
        value = unescape(re.sub(r"<[^>]+>", " ", match.group(1)))
        value = re.sub(r"\s+", " ", value).strip()
        return value or None

    @staticmethod
    def _value(cells, header_index, names):
        for name in names:
            if name in header_index and header_index[name] < len(cells):
                return cells[header_index[name]].strip() or None
        return None

    @staticmethod
    def _find_reference(text: str):
        match = re.search(r"(?:tender\s*(?:id|reference|ref(?:erence)?\.?\s*no)|reference\s*no)\s*[:#-]?\s*([A-Z0-9][A-Z0-9./_-]{4,})", text, re.I)
        return match.group(1) if match else None

    @staticmethod
    def _find_detail_link(links):
        for href, text in links:
            if not CPPPConnector._looks_like_document(href, text):
                return href
        return None

    @staticmethod
    def _looks_like_document(href: str, text: str) -> bool:
        value = f"{href} {text}".lower()
        return any(token in value for token in (".pdf", ".doc", ".docx", ".zip", "download", "document", "nit"))

    @staticmethod
    def _merge_documents(first, second):
        seen = set()
        result = []
        for candidate in list(first) + list(second):
            if candidate.document_url not in seen:
                seen.add(candidate.document_url)
                result.append(candidate)
        return result

    @staticmethod
    def _has_captcha_form(html: str) -> bool:
        lowered = unescape(html).lower()
        return "captcha" in lowered and ("captcha" in lowered and "form" in lowered)
