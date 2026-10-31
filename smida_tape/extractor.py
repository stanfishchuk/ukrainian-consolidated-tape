"""Module 2 — extraction of the exchange trade tape from disclosures.

Two transports carry the same tape:

* ``/db/feed/showform/discl/{id}`` — the rendered HTML form with the table
  "Інформація про укладені (виконані) біржові контракти (договори РЕПО)…".
  Richest metadata (free-text instrument description) but expensive: heavy
  reports are 1–3 MB and can take 30–60 s, some exceed SMIDA's own timeout.
* ``/files/feed/.../report.xml`` — the static machine-readable original.
  ~0.1 s regardless of size, so it is the only reliable way to ingest the
  heavy reports, and it exposes the НКЦПФР instrument code and the ISO 10962
  CFI class instead of the free-text description.

``source="auto"`` (default) prefers XML and falls back to HTML; both paths
produce exactly the same row schema.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .config import Settings, exchange_for_edrpou
from .db import QueueDB
from .http_client import SmidaClient, SmidaError
from .parquet_io import merge_raw_dataset, read_raw_dataset
from .parsing import (
    TRADE_COLUMNS,
    ReportForm,
    is_valid_isin,
    parse_report_form,
    parse_report_xml,
)

log = logging.getLogger(__name__)


class ExtractionError(RuntimeError):
    pass


def _instrument_names(raw_path: Path) -> dict[str, str]:
    """ISIN -> free-text instrument description, learned from HTML parses.

    XML rows only carry the classifier codes, so every report already ingested
    through the HTML form doubles as a dictionary for the rest.
    """
    frame = read_raw_dataset(raw_path, years=None)
    if frame.empty:
        return {}
    text = frame["instrument_type"].astype("string")
    keep = text.notna() & (text != "") & ~text.str.contains("CFI ", na=False)
    subset = frame[keep.fillna(False)]
    if subset.empty:
        return {}
    subset = subset.drop_duplicates("isin", keep="last")
    return dict(zip(subset["isin"], subset["instrument_type"], strict=False))


@dataclass
class ExtractionStats:
    requested: int = 0
    parsed: int = 0
    failed: int = 0
    empty_reports: int = 0
    rows: int = 0
    duplicates_in_source: int = 0
    invalid_isin: int = 0
    missing_price: int = 0
    missing_quantity: int = 0
    warnings: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    total_rows_in_dataset: int = 0
    by_source: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "requested": self.requested,
            "parsed": self.parsed,
            "failed": self.failed,
            "empty_reports": self.empty_reports,
            "rows_extracted": self.rows,
            "duplicate_rows_in_source": self.duplicates_in_source,
            "invalid_isin": self.invalid_isin,
            "missing_price": self.missing_price,
            "missing_quantity": self.missing_quantity,
            "warnings": self.warnings,
            "total_rows_in_dataset": self.total_rows_in_dataset,
            "transports": self.by_source,
            "errors": [{"report_id": rid, "error": msg} for rid, msg in self.errors[:50]],
        }


def _finalise(
    form,
    report_id: str,
    exchange_code: str,
    names: dict[str, str],
) -> tuple[list[dict], str | None, int, str]:
    code = exchange_for_edrpou(form.meta.exchange_edrpou) or exchange_code
    as_of = form.meta.as_of_date.isoformat() if form.meta.as_of_date else None

    rows: list[dict] = []
    duplicates = 0
    seen: set[tuple] = set()
    for trade in form.trades:
        key = tuple(trade[column] for column in TRADE_COLUMNS)
        if key in seen:
            duplicates += 1
        seen.add(key)
        # Prefer the free-text description learned from an HTML form.
        learned = names.get(trade["isin"])
        if learned:
            trade["instrument_type"] = learned
        trade["exchange_code"] = code
        trade["report_id"] = report_id
        rows.append(trade)
    return rows, as_of, duplicates, form.source


async def _from_html(client: SmidaClient, report_id: str) -> ReportForm:
    form = parse_report_form(await client.report_form(report_id))
    form.source = "html"
    unmapped = [warning for warning in form.warnings if warning.startswith("unmapped")]
    if unmapped:
        raise ExtractionError(f"cannot map trade table columns ({unmapped[0]})")
    # An exchange may legitimately disclose "no contracts were executed" for
    # the day: the form keeps the metadata block and leaves the trade section
    # empty.  That is a valid zero-row report, not a parse failure.
    if not form.trades and "trade table not found" in form.warnings and not form.meta.exchange_edrpou:
        raise ExtractionError("neither trade table nor exchange metadata found")
    return form


async def _from_xml(client: SmidaClient, report_id: str, xml_url: str) -> ReportForm:
    form = parse_report_xml(await client.report_xml(xml_url))
    if not form.meta.exchange_edrpou and not form.trades:
        raise ExtractionError("empty report.xml (no metadata, no rows)")
    return form


async def _extract_one(
    client: SmidaClient,
    item,
    names: dict[str, str] | None = None,
    source: str = "auto",
) -> tuple[list[dict], str | None, int, str]:
    names = names or {}
    report_id = item.report_id
    xml_url = getattr(item, "xml_url", "") or ""

    order: list[str] = []
    if source == "xml":
        order = ["xml"]
    elif source == "html":
        order = ["html"]
    else:
        order = ["xml", "html"] if xml_url else ["html"]

    form = None
    errors: list[str] = []
    for transport in order:
        try:
            if transport == "xml":
                url = xml_url
                if not url:
                    # The card page is cheap and links to the XML file.
                    url = await client.report_xml_url(report_id) or ""
                if not url:
                    raise ExtractionError("report.xml link not found")
                form = await _from_xml(client, report_id, url)
            else:
                form = await _from_html(client, report_id)
            break
        except (SmidaError, ExtractionError) as exc:
            errors.append(f"{transport}: {exc}")

    if form is None:
        raise ExtractionError("; ".join(errors) or "no transport succeeded")
    return _finalise(form, report_id, item.exchange_code, names)


def _validate(rows: list[dict], stats: ExtractionStats) -> None:
    for row in rows:
        if not is_valid_isin(row.get("isin")):
            stats.invalid_isin += 1
        if row.get("price") is None:
            stats.missing_price += 1
        if row.get("quantity") is None:
            stats.missing_quantity += 1


async def extract(
    settings: Settings,
    db: QueueDB,
    *,
    exchange: str | None = None,
    limit: int | None = None,
    workers: int = 3,
    include_errors: bool = False,
    redownload: bool = False,
    dry_run: bool = False,
    raw_path: Path | None = None,
    source: str = "auto",
) -> ExtractionStats:
    settings.ensure_dirs()
    db.init_schema()
    raw_path = Path(raw_path or settings.raw_path)
    items = db.pending(
        limit=limit,
        exchange=exchange,
        include_downloaded=redownload,
        include_errors=include_errors,
    )
    if source != "html":
        # Cheap transports first: reports with a static XML copy finish in
        # milliseconds, the HTML-only leftovers are the slow ones.  Doing them
        # last keeps the queue moving and lets checkpoints land early.
        items.sort(key=lambda item: (not item.xml_url, item.report_date, item.report_id))

    stats = ExtractionStats(requested=len(items))
    if not items:
        log.info("queue is empty — run `crawl` first")
        stats.total_rows_in_dataset = len(read_raw_dataset(raw_path))
        return stats

    names = _instrument_names(raw_path)
    log.info(
        "extracting %s reports (%s workers, <= %.1f req/s, source=%s, %s known instrument names)",
        len(items), workers, settings.max_rps, source, len(names),
    )
    pending_rows: list[dict] = []
    pending_done: list[tuple[str, str | None, int, str | None]] = []
    completed = 0
    checkpoints = 0
    lock = asyncio.Lock()

    def checkpoint(force: bool = False) -> int:
        """Persist the rows parsed so far and mark those reports as downloaded.

        Nothing outside this function marks a report as ``downloaded``, so a
        killed run can never lose rows that the queue considers done.
        """
        nonlocal pending_rows, pending_done, checkpoints
        if dry_run or not pending_rows:
            return 0
        if not force and len(pending_done) < settings.checkpoint_every:
            return 0
        rows, done = pending_rows, pending_done
        pending_rows, pending_done = [], []
        _, written = merge_raw_dataset(pd.DataFrame(rows), raw_path)
        db.mark_downloaded_many(done)
        checkpoints += 1
        log.info(
            "  checkpoint %s: +%s reports, +%s rows (dataset: %s rows)",
            checkpoints, len(done), len(rows), written,
        )
        return written

    async with SmidaClient(settings) as client:
        semaphore = asyncio.Semaphore(max(1, workers))

        async def process(item) -> None:
            nonlocal completed
            async with semaphore:
                try:
                    rows, as_of, duplicates, transport = await _extract_one(
                        client, item, names, source
                    )
                except (SmidaError, ExtractionError, ValueError, AttributeError) as exc:
                    async with lock:
                        stats.failed += 1
                        stats.errors.append((item.report_id, str(exc)[:300]))
                        if not dry_run:
                            db.mark_error(item.report_id, str(exc))
                    log.warning("report %s failed: %s", item.report_id, exc)
                    return
                except Exception as exc:
                    async with lock:
                        stats.failed += 1
                        stats.errors.append((item.report_id, f"{type(exc).__name__}: {exc}"[:300]))
                        if not dry_run:
                            db.mark_error(item.report_id, f"{type(exc).__name__}: {exc}")
                    log.exception("report %s crashed the parser", item.report_id)
                    return

                async with lock:
                    completed += 1
                    _validate(rows, stats)
                    stats.duplicates_in_source += duplicates
                    if not rows:
                        stats.empty_reports += 1
                    stats.parsed += 1
                    stats.rows += len(rows)
                    stats.by_source[transport] = stats.by_source.get(transport, 0) + 1
                    pending_rows.extend(rows)
                    pending_done.append((item.report_id, as_of, len(rows), transport))
                    if completed % 100 == 0 or completed == len(items):
                        log.info("  %s/%s reports, %s trade rows", completed, len(items), stats.rows)
                    try:
                        stats.total_rows_in_dataset = checkpoint() or stats.total_rows_in_dataset
                    except OSError as exc:  # disk full / permissions
                        log.error("checkpoint failed: %s", exc)

        tasks = [asyncio.create_task(process(item)) for item in items]
        try:
            await asyncio.gather(*tasks)
        finally:
            # Whatever finished before an interruption is still on disk.
            if not dry_run:
                try:
                    stats.total_rows_in_dataset = checkpoint(force=True) or stats.total_rows_in_dataset
                except OSError as exc:
                    log.error("final checkpoint failed: %s", exc)

    if dry_run:
        log.info("dry-run: %s rows parsed, dataset not modified", stats.rows)
        return stats
    if stats.total_rows_in_dataset == 0:
        stats.total_rows_in_dataset = len(read_raw_dataset(raw_path))
    log.info(
        "raw dataset: %s rows total, %s reports parsed this pass -> %s",
        stats.total_rows_in_dataset,
        stats.parsed,
        raw_path,
    )
    return stats
