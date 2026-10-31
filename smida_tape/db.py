"""SQLite-backed work queue for SMIDA disclosures."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports_queue (
    report_id     TEXT PRIMARY KEY,
    exchange_code TEXT NOT NULL,
    edrpou        TEXT NOT NULL,
    report_date   TEXT NOT NULL,          -- ISO date (information date)
    report_url    TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'downloaded', 'error')),
    published_at  TEXT,                   -- ISO datetime of publication in SMIDA
    xml_url       TEXT,                   -- static machine-readable copy of the tape
    as_of_date    TEXT,                   -- date from the report form
    trades_rows   INTEGER DEFAULT 0,
    source        TEXT,                   -- 'html' | 'xml' (transport actually used)
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_status  ON reports_queue(status);
CREATE INDEX IF NOT EXISTS idx_reports_exch    ON reports_queue(exchange_code, report_date);
CREATE TABLE IF NOT EXISTS crawl_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    exchange_code TEXT,
    date_from  TEXT,
    date_to    TEXT,
    pages      INTEGER DEFAULT 0,
    discovered INTEGER DEFAULT 0,
    note       TEXT
);
"""


def utcnow() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class QueueItem:
    report_id: str
    exchange_code: str
    edrpou: str
    report_date: str
    report_url: str
    status: str
    xml_url: str = ""


class QueueDB:
    """Thin, synchronous wrapper around the metadata database.

    Crawling and extraction are async, but the database is only touched from
    the coordinating coroutine, so a single connection is enough.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")

    # -- lifecycle ---------------------------------------------------------
    def init_schema(self) -> None:
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after the first schema version."""
        existing = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(reports_queue)").fetchall()
        }
        for column, ddl in (("xml_url", "TEXT"), ("source", "TEXT")):
            if column not in existing:
                log.info("migrating reports_queue: adding column %s", column)
                self._conn.execute(f"ALTER TABLE reports_queue ADD COLUMN {column} {ddl}")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        self.init_schema()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # -- writes ------------------------------------------------------------
    def upsert_reports(self, records: Iterable[dict]) -> int:
        """Insert newly discovered reports, never downgrading downloaded ones."""
        rows = [
            (
                str(r["report_id"]),
                r["exchange_code"],
                r["edrpou"],
                r["report_date"],
                r["report_url"],
                (r.get("published_at") or "")[:19] or None,
                r.get("xml_url") or None,
                utcnow(),
            )
            for r in records
        ]
        if not rows:
            return 0
        sql = """
        INSERT INTO reports_queue
            (report_id, exchange_code, edrpou, report_date, report_url, status,
             published_at, xml_url, updated_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
        ON CONFLICT(report_id) DO UPDATE SET
            exchange_code = excluded.exchange_code,
            edrpou        = excluded.edrpou,
            report_date   = excluded.report_date,
            report_url    = excluded.report_url,
            published_at  = COALESCE(excluded.published_at, reports_queue.published_at),
            -- the XML link is pure metadata: refresh it even for done reports
            xml_url       = COALESCE(excluded.xml_url, reports_queue.xml_url),
            updated_at    = excluded.updated_at
        WHERE reports_queue.status <> 'downloaded'
           OR reports_queue.xml_url IS NULL
        """
        with self._tx() as conn:
            before = conn.execute("SELECT COUNT(*) FROM reports_queue").fetchone()[0]
            conn.executemany(sql, rows)
            after = conn.execute("SELECT COUNT(*) FROM reports_queue").fetchone()[0]
        return after - before

    def mark_downloaded(
        self, report_id: str, as_of: str | None, rows: int, source: str | None = None
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """UPDATE reports_queue
                   SET status='downloaded', as_of_date=?, trades_rows=?, error=NULL,
                       source=?, attempts=attempts+1, updated_at=?
                   WHERE report_id=?""",
                (as_of, rows, source, utcnow(), report_id),
            )

    def mark_downloaded_many(
        self, items: Sequence[tuple[str, str | None, int, str | None]]
    ) -> None:
        """Mark several reports as downloaded in one transaction."""
        if not items:
            return
        timestamp = utcnow()
        with self._tx() as conn:
            conn.executemany(
                """UPDATE reports_queue
                   SET status='downloaded', as_of_date=?, trades_rows=?, error=NULL,
                       source=?, attempts=attempts+1, updated_at=?
                   WHERE report_id=?""",
                [
                    (as_of, rows, source, timestamp, report_id)
                    for report_id, as_of, rows, source in items
                ],
            )

    def mark_error(self, report_id: str, message: str) -> None:
        with self._tx() as conn:
            conn.execute(
                """UPDATE reports_queue
                   SET status='error', error=?, attempts=attempts+1, updated_at=?
                   WHERE report_id=?""",
                (message[:1000], utcnow(), report_id),
            )

    def requeue_errors(self, exchange: str | None = None) -> int:
        sql = "UPDATE reports_queue SET status='pending', updated_at=? WHERE status='error'"
        params: list[object] = [utcnow()]
        if exchange:
            sql += " AND exchange_code=?"
            params.append(exchange)
        with self._tx() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount

    def reset_all(self) -> int:
        with self._tx() as conn:
            cur = conn.execute("UPDATE reports_queue SET status='pending', updated_at=?", (utcnow(),))
            return cur.rowcount

    def log_crawl(self, exchange: str | None, date_from: str, date_to: str, pages: int,
                  discovered: int, note: str = "") -> None:
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO crawl_log (started_at, finished_at, exchange_code, date_from, date_to, pages, discovered, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (utcnow(), utcnow(), exchange, date_from, date_to, pages, discovered, note),
            )

    # -- reads -------------------------------------------------------------
    def pending(
        self,
        limit: int | None = None,
        exchange: str | None = None,
        include_downloaded: bool = False,
        include_errors: bool = False,
    ) -> list[QueueItem]:
        statuses = ["pending"]
        if include_errors:
            statuses.append("error")
        if include_downloaded:
            statuses.append("downloaded")
        placeholders = ",".join("?" for _ in statuses)
        sql = f"""SELECT report_id, exchange_code, edrpou, report_date, report_url, status,
                         COALESCE(xml_url, '') AS xml_url
                  FROM reports_queue
                  WHERE status IN ({placeholders})"""
        params: list[object] = list(statuses)
        if exchange:
            sql += " AND exchange_code=?"
            params.append(exchange)
        sql += " ORDER BY report_date, report_id"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [QueueItem(**dict(row)) for row in rows]

    def stats(self) -> dict[str, object]:
        by_status = {
            row["status"]: row["n"]
            for row in self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM reports_queue GROUP BY status"
            )
        }
        by_exchange = {
            f"{row['exchange_code']}:{row['status']}": row["n"]
            for row in self._conn.execute(
                "SELECT exchange_code, status, COUNT(*) AS n FROM reports_queue "
                "GROUP BY exchange_code, status ORDER BY exchange_code"
            )
        }
        by_source = {
            str(row["source"] or "—"): row["n"]
            for row in self._conn.execute(
                "SELECT source, COUNT(*) AS n FROM reports_queue "
                "WHERE status='downloaded' GROUP BY source ORDER BY 2 DESC"
            )
        }
        span = self._conn.execute(
            "SELECT MIN(report_date) AS first, MAX(report_date) AS last, "
            "COALESCE(SUM(trades_rows), 0) AS trades FROM reports_queue"
        ).fetchone()
        return {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "by_exchange": by_exchange,
            "by_source": by_source,
            "first_report_date": span["first"],
            "last_report_date": span["last"],
            "trade_rows_parsed": span["trades"],
        }

    def year_matrix(self) -> dict[str, dict[str, int]]:
        """Reports per year and exchange: ``{year: {exchange: count, "TOTAL": n}}``."""
        rows = self._conn.execute(
            """SELECT substr(report_date, 1, 4) AS year, exchange_code, COUNT(*) AS n
               FROM reports_queue
               GROUP BY year, exchange_code
               ORDER BY year, exchange_code"""
        ).fetchall()
        matrix: dict[str, dict[str, int]] = {}
        for row in rows:
            bucket = matrix.setdefault(row["year"], {})
            bucket[row["exchange_code"]] = row["n"]
        for bucket in matrix.values():
            bucket["TOTAL"] = sum(bucket.values())
        return matrix

    def year_status_matrix(self) -> dict[str, dict[str, int]]:
        """Reports per year and status (pending / downloaded / error)."""
        rows = self._conn.execute(
            """SELECT substr(report_date, 1, 4) AS year, status, COUNT(*) AS n
               FROM reports_queue
               GROUP BY year, status
               ORDER BY year, status"""
        ).fetchall()
        matrix: dict[str, dict[str, int]] = {}
        for row in rows:
            matrix.setdefault(row["year"], {})[row["status"]] = row["n"]
        return matrix

    def report_ids(self, exchange: str | None = None, status: str | None = None) -> Sequence[str]:
        sql = "SELECT report_id FROM reports_queue"
        clauses, params = [], []
        if exchange:
            clauses.append("exchange_code=?")
            params.append(exchange)
        if status:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return [row[0] for row in self._conn.execute(sql, params)]
