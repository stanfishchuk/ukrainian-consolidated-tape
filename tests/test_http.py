from __future__ import annotations

import time
from datetime import date

import pytest

from smida_tape.crawler import CrawlError, _validated_page, year_windows
from smida_tape.http_client import RateLimiter, _extract_token
from smida_tape.parsing import SearchPage, SearchRow


class FakeClient:
    def __init__(self, pages: list[SearchPage]):
        self._pages = list(pages)
        self.refresh_calls = 0

    async def search_page(self, **kwargs) -> SearchPage:
        return self._pages.pop(0)

    async def refresh_session(self) -> None:
        self.refresh_calls += 1


def row(edrpou: str = "21672206") -> SearchRow:
    return SearchRow(
        report_id="1",
        published_at=None,
        edrpou=edrpou,
        entity_name="АТ ФОНДОВА БІРЖА ПФТС",
        info_type="Щоденні дані",
    )


def test_year_windows_cover_requested_span() -> None:
    windows = year_windows(date(2015, 1, 1), date(2025, 12, 31))
    assert len(windows) == 11
    assert windows[0] == (date(2015, 1, 1), date(2015, 12, 31))
    assert windows[-1] == (date(2025, 1, 1), date(2025, 12, 31))

    clipped = year_windows(date(2020, 6, 1), date(2021, 3, 15))
    assert clipped == [(date(2020, 6, 1), date(2020, 12, 31)), (date(2021, 1, 1), date(2021, 3, 15))]


def test_extract_token() -> None:
    html = '<input id="x" name="cabinet_report_search[_token]" value="abc123" /></form>'
    assert _extract_token(html) == "abc123"
    assert _extract_token("<html></html>") is None


@pytest.mark.asyncio
async def test_rate_limiter_spaces_requests() -> None:
    limiter = RateLimiter(max_rps=50)
    started = time.monotonic()
    for _ in range(4):
        await limiter.acquire()
    elapsed = time.monotonic() - started
    assert elapsed >= 3 / 50 * 0.9


@pytest.mark.asyncio
async def test_unfiltered_page_is_retried_after_refresh() -> None:
    client = FakeClient([SearchPage(rows=[row("99999999")]), SearchPage(rows=[row()])])

    page = await _validated_page(
        client,  # type: ignore[arg-type]
        edrpou="21672206",
        name=None,
        date_from=date(2025, 1, 1),
        date_to=date(2025, 12, 31),
        page=1,
        allowed_edrpou={"21672206"},
    )

    assert page.rows[0].edrpou == "21672206"
    assert client.refresh_calls == 1


@pytest.mark.asyncio
async def test_persistently_unfiltered_page_raises() -> None:
    client = FakeClient([SearchPage(rows=[row("99999999")]) for _ in range(3)])

    with pytest.raises(CrawlError):
        await _validated_page(
            client,  # type: ignore[arg-type]
            edrpou="21672206",
            name=None,
            date_from=date(2025, 1, 1),
            date_to=date(2025, 12, 31),
            page=1,
            allowed_edrpou={"21672206"},
        )
    assert client.refresh_calls == 2
