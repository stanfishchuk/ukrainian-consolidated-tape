from __future__ import annotations

from pathlib import Path

import pytest

from smida_tape.db import QueueDB


@pytest.fixture()
def db(tmp_path: Path) -> QueueDB:
    database = QueueDB(tmp_path / "metadata.db")
    database.init_schema()
    yield database
    database.close()


def record(report_id: str, exchange: str = "PFTS") -> dict:
    return {
        "report_id": report_id,
        "exchange_code": exchange,
        "edrpou": "21672206",
        "report_date": "2025-12-30",
        "report_url": f"https://smida.gov.ua/db/feed/showform/discl/{report_id}",
        "published_at": "2025-12-30T17:55:01",
    }


def test_upsert_is_idempotent(db: QueueDB) -> None:
    assert db.upsert_reports([record("1"), record("2")]) == 2
    db.upsert_reports([record("1"), record("2")])
    assert db.stats()["total"] == 2
    assert db.stats()["by_status"] == {"pending": 2}


def test_upsert_keeps_downloaded_status(db: QueueDB) -> None:
    db.upsert_reports([record("1")])
    db.mark_downloaded("1", "2025-12-30", 42)

    db.upsert_reports([record("1")])

    pending = db.pending()
    assert pending == []
    assert db.stats()["trade_rows_parsed"] == 42


def test_mark_error_and_requeue(db: QueueDB) -> None:
    db.upsert_reports([record("1"), record("2", "UX")])
    db.mark_error("1", "boom")

    assert db.stats()["by_status"] == {"pending": 1, "error": 1}
    assert db.requeue_errors() == 1
    assert db.stats()["by_status"] == {"pending": 2}
    assert db.requeue_errors("UX") == 0


def test_pending_filters(db: QueueDB) -> None:
    db.upsert_reports([record("1"), record("2", "UX"), record("3", "PERSPEKTIVA")])
    assert len(db.pending()) == 3
    assert len(db.pending(exchange="UX")) == 1
    assert [item.report_id for item in db.pending(limit=1)] == ["1"]
    assert len(db.pending(include_downloaded=True)) == 3


def test_year_matrix(db: QueueDB) -> None:
    db.upsert_reports(
        [
            record("1"),
            record("2", "UX"),
            {**record("3", "UX"), "report_date": "2024-01-15"},
        ]
    )

    matrix = db.year_matrix()
    assert matrix["2025"] == {"PFTS": 1, "UX": 1, "TOTAL": 2}
    assert matrix["2024"] == {"UX": 1, "TOTAL": 1}

    statuses = db.year_status_matrix()
    assert statuses["2025"] == {"pending": 2}
    assert statuses["2024"] == {"pending": 1}
