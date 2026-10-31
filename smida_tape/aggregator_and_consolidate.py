"""Module 3 — ETL for the econometric model (Fyshchuk's consolidated tape).

Pipeline
--------
1. Deduplicate the source (SMIDA publishes every contract twice per form).
2. Split the tape into spot trades (``repo_part == "-"``) and REPO legs.
3. Build daily OHLCV bars per (instrument, exchange).
4. Consolidate the exchanges per the dissertation model:

   * ``V_cons = V_PFTS + V_UX`` (if the sum is positive, otherwise
     ``V_Perspektiva``);
   * ``P_cons`` = price of the exchange with the larger volume among PFTS/UX,
     falling back to Perspektiva when both are zero;
   * ``R_t = ln(P_cons_t / P_cons_{t-1})`` per instrument.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .config import EXCHANGE_CODES, PRICING_EXCHANGES, Settings
from .parquet_io import RAW_COLUMNS, read_raw_dataset

log = logging.getLogger(__name__)

SPOT = "-"
REPO_PARTS = ("1", "2")


@dataclass
class AggregationStats:
    rows_raw: int = 0
    rows_after_dedupe: int = 0
    duplicates_removed: int = 0
    dropped_invalid_price: int = 0
    dropped_invalid_quantity: int = 0
    dropped_without_key: int = 0
    spot_rows: int = 0
    repo_rows: int = 0
    bars: int = 0
    instruments: int = 0
    consolidated_rows: int = 0
    returns_computed: int = 0
    price_source: dict[str, int] | None = None
    volume_source: dict[str, int] | None = None
    first_date: str | None = None
    last_date: str | None = None

    def as_dict(self) -> dict:
        return {
            "rows_raw": self.rows_raw,
            "rows_after_dedupe": self.rows_after_dedupe,
            "duplicates_removed": self.duplicates_removed,
            "dropped_invalid_price": self.dropped_invalid_price,
            "dropped_invalid_quantity": self.dropped_invalid_quantity,
            "dropped_without_instrument_key": self.dropped_without_key,
            "spot_rows": self.spot_rows,
            "repo_rows": self.repo_rows,
            "daily_bars": self.bars,
            "instruments": self.instruments,
            "consolidated_rows": self.consolidated_rows,
            "log_returns_computed": self.returns_computed,
            "price_source": self.price_source or {},
            "volume_source": self.volume_source or {},
            "first_date": self.first_date,
            "last_date": self.last_date,
        }


def instrument_key(frame: pd.DataFrame) -> pd.Series:
    """Prefer ISIN; fall back to EDRPOU+instrument for malformed codes."""
    isin = frame["isin"].fillna("").astype(str).str.strip()
    fallback = (
        frame["issuer_edrpou"].fillna("").astype(str)
        + ":"
        + frame["instrument_type"].fillna("").astype(str).str.slice(0, 60)
    )
    return isin.where(isin.str.len() > 0, fallback)


def deduplicate(frame: pd.DataFrame, stats: AggregationStats) -> pd.DataFrame:
    """Drop byte-identical rows inside the same report.

    SMIDA's disclosure form lists every executed contract twice (both sides of
    the trade).  Keeping both would double volume and trade counts, so the
    duplicates are removed *within a report*; identical trades reported by
    different reports are preserved.
    """
    subset = list(RAW_COLUMNS)
    before = len(frame)
    frame = frame.drop_duplicates(subset=subset, keep="first")
    stats.duplicates_removed = before - len(frame)
    return frame.reset_index(drop=True)


def split_spot_repo(frame: pd.DataFrame, stats: AggregationStats) -> tuple[pd.DataFrame, pd.DataFrame]:
    spot = frame[frame["repo_part"] == SPOT].copy()
    repo = frame[frame["repo_part"].isin(REPO_PARTS)].copy()
    stats.spot_rows = len(spot)
    stats.repo_rows = len(repo)
    return spot, repo


def build_daily_bars(trades: pd.DataFrame) -> pd.DataFrame:
    """Aggregate spot trades into daily OHLCV bars per instrument and exchange."""
    if trades.empty:
        return pd.DataFrame(
            columns=[
                "trade_date", "exchange_code", "instrument_key", "isin", "issuer_name",
                "issuer_edrpou", "instrument_type", "open", "high", "low", "close",
                "volume_units", "turnover_uah", "vwap", "trades_count",
            ]
        )

    frame = trades.copy()
    # Defensive coercion: the Parquet dataset is already typed, but the module
    # must also survive frames assembled by hand or re-read from CSV.
    frame["quantity"] = pd.to_numeric(frame["quantity"], errors="coerce")
    frame["price"] = pd.to_numeric(frame["price"], errors="coerce")
    frame = frame[(frame["price"] > 0) & (frame["quantity"] > 0)]
    if frame.empty:
        return build_daily_bars(trades.iloc[0:0])
    frame["instrument_key"] = instrument_key(frame)
    frame["turnover_uah"] = frame["quantity"] * frame["price"]
    frame = frame.sort_values(
        ["instrument_key", "exchange_code", "trade_date", "report_id"], kind="stable"
    )

    grouped = frame.groupby(
        ["trade_date", "exchange_code", "instrument_key"], sort=True, observed=True
    )
    bars = grouped.agg(
        open=("price", "first"),
        high=("price", "max"),
        low=("price", "min"),
        close=("price", "last"),
        volume_units=("quantity", "sum"),
        turnover_uah=("turnover_uah", "sum"),
        trades_count=("price", "size"),
        isin=("isin", "last"),
        issuer_name=("issuer_name", "last"),
        issuer_edrpou=("issuer_edrpou", "last"),
        instrument_type=("instrument_type", "last"),
    ).reset_index()

    bars["vwap"] = np.where(
        bars["volume_units"] > 0, bars["turnover_uah"] / bars["volume_units"], np.nan
    )
    return bars[
        [
            "trade_date", "exchange_code", "instrument_key", "isin", "issuer_name",
            "issuer_edrpou", "instrument_type", "open", "high", "low", "close",
            "volume_units", "turnover_uah", "vwap", "trades_count",
        ]
    ]


def _column(frame: pd.DataFrame, column: str, fill: float | None = None) -> pd.Series:
    """Return a column of a pivot or a matching zero/NaN series."""
    if column in frame.columns:
        series = frame[column]
        return series.fillna(fill) if fill is not None else series
    return pd.Series(np.nan if fill is None else fill, index=frame.index, dtype="float64")


def consolidate(bars: pd.DataFrame, *, price_column: str = "close") -> pd.DataFrame:
    """Cross-exchange consolidation per (instrument, day) following the model."""
    if bars.empty:
        return pd.DataFrame(
            columns=[
                "trade_date", "instrument_key", "isin", "issuer_name", "issuer_edrpou",
                "instrument_type", "volume_pfts", "volume_ux", "volume_persp",
                "price_pfts", "price_ux", "price_persp", "volume_cons", "price_cons",
                "turnover_cons", "price_source", "volume_source", "log_return",
            ]
        )

    if price_column not in bars.columns:
        raise ValueError(f"unknown price column {price_column!r}")
    index = ["trade_date", "instrument_key"]
    volume = bars.pivot_table(
        index=index, columns="exchange_code", values="volume_units", aggfunc="sum"
    )
    prices = bars.pivot_table(
        index=index, columns="exchange_code", values=price_column, aggfunc="last"
    )
    turnover = bars.pivot_table(
        index=index, columns="exchange_code", values="turnover_uah", aggfunc="sum"
    )

    for code in EXCHANGE_CODES:
        if code not in volume.columns:
            volume[code] = 0.0
        if code not in prices.columns:
            prices[code] = np.nan
        if code not in turnover.columns:
            turnover[code] = 0.0
    volume = volume.fillna(0.0)
    turnover = turnover.fillna(0.0)

    pfts, ux = PRICING_EXCHANGES
    v_p, v_u, v_s = volume[pfts], volume[ux], volume["PERSPEKTIVA"]
    p_p, p_u, p_s = prices[pfts], prices[ux], prices["PERSPEKTIVA"]
    t_p, t_u, t_s = turnover[pfts], turnover[ux], turnover["PERSPEKTIVA"]

    paired_volume = v_p + v_u
    use_pair = paired_volume > 0
    pfts_wins = v_p >= v_u  # tie -> PFTS, documented modelling choice

    paired_price = np.where(pfts_wins, p_p, p_u)
    # If the winning exchange has no usable price, fall back to the other one.
    paired_price = np.where(np.isnan(paired_price), np.where(pfts_wins, p_u, p_p), paired_price)

    both_sides = (v_p > 0) & (v_u > 0)
    single_side = np.where(v_p > 0, pfts, ux)

    out = pd.DataFrame(index=volume.index)
    out["volume_pfts"] = v_p
    out["volume_ux"] = v_u
    out["volume_persp"] = v_s
    out["price_pfts"] = p_p
    out["price_ux"] = p_u
    out["price_persp"] = p_s
    out["volume_cons"] = np.where(use_pair, paired_volume, v_s)
    out["price_cons"] = np.where(use_pair, paired_price, p_s)
    out["turnover_cons"] = np.where(use_pair, t_p + t_u, t_s)
    out["price_source"] = np.where(use_pair, np.where(pfts_wins, pfts, ux), "PERSPEKTIVA")
    out["volume_source"] = np.where(
        use_pair, np.where(both_sides, f"{pfts}+{ux}", single_side), "PERSPEKTIVA"
    )

    # Static instrument attributes (last non-null observation wins).
    static = (
        bars.sort_values("trade_date")
        .groupby("instrument_key", as_index=True)[["isin", "issuer_name", "issuer_edrpou", "instrument_type"]]
        .last()
    )
    out = out.reset_index().merge(static.reset_index(), on="instrument_key", how="left")

    return out[
        [
            "trade_date", "instrument_key", "isin", "issuer_name", "issuer_edrpou",
            "instrument_type", "volume_pfts", "volume_ux", "volume_persp",
            "price_pfts", "price_ux", "price_persp", "volume_cons", "price_cons",
            "turnover_cons", "price_source", "volume_source",
        ]
    ]


def add_log_returns(consolidated: pd.DataFrame) -> pd.DataFrame:
    """R_t = ln(P_t / P_{t-1}) computed on each instrument's own time series.

    Returns are chained over consecutive *observations* of the instrument: if a
    day is missing from the tape, the return is measured against the previous
    available price (gaps are not interpolated).
    """
    if consolidated.empty:
        consolidated["log_return"] = pd.Series(dtype="float64")
        return consolidated

    frame = consolidated.sort_values(["instrument_key", "trade_date"], kind="stable").reset_index(drop=True)
    tradable = frame["price_cons"] > 0
    log_price = np.log(frame.loc[tradable, "price_cons"])
    returns = log_price.groupby(frame.loc[tradable, "instrument_key"]).diff()
    frame["log_return"] = returns.reindex(frame.index)
    return frame


def _write_frame(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    table = pa.Table.from_pandas(frame, preserve_index=False)
    pq.write_table(table, tmp, compression="zstd", compression_level=7, row_group_size=200_000)
    tmp.replace(path)


def run_aggregation(
    settings: Settings,
    *,
    year_from: int = 2015,
    year_to: int = 2025,
    drop_duplicates: bool = True,
    price_column: str = "close",
    raw_path: Path | None = None,
) -> tuple[AggregationStats, Path | None]:
    settings.ensure_dirs()
    raw_path = Path(raw_path or settings.raw_path)
    frame = read_raw_dataset(raw_path, years=range(year_from, year_to + 1))
    stats = AggregationStats(rows_raw=len(frame))
    if frame.empty:
        log.error("raw dataset is empty: %s (run crawl + extract first)", raw_path)
        return stats, None

    frame["repo_part"] = frame["repo_part"].fillna("-").replace({"": "-"})
    if drop_duplicates:
        frame = deduplicate(frame, stats)
    stats.rows_after_dedupe = len(frame)

    valid = frame["trade_date"].notna() & (frame["price"] > 0) & (frame["quantity"] > 0)
    stats.dropped_invalid_price = int((frame["price"].isna() | (frame["price"] <= 0)).sum())
    stats.dropped_invalid_quantity = int((frame["quantity"].isna() | (frame["quantity"] <= 0)).sum())
    frame = frame[valid].copy()

    frame["instrument_key"] = instrument_key(frame)
    has_key = frame["instrument_key"].str.len() > 0
    stats.dropped_without_key = int((~has_key).sum())
    frame = frame[has_key]

    if frame.empty:
        log.error("no valid trades after cleaning")
        return stats, None

    spot, repo = split_spot_repo(frame, stats)
    log.info(
        "%s trades: %s spot, %s REPO legs (%s source duplicates removed)",
        len(frame), stats.spot_rows, stats.repo_rows, stats.duplicates_removed,
    )

    bars = build_daily_bars(spot)
    stats.bars = len(bars)
    stats.instruments = int(bars["instrument_key"].nunique()) if not bars.empty else 0

    consolidated = add_log_returns(consolidate(bars, price_column=price_column))
    stats.consolidated_rows = len(consolidated)
    stats.returns_computed = int(consolidated["log_return"].notna().sum())
    stats.price_source = consolidated["price_source"].value_counts().to_dict() if not consolidated.empty else {}
    stats.volume_source = consolidated["volume_source"].value_counts().to_dict() if not consolidated.empty else {}
    if not consolidated.empty:
        stats.first_date = str(consolidated["trade_date"].min().date())
        stats.last_date = str(consolidated["trade_date"].max().date())

    suffix = f"{year_from}_{year_to}"
    output = Path(settings.output_dir)
    consolidated_path = output / f"consolidated_tape_{suffix}.parquet"
    spot_path = output / f"spot_tape_{suffix}.parquet"
    repo_path = output / f"repo_tape_{suffix}.parquet"
    bars_path = output / "daily_bars.parquet"

    _write_frame(consolidated, consolidated_path)
    _write_frame(bars, bars_path)
    if not spot.empty:
        _write_frame(spot.reset_index(drop=True), spot_path)
    if not repo.empty:
        _write_frame(repo.reset_index(drop=True), repo_path)

    report = stats.as_dict() | {
        "raw_path": str(raw_path),
        "consolidated_path": str(consolidated_path),
        "daily_bars_path": str(bars_path),
        "spot_path": str(spot_path) if not spot.empty else None,
        "repo_path": str(repo_path) if not repo.empty else None,
        "price_column": price_column,
    }
    (output / "aggregation_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log.info("consolidated tape: %s rows -> %s", len(consolidated), consolidated_path)
    return stats, consolidated_path
