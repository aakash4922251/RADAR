from __future__ import annotations

import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener


RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


@dataclass
class HttpResponse:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()


class PublicHttpClient:
    """Small, polite HTTP client with bounded retries and no anti-bot bypass."""

    def __init__(self, *, timeout=30, max_retries=3, rate_limit_delay=1.0, user_agent="ContractExpiryRadar/1.0"):
        self.timeout = timeout
        self.max_retries = max_retries
        self.rate_limit_delay = max(0.0, rate_limit_delay)
        self.user_agent = user_agent
        self._last_request_at = 0.0
        # CPPP's homepage detail links carry a session token. Retain the
        # homepage cookie so those public links do not become stale immediately.
        self._cookie_jar = CookieJar()
        self._opener = build_opener(HTTPCookieProcessor(self._cookie_jar))

    def get(self, url: str) -> HttpResponse:
        last_error = None
        for attempt in range(self.max_retries + 1):
            self._wait_for_rate_limit()
            request = Request(url, headers={"User-Agent": self.user_agent, "Accept": "text/html,application/pdf,*/*"})
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    return HttpResponse(response.geturl(), response.status, dict(response.headers.items()), response.read())
            except HTTPError as exc:
                headers = dict(exc.headers.items()) if exc.headers else {}
                if exc.code not in RETRYABLE_STATUS_CODES or attempt >= self.max_retries:
                    return HttpResponse(url, exc.code, headers, exc.read() if hasattr(exc, "read") else b"")
                last_error = exc
                self._sleep_before_retry(attempt, headers)
            except (TimeoutError, URLError, OSError) as exc:
                if attempt >= self.max_retries:
                    raise
                last_error = exc
                self._sleep_before_retry(attempt, {})
        raise last_error or RuntimeError("HTTP request failed")

    def _wait_for_rate_limit(self):
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.rate_limit_delay:
            time.sleep(self.rate_limit_delay - elapsed)
        self._last_request_at = time.monotonic()

    def _sleep_before_retry(self, attempt: int, headers: dict[str, str]):
        retry_after = headers.get("Retry-After")
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                try:
                    delay = max(0, (parsedate_to_datetime(retry_after).timestamp() - time.time()))
                except (TypeError, ValueError, OverflowError):
                    delay = 2**attempt
        else:
            delay = 2**attempt
        time.sleep(min(delay, 30.0))
