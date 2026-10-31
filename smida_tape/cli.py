"""Command line interface (``python -m smida_tape <command>``)."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

import click

from .aggregator_and_consolidate import run_aggregation
from .config import EXCHANGES, WINDOW_END, WINDOW_START, Settings
from .crawler import crawl
from .db import QueueDB
from .extractor import extract
from .parsing import parse_report_form, parse_search_page

log = logging.getLogger("smida_tape")


def _parse_cli_date(value: str) -> date:
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise click.BadParameter(f"expected YYYY-MM-DD, got {value!r}")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--home", type=click.Path(file_okay=False, path_type=Path), default=".",
              envvar="SMIDA_HOME", show_default=True, help="Base data directory.")
@click.option("--rps", type=float, default=None, envvar="SMIDA_MAX_RPS",
              help="Max requests per second (hard-capped at 5).")
@click.option("--log-level", default="INFO", envvar="SMIDA_LOG_LEVEL",
              type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
              show_default=True)
@click.pass_context
def main(ctx: click.Context, home: Path, rps: float | None, log_level: str) -> None:
    """SMIDA → consolidated tape pipeline (PFTS / Перспектива / Українська біржа)."""
    _setup_logging(log_level)
    ctx.obj = Settings.from_env(home=home, max_rps=rps)


@main.command()
@click.pass_obj
def initdb(settings: Settings) -> None:
    """Create storage/metadata.db and the required directories."""
    settings.ensure_dirs()
    with QueueDB(settings.db_path) as db:
        db.init_schema()
    click.echo(f"initialised {settings.db_path}")


@main.command("crawl")
@click.option("--exchange", "exchanges", multiple=True,
              type=click.Choice(list(EXCHANGES), case_sensitive=False),
              help="Exchange to crawl (default: all).")
@click.option("--start", "start", default=WINDOW_START.isoformat(),
              callback=lambda c, p, v: _parse_cli_date(v), help="Start date (YYYY-MM-DD).")
@click.option("--end", "end", default=WINDOW_END.isoformat(),
              callback=lambda c, p, v: _parse_cli_date(v), help="End date (YYYY-MM-DD).")
@click.option("--workers", default=4, show_default=True, type=click.IntRange(1, 16),
              help="Concurrent page fetches (keep low: SMIDA's renderer is fragile).")
@click.option("--max-pages", default=None, type=int,
              help="Debug: cap pages per year window.")
@click.pass_obj
def crawl_cmd(settings: Settings, exchanges: tuple[str, ...], start: date, end: date,
              workers: int, max_pages: int | None) -> None:
    """Discover disclosures and fill the SQLite queue."""
    settings.ensure_dirs()
    with QueueDB(settings.db_path) as db:
        results = asyncio.run(
            crawl(settings, db, list(exchanges) or None, start=start, end=end,
                  workers=workers, max_pages=max_pages)
        )
        for code, result in results.items():
            click.echo(f"{code}: {result.discovered} reports / {result.pages} pages")
        stats = db.stats()
    click.echo(json.dumps(stats, indent=2, ensure_ascii=False))


@main.command("extract")
@click.option("--exchange", type=click.Choice(list(EXCHANGES), case_sensitive=False),
              default=None, help="Only reports of one exchange.")
@click.option("--limit", default=None, type=int, help="Process at most N reports.")
@click.option("--workers", default=3, show_default=True, type=click.IntRange(1, 16))
@click.option("--retry-errors", is_flag=True, help="Re-download reports previously marked error.")
@click.option("--redownload", is_flag=True, help="Re-download already downloaded reports.")
@click.option("--dry-run", is_flag=True, help="Parse only; do not write Parquet or touch the queue.")
@click.option("--strict", is_flag=True, help="Exit with code 1 if at least one report failed.")
@click.option("--source", "source", default="auto", show_default=True,
              type=click.Choice(["auto", "html", "xml"]),
              help="auto = static report.xml first (fast), HTML form as fallback.")
@click.pass_obj
def extract_cmd(settings: Settings, exchange: str | None, limit: int | None, workers: int,
                retry_errors: bool, redownload: bool, dry_run: bool, strict: bool,
                source: str) -> None:
    """Parse trade tables of queued reports into storage/raw_trades.parquet."""
    settings.ensure_dirs()
    with QueueDB(settings.db_path) as db:
        stats = asyncio.run(
            extract(
                settings, db,
                exchange=exchange.upper() if exchange else None,
                limit=limit, workers=workers, include_errors=retry_errors,
                redownload=redownload, dry_run=dry_run, source=source,
            )
        )
    click.echo(json.dumps(stats.as_dict(), indent=2, ensure_ascii=False))
    if stats.failed:
        click.echo(
            f"warning: {stats.failed} report(s) failed; they stay in status 'error' "
            f"— rerun with --retry-errors",
            err=True,
        )
        if strict:
            sys.exit(1)


@main.command()
@click.option("--year-from", default=2015, show_default=True, type=int)
@click.option("--year-to", default=2025, show_default=True, type=int)
@click.option("--keep-duplicates", is_flag=True,
              help="Do not remove SMIDA's duplicated contract rows (volume will be doubled).")
@click.option("--price", "price_column", default="close", show_default=True,
              type=click.Choice(["close", "vwap"]),
              help="Exchange price used for consolidation.")
@click.pass_obj
def aggregate(settings: Settings, year_from: int, year_to: int,
              keep_duplicates: bool, price_column: str) -> None:
    """Build daily OHLCV bars and the consolidated cross-exchange tape."""
    stats, path = run_aggregation(
        settings, year_from=year_from, year_to=year_to,
        drop_duplicates=not keep_duplicates, price_column=price_column,
    )
    click.echo(json.dumps(stats.as_dict(), indent=2, ensure_ascii=False))
    if path:
        click.echo(f"written: {path}")


@main.command()
@click.option("--exchange", "exchanges", multiple=True,
              type=click.Choice(list(EXCHANGES), case_sensitive=False))
@click.option("--start", "start", default=WINDOW_START.isoformat(),
              callback=lambda c, p, v: _parse_cli_date(v))
@click.option("--end", "end", default=WINDOW_END.isoformat(),
              callback=lambda c, p, v: _parse_cli_date(v))
@click.option("--workers", default=3, show_default=True, type=click.IntRange(1, 16))
@click.option("--limit", default=None, type=int, help="Debug: extract at most N reports.")
@click.option("--keep-duplicates", is_flag=True)
@click.option("--price", "price_column", default="close", type=click.Choice(["close", "vwap"]))
@click.pass_obj
def pipeline(settings: Settings, exchanges: tuple[str, ...], start: date, end: date,
             workers: int, limit: int | None, keep_duplicates: bool, price_column: str) -> None:
    """Run crawl → extract → aggregate in one go."""
    settings.ensure_dirs()
    click.echo(f"== 1/3 crawl {start}..{end} ==")
    with QueueDB(settings.db_path) as db:
        results = asyncio.run(
            crawl(settings, db, list(exchanges) or None, start=start, end=end, workers=workers)
        )
        for code, result in results.items():
            click.echo(f"{code}: {result.discovered} new reports / {result.pages} pages")

        click.echo("== 2/3 extract ==")
        stats = asyncio.run(
            extract(settings, db, limit=limit, workers=workers, include_errors=True,
                    exchange=exchanges[0].upper() if len(exchanges) == 1 else None)
        )
        click.echo(json.dumps(stats.as_dict(), indent=2, ensure_ascii=False))

    click.echo("== 3/3 aggregate ==")
    agg_stats, path = run_aggregation(
        settings, year_from=start.year, year_to=end.year,
        drop_duplicates=not keep_duplicates, price_column=price_column,
    )
    click.echo(json.dumps(agg_stats.as_dict(), indent=2, ensure_ascii=False))
    if path:
        click.echo(f"consolidated tape: {path}")


@main.command()
@click.option("--by-year", is_flag=True, help="Also print year x exchange and year x status matrices.")
@click.pass_obj
def status(settings: Settings, by_year: bool) -> None:
    """Show queue statistics (add --by-year for a year x exchange matrix)."""
    if not settings.db_path.exists():
        click.echo("no database yet — run `crawl` or `initdb`")
        return
    with QueueDB(settings.db_path) as db:
        db.init_schema()
        stats = db.stats()
        matrix = db.year_matrix() if by_year else None
        year_status = db.year_status_matrix() if by_year else None
    click.echo(json.dumps(stats, indent=2, ensure_ascii=False))
    if not by_year or not matrix:
        return

    columns = [*EXCHANGES, "TOTAL"]
    header = f"{'year':<6}" + "".join(f"{column:>14}" for column in columns)
    click.echo("\nreports per year / exchange")
    click.echo(header)
    click.echo("-" * len(header))
    for year in sorted(matrix):
        row = matrix[year]
        click.echo(f"{year:<6}" + "".join(f"{row.get(column, 0):>14}" for column in columns))
    totals = {column: sum(matrix[year].get(column, 0) for year in matrix) for column in columns}
    click.echo("-" * len(header))
    click.echo(f"{'all':<6}" + "".join(f"{totals[column]:>14}" for column in columns))

    click.echo("\nreports per year / status")
    statuses = ["pending", "downloaded", "error"]
    header = f"{'year':<6}" + "".join(f"{name:>12}" for name in statuses)
    click.echo(header)
    for year in sorted(year_status):
        row = year_status[year]
        click.echo(f"{year:<6}" + "".join(f"{row.get(name, 0):>12}" for name in statuses))


@main.command()
@click.option("--exchange", type=click.Choice(list(EXCHANGES), case_sensitive=False), default=None)
@click.option("--all", "reset_all", is_flag=True, help="Reset every report to pending.")
@click.pass_obj
def requeue(settings: Settings, exchange: str | None, reset_all: bool) -> None:
    """Move failed (or all) reports back to pending."""
    with QueueDB(settings.db_path) as db:
        db.init_schema()
        count = db.reset_all() if reset_all else db.requeue_errors(
            exchange.upper() if exchange else None
        )
    click.echo(f"{count} reports queued again")


@main.command("parse-html")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--kind", type=click.Choice(["search", "report"]), default="report", show_default=True)
@click.pass_obj
def parse_html(settings: Settings, path: Path, kind: str) -> None:
    """Offline debugging: parse a saved SMIDA page and print the result."""
    html = path.read_text(encoding="utf-8", errors="replace")
    if kind == "search":
        page = parse_search_page(html)
        click.echo(f"rows={len(page.rows)} total_pages={page.total_pages}")
        for row in page.rows[:5]:
            click.echo(f"  {row.report_id} {row.published_at} {row.edrpou} {row.entity_name}")
    else:
        form = parse_report_form(html)
        click.echo(
            f"edrpou={form.meta.exchange_edrpou} name={form.meta.exchange_name} "
            f"as_of={form.meta.as_of_date} trades={len(form.trades)} "
            f"warnings={len(form.warnings)}"
        )
        for warning in form.warnings[:5]:
            click.echo(f"  ! {warning}")
        for trade in form.trades[:5]:
            click.echo(f"  {trade}")


if __name__ == "__main__":  # pragma: no cover
    main()
