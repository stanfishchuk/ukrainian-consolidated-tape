"""Async SMIDA HTTP client: polite rate limiting, retries and CSRF session.

SMIDA's search form is a Symfony GET form protected by a CSRF token that is
bound to a session cookie.  Requesting ``/db/feed`` without both silently
returns the *unfiltered* latest feed (HTTP 200, wrong data), so the client:

1. opens ``/db/feed`` once to obtain ``PHPSESSID`` + ``cabinet_report_search[_token]``;
2. re-uses that token for all searches of the session;
3. refreshes the token on demand (expiry, or when the crawler detects that a
   filtered query came back unfiltered).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import date
from typing import Self

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .config import (
    BASE_URL,
    FEED_URL,
    REPORT_CARD_URL,
    REPORT_FORM_URL,
    REPORT_KIND,
    Settings,
)
from .parsing import SearchPage, parse_search_page

log = logging.getLogger(__name__)


class SmidaError(RuntimeError):
    """Non-retryable SMIDA error."""


class RetryableError(SmidaError):
    """Transient failure (network, 429, 5xx)."""


@dataclass
class RateLimiter:
    """Global request pacing: guarantees ``1/max_rps`` seconds between calls."""

    max_rps: float = 3.0

    def __post_init__(self) -> None:
        self._interval = 1.0 / max(self.max_rps, 0.1)
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next_at - now
            if delay > 0:
                await asyncio.sleep(delay)
                now = time.monotonic()
            self._next_at = max(now, self._next_at) + self._interval


class SmidaClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._limiter = RateLimiter(settings.max_rps)
        self._token: str | None = None
        self._token_ts: float = 0.0
        self._token_lock = asyncio.Lock()
        self._consecutive_failures = 0
        self._client = httpx.AsyncClient(
            http2=True,
            follow_redirects=True,
            timeout=httpx.Timeout(settings.timeout, connect=15.0),
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
            },
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )
        self._requests = 0

    # -- lifecycle ---------------------------------------------------------
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def request_count(self) -> int:
        return self._requests

    # -- low level ---------------------------------------------------------
    async def _fetch(self, url: str, params: dict | None = None) -> httpx.Response:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.settings.retries),
            wait=wait_exponential_jitter(initial=2.0, max=60.0),
            retry=retry_if_exception_type(RetryableError),
            reraise=True,
        ):
            with attempt:
                await self._maybe_cool_down()
                await self._limiter.acquire()
                self._requests += 1
                try:
                    response = await self._client.get(url, params=params)
                except httpx.TimeoutException as exc:
                    # A read timeout means SMIDA's renderer gave up on a heavy
                    # report; retrying it 5 times only burns 5 minutes of a
                    # worker slot. Defer it to a dedicated retry pass.
                    self._consecutive_failures += 1
                    raise SmidaError(f"{type(exc).__name__} (deferred): {exc}") from exc
                except httpx.HTTPError as exc:  # transport / protocol
                    self._consecutive_failures += 1
                    raise RetryableError(f"{type(exc).__name__}: {exc}") from exc

                status = response.status_code
                if status == 504:
                    # SMIDA's renderer gives up after 60 s on heavy reports.
                    # Retrying such a page in the same pass only wastes a slot
                    # and hammers the registry: defer it to a later pass.
                    self._consecutive_failures += 1
                    raise SmidaError(f"HTTP 504 (deferred) for {response.request.url}")
                if status == 429 or 500 <= status < 600:
                    self._consecutive_failures += 1
                    await self._respect_retry_after(response)
                    raise RetryableError(f"HTTP {status} for {response.request.url}")
                if status >= 400:
                    raise SmidaError(f"HTTP {status} for {response.request.url}")
                self._consecutive_failures = 0
                return response
        raise SmidaError("unreachable")  # pragma: no cover

    async def _maybe_cool_down(self) -> None:
        """Back off globally when SMIDA starts timing out page after page.

        A sustained run of failures is the signature of server-side overload
        (or of soft throttling of this client); hammering it further only
        deepens the problem, so the whole session pauses once.
        """
        if self._consecutive_failures < self.settings.slow_after:
            return
        delay = min(float(self.settings.cooldown), 5.0 * self._consecutive_failures)
        log.warning(
            "SMIDA looks overloaded (%s consecutive failures) — cooling down for %.0fs",
            self._consecutive_failures,
            delay,
        )
        await asyncio.sleep(delay)
        self._consecutive_failures = 0

    async def _respect_retry_after(self, response: httpx.Response) -> None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return
        try:
            delay = min(float(raw), 60.0)
        except ValueError:
            return
        log.warning("SMIDA asked to back off for %.1fs", delay)
        await asyncio.sleep(delay)

    async def _get_token(self, force: bool = False) -> str:
        async with self._token_lock:
            fresh = self._token and (time.monotonic() - self._token_ts) < 15 * 60
            if fresh and not force:
                return self._token  # type: ignore[return-value]

            response = await self._fetch(FEED_URL)
            page = parse_search_page(response.text)
            token = _extract_token(response.text)
            if not token:
                # A page without the form but with results means we lost the
                # session; surface it instead of silently scraping the feed.
                raise SmidaError(
                    "search form token not found (page size "
                    f"{len(response.text)}, rows {len(page.rows)})"
                )
            self._token = token
            self._token_ts = time.monotonic()
            log.debug("SMIDA session token refreshed")
            return token

    async def refresh_session(self) -> None:
        await self._get_token(force=True)

    # -- public API --------------------------------------------------------
    async def search_page(
        self,
        *,
        edrpou: str | None,
        date_from: date,
        date_to: date,
        page: int = 1,
        name: str | None = None,
        kind: str = REPORT_KIND,
    ) -> SearchPage:
        token = await self._get_token()
        params: dict[str, object] = {
            "cabinet_report_search[kind]": kind,
            "cabinet_report_search[fromDt]": date_from.strftime("%d.%m.%Y"),
            "cabinet_report_search[toDt]": date_to.strftime("%d.%m.%Y"),
            "cabinet_report_search[_token]": token,
        }
        if edrpou:
            params["cabinet_report_search[dCode]"] = edrpou
        if name:
            params["cabinet_report_search[dName]"] = name
        if page > 1:
            params["page"] = page
        response = await self._fetch(FEED_URL, params=params)
        return parse_search_page(response.text)

    async def report_form(self, report_id: str) -> str:
        response = await self._fetch(REPORT_FORM_URL.format(report_id=report_id))
        return response.text

    async def report_xml(self, url: str) -> bytes:
        """Download the static ``report.xml`` copy of a disclosure."""
        response = await self._fetch(url)
        return response.content

    async def report_xml_url(self, report_id: str) -> str | None:
        """Discover the XML link from the (cheap) report card page."""
        response = await self._fetch(REPORT_CARD_URL.format(report_id=report_id))
        match = re.search(r'href="([^"]*/report\.xml)"', response.text)
        if not match:
            return None
        href = match.group(1)
        return f"{BASE_URL}{href}" if href.startswith("/") else href


def _extract_token(html: str) -> str | None:
    marker = 'name="cabinet_report_search[_token]" value="'
    start = html.find(marker)
    if start == -1:
        return None
    start += len(marker)
    end = html.find('"', start)
    return html[start:end] if end > start else None
