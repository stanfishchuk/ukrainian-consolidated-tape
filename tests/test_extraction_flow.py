"""Extraction loop: checkpointing, failure isolation, queue bookkeeping."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path
from typing import ClassVar, Self

import pandas as pd
import pytest

from smida_tape import extractor as extractor_module
from smida_tape.config import Settings
from smida_tape.db import QueueDB
from smida_tape.extractor import extract

FORM = """<!DOCTYPE html><html><body><div class="singlecol">
<div class="tbl"><table class="xsTableReportLine">
  <tr><th>Код за ЄДРПОУ фондової біржі:</th><td>21672206</td></tr>
  <tr><th>Дата, на яку складено інформацію:</th><td>02.01.2025</td></tr>
</table></div>
<div class="tbl actinfo">
<table class="xsTableReport"><tr>
  <th>Дата вчинення правочину</th><th>Частина операції за договором РЕПО</th>
  <th>Найменування емітента цінних паперів за правочином</th>
  <th>Код за ЄДРПОУ емітента цінних паперів за правочином</th>
  <th>Вид фінансового інструменту</th><th>Міжнародний ідентифікаційний номер</th>
  <th>Кількість цінних паперів</th><th>Ціна цінних паперів</th>
</tr>
<tr><td>02.01.2025</td><td> - </td><td>Мінфін</td><td>00013480</td>
    <td>ОБЛІГАЦІЯ</td><td>UA4000000001</td><td>10</td><td>100,5</td></tr>
</table></div>
</div></body></html>
"""


class FakeSmidaClient:
    """Minimal stand-in for :class:`SmidaClient` used as an async context manager."""

    fail_ids: ClassVar[set[str]] = set()

    def __init__(self, settings: Settings):
        self.settings = settings

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def report_form(self, report_id: str) -> str:
        if report_id in self.fail_ids:
            raise extractor_module.SmidaError("HTTP 504 (deferred) for test")
        return FORM

    async def report_xml_url(self, report_id: str) -> str | None:
        return None


@pytest.fixture()
def queue(tmp_path: Path) -> QueueDB:
    db = QueueDB(tmp_path / "metadata.db")
    db.init_schema()
    db.upsert_reports(
        [
            {
                "report_id": str(index),
                "exchange_code": "PFTS",
                "edrpou": "21672206",
                "report_date": "2025-01-02",
                "report_url": f"https://smida.gov.ua/db/feed/showform/discl/{index}",
            }
            for index in range(1, 8)
        ]
    )
    yield db
    db.close()


@pytest.fixture(autouse=True)
def patch_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extractor_module, "SmidaClient", FakeSmidaClient)
    FakeSmidaClient.fail_ids = set()


def test_extract_marks_downloaded_and_writes_dataset(queue: QueueDB, tmp_path: Path) -> None:
    settings = Settings.from_env(home=tmp_path)
    stats = asyncio.run(extract(settings, queue, workers=2))

    assert stats.requested == 7
    assert stats.parsed == 7
    assert stats.failed == 0
    assert stats.rows == 7
    assert stats.total_rows_in_dataset == 7
    assert queue.stats()["by_status"] == {"downloaded": 7}

    frame = pd.read_parquet(settings.raw_path)
    assert len(frame) == 7
    assert set(frame["isin"]) == {"UA4000000001"}
    assert frame["price"].iloc[0] == pytest.approx(100.5)
    assert frame["trade_date"].iloc[0] == datetime(2025, 1, 2)
    assert {"year", "exchange_code"} <= set(frame.columns)
    assert list(frame["exchange_code"].unique()) == ["PFTS"]


def test_checkpoints_persist_partial_progress(queue: QueueDB, tmp_path: Path) -> None:
    settings = Settings.from_env(home=tmp_path)
    base = Settings.from_env(home=tmp_path)
    settings = Settings(
        home=settings.home,
        storage_dir=settings.storage_dir,
        output_dir=settings.output_dir,
        db_path=settings.db_path,
        raw_path=settings.raw_path,
        checkpoint_every=2,
    )

    stats = asyncio.run(extract(settings, queue, workers=3))

    assert stats.parsed == 7
    assert stats.total_rows_in_dataset == 7
    assert queue.stats()["by_status"] == {"downloaded": 7}
    assert base.raw_path.exists()


def test_failed_report_is_isolated(queue: QueueDB, tmp_path: Path) -> None:
    FakeSmidaClient.fail_ids = {"3"}
    settings = Settings.from_env(home=tmp_path)

    stats = asyncio.run(extract(settings, queue, workers=2))

    assert stats.parsed == 6
    assert stats.failed == 1
    assert stats.errors[0][0] == "3"
    statuses = queue.stats()["by_status"]
    assert statuses == {"downloaded": 6, "error": 1}
    assert len(pd.read_parquet(settings.raw_path)) == 6

    # A retry pass picks the failed report up again.
    FakeSmidaClient.fail_ids = set()
    retry = asyncio.run(extract(settings, queue, workers=1, include_errors=True))
    assert retry.parsed == 1
    assert queue.stats()["by_status"] == {"downloaded": 7}
    assert len(pd.read_parquet(settings.raw_path)) == 7


def test_dry_run_does_not_touch_queue_or_disk(queue: QueueDB, tmp_path: Path) -> None:
    settings = Settings.from_env(home=tmp_path)

    stats = asyncio.run(extract(settings, queue, workers=2, dry_run=True))

    assert stats.rows == 7
    assert queue.stats()["by_status"] == {"pending": 7}
    assert not settings.raw_path.exists()


def test_date_normalisation_in_fixture() -> None:
    from smida_tape.parsing import parse_report_form

    form = parse_report_form(FORM)
    assert form.meta.as_of_date == date(2025, 1, 2)
    assert form.trades[0]["price"] == pytest.approx(100.5)
