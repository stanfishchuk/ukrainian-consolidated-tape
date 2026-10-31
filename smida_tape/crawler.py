"""Module 1 — discovery of exchange disclosures in the SMIDA feed.

The crawler walks the SMIDA search form once per (exchange, EDRPOU, year) and
stores every matching disclosure in the SQLite queue.  Year-sized windows keep
pagination small (<= ~15 pages) and make the crawl resumable at a fine grain.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date

from .config import (
    EXCHANGES,
    REPORT_FORM_URL,
    WINDOW_END,
    WINDOW_START,
    Exchange,
    Settings,
)
from .db import QueueDB
from .http_client import SmidaClient
from .parsing import SearchRow

log = logging.getLogger(__name__)


class CrawlError(RuntimeError):
    pass


@dataclass
class CrawlResult:
    exchange: str
    pages: int = 0
    discovered: int = 0


def year_windows(start: date, end: date) -> list[tuple[date, date]]:
    """Split ``[start, end]`` into calendar-year windows (clipped)."""
    windows: list[tuple[date, date]] = []
    cursor = date(start.year, 1, 1)
    while cursor <= end:
        chunk_start = max(cursor, start)
        chunk_end = min(date(cursor.year, 12, 31), end)
        windows.append((chunk_start, chunk_end))
        cursor = date(cursor.year + 1, 1, 1)
    return windows


async def _validated_page(
    client: SmidaClient,
    *,
    edrpou: str | None,
    name: str | None,
    date_from: date,
    date_to: date,
    page: int,
    allowed_edrpou: set[str],
    attempts: int = 3,
):
    """Fetch a result page and make sure the EDRPOU filter really applied.

    Without a valid session token SMIDA answers 200 with the *global* feed, so
    every row must carry the expected EDRPOU code; a page that contains other
    codes is treated as a lost session and retried after refreshing the token.
    """
    last_rows = 0
    for attempt in range(1, attempts + 1):
        result = await client.search_page(
            edrpou=edrpou, name=name, date_from=date_from, date_to=date_to, page=page
        )
        last_rows = len(result.rows)
        if not result.rows:
            return result
        if all(row.edrpou in allowed_edrpou for row in result.rows):
            return result
        log.warning(
            "page %s for %s came back unfiltered (%s rows, codes %s) — refreshing session",
            page,
            edrpou or name,
            last_rows,
            sorted({row.edrpou for row in result.rows})[:5],
        )
        if attempt < attempts:
            await client.refresh_session()
    raise CrawlError(
        f"cannot obtain filtered results for {edrpou or name} "
        f"{date_from}..{date_to} page {page}"
    )


async def _crawl_chunk(
    client: SmidaClient,
    db: QueueDB,
    exchange: Exchange,
    *,
    edrpou: str | None,
    name: str | None,
    date_from: date,
    date_to: date,
    workers: int,
    max_pages: int | None,
) -> tuple[int, int]:
    """Crawl one date window; returns ``(pages, discovered_rows)``."""
    allowed = {edrpou} if edrpou else set(exchange.edrpou)
    first = await _validated_page(
        client,
        edrpou=edrpou,
        name=name,
        date_from=date_from,
        date_to=date_to,
        page=1,
        allowed_edrpou=allowed,
    )
    total_pages = max(1, first.total_pages)
    if max_pages:
        total_pages = min(total_pages, max_pages)

    pages: dict[int, list[SearchRow]] = {1: first.rows}
    semaphore = asyncio.Semaphore(max(1, workers))

    async def fetch(page: int) -> None:
        async with semaphore:
            result = await _validated_page(
                client,
                edrpou=edrpou,
                name=name,
                date_from=date_from,
                date_to=date_to,
                page=page,
                allowed_edrpou=allowed,
            )
            pages[page] = result.rows

    if total_pages > 1:
        tasks = [asyncio.create_task(fetch(page)) for page in range(2, total_pages + 1)]
        try:
            for index, coro in enumerate(asyncio.as_completed(tasks), start=2):
                await coro
                if index % 20 == 0:
                    log.info(
                        "  %s %s..%s: %s/%s pages",
                        exchange.code, date_from, date_to, index, total_pages,
                    )
        finally:
            # Never leave background page fetches behind if one of them fails.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    records: list[dict] = []
    seen: set[str] = set()
    for page in sorted(pages):
        for row in pages[page]:
            if row.edrpou not in allowed or row.report_id in seen:
                continue
            seen.add(row.report_id)
            published = row.published_at
            records.append(
                {
                    "report_id": row.report_id,
                    "exchange_code": exchange.code,
                    "edrpou": row.edrpou,
                    "report_date": (published.date() if published else date_from).isoformat(),
                    "report_url": REPORT_FORM_URL.format(report_id=row.report_id),
                    "xml_url": row.xml_url or None,
                    "published_at": published.isoformat() if published else None,
                }
            )
    db.upsert_reports(records)
    return total_pages, len(records)


async def crawl_exchange(
    settings: Settings,
    db: QueueDB,
    exchange: Exchange,
    *,
    start: date | None = None,
    end: date | None = None,
    workers: int = 8,
    max_pages: int | None = None,
) -> CrawlResult:
    start = max(start or exchange.period_start, exchange.period_start)
    end = min(end or exchange.period_end, exchange.period_end)
    result = CrawlResult(exchange=exchange.code)
    if start > end:
        log.warning("%s: empty period %s..%s, skipped", exchange.code, start, end)
        return result

    unique_ids: set[str] = set()
    async with SmidaClient(settings) as client:
        for edrpou in exchange.edrpou:
            for window_start, window_end in year_windows(start, end):
                pages, found = await _crawl_chunk(
                    client,
                    db,
                    exchange,
                    edrpou=edrpou,
                    name=None,
                    date_from=window_start,
                    date_to=window_end,
                    workers=workers,
                    max_pages=max_pages,
                )
                result.pages += pages
                unique_ids.update(db.report_ids(exchange=exchange.code))
                log.info(
                    "%s [%s] %s..%s: %s pages, %s reports",
                    exchange.code,
                    edrpou,
                    window_start,
                    window_end,
                    pages,
                    found,
                )

        # Fallback for entities that disclose under a code we do not know:
        # only needed when no EDRPOU alias produced anything at all.
        if not unique_ids:
            for display_name in exchange.search_names:
                log.info("%s: no reports by EDRPOU, retrying by name %r", exchange.code, display_name)
                for window_start, window_end in year_windows(start, end):
                    pages, _ = await _crawl_chunk(
                        client,
                        db,
                        exchange,
                        edrpou=None,
                        name=display_name,
                        date_from=window_start,
                        date_to=window_end,
                        workers=workers,
                        max_pages=max_pages,
                    )
                    result.pages += pages
                    unique_ids.update(db.report_ids(exchange=exchange.code))
                if unique_ids:
                    break

        result.discovered = len(unique_ids)
        requests_made = client.request_count

    db.log_crawl(exchange.code, start.isoformat(), end.isoformat(), result.pages, result.discovered)
    log.info(
        "%s: %s reports discovered over %s pages (%s HTTP requests)",
        exchange.code,
        result.discovered,
        result.pages,
        requests_made,
    )
    return result


async def crawl(
    settings: Settings,
    db: QueueDB,
    exchanges: list[str] | None = None,
    *,
    start: date | None = None,
    end: date | None = None,
    workers: int = 8,
    max_pages: int | None = None,
) -> dict[str, CrawlResult]:
    settings.ensure_dirs()
    db.init_schema()
    codes = [code.upper() for code in (exchanges or list(EXCHANGES))]
    unknown = [code for code in codes if code not in EXCHANGES]
    if unknown:
        raise CrawlError(f"unknown exchange codes: {unknown}; expected {list(EXCHANGES)}")

    results: dict[str, CrawlResult] = {}
    for code in codes:
        results[code] = await crawl_exchange(
            settings,
            db,
            EXCHANGES[code],
            start=start or WINDOW_START,
            end=end or WINDOW_END,
            workers=workers,
            max_pages=max_pages,
        )
    return results
