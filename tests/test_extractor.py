"""Fixture: a real SMIDA disclosure with an empty trade section.

Report 15356 (PFTS, 31.07.2019) discloses "no contracts were executed" for the
day: the metadata block is present, the trade table is absent.  The extractor
must treat this as a valid zero-row report, not as a parse failure.
"""

from __future__ import annotations

import pytest

from smida_tape.extractor import ExtractionError, _extract_one


class FakeClient:
    def __init__(self, html: str):
        self._html = html

    async def report_form(self, report_id: str) -> str:
        return self._html

    async def report_xml_url(self, report_id: str) -> str | None:
        return None


class Item:
    """Minimal stand-in for :class:`smida_tape.db.QueueItem`."""

    report_id = "15356"
    exchange_code = "PFTS"
    xml_url = ""


EMPTY_FORM = """<!DOCTYPE html><html><body>
<div class="singlecol">
  <div class="tbl"><table class="xsTableReportLine">
    <tr><th>Код за ЄДРПОУ фондової біржі:</th><td>21672206</td></tr>
    <tr><th>Найменування фондової біржі:</th><td>АТ &quot;ФОНДОВА БІРЖА ПФТС&quot;</td></tr>
    <tr><th>Дата, на яку складено інформацію:</th><td>31.07.2019</td></tr>
  </table></div>
  <div class="tbl actinfo">
  </div>
</div>
</body></html>
"""

BROKEN_FORM = """<!DOCTYPE html><html><body>
<div class="singlecol"><div class="tbl actinfo"></div></div>
</body></html>
"""


@pytest.mark.asyncio
async def test_empty_trade_section_is_a_valid_zero_row_report() -> None:
    rows, as_of, duplicates, transport = await _extract_one(FakeClient(EMPTY_FORM), Item(), source="html")

    assert rows == []
    assert as_of == "2019-07-31"
    assert duplicates == 0
    assert transport == "html"


@pytest.mark.asyncio
async def test_page_without_metadata_is_a_real_error() -> None:
    with pytest.raises(ExtractionError):
        await _extract_one(FakeClient(BROKEN_FORM), Item(), source="html")
