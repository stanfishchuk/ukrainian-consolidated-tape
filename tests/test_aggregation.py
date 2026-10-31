from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from smida_tape.aggregator_and_consolidate import (
    AggregationStats,
    add_log_returns,
    build_daily_bars,
    consolidate,
    deduplicate,
    run_aggregation,
)
from smida_tape.config import Settings
from smida_tape.parquet_io import normalize_raw_frame, write_dataset


def trade(
    isin: str,
    day: str,
    price: float,
    quantity: float,
    exchange: str,
    report_id: str,
    repo_part: str = "-",
    issuer: str = "Міністерство фінансів України",
    edrpou: str = "00013480",
    instrument: str = "ОБЛІГАЦІЯ",
) -> dict:
    return {
        "trade_date": datetime.fromisoformat(day),
        "repo_part": repo_part,
        "issuer_name": issuer,
        "issuer_edrpou": edrpou,
        "instrument_type": instrument,
        "isin": isin,
        "quantity": quantity,
        "price": price,
        "exchange_code": exchange,
        "report_id": report_id,
    }


@pytest.fixture()
def trades() -> pd.DataFrame:
    rows = [
        trade("UA4000000001", "2025-01-02", 100.0, 10, "PFTS", "r1"),
        trade("UA4000000001", "2025-01-02", 102.0, 5, "PFTS", "r1"),
        trade("UA4000000001", "2025-01-02", 101.0, 20, "UX", "r2"),
        trade("UA4000000001", "2025-01-03", 103.0, 5, "PFTS", "r3"),
        trade("UA4000000001", "2025-01-03", 104.0, 2, "UX", "r4"),
        trade("UA4000000002", "2025-01-02", 50.0, 7, "PERSPEKTIVA", "r5"),
        # REPO leg must stay out of the spot bars
        trade("UA4000000001", "2025-01-02", 1000.0, 1, "PFTS", "r6", repo_part="1"),
    ]
    return pd.DataFrame(rows)


def test_deduplicate_drops_exact_source_duplicates(trades: pd.DataFrame) -> None:
    stats = AggregationStats()
    frame = pd.concat([trades, trades.iloc[[0]]], ignore_index=True)
    result = deduplicate(frame, stats)
    assert stats.duplicates_removed == 1
    assert len(result) == len(trades)


def test_build_daily_bars_ohlcv(trades: pd.DataFrame) -> None:
    spot = trades[trades["repo_part"] == "-"]
    bars = build_daily_bars(spot)

    pfts = bars[
        (bars["exchange_code"] == "PFTS")
        & (bars["instrument_key"] == "UA4000000001")
        & (bars["trade_date"] == pd.Timestamp("2025-01-02"))
    ].iloc[0]
    assert pfts["open"] == pytest.approx(100.0)
    assert pfts["high"] == pytest.approx(102.0)
    assert pfts["low"] == pytest.approx(100.0)
    assert pfts["close"] == pytest.approx(102.0)
    assert pfts["volume_units"] == pytest.approx(15.0)
    assert pfts["turnover_uah"] == pytest.approx(100 * 10 + 102 * 5)
    assert pfts["vwap"] == pytest.approx((100 * 10 + 102 * 5) / 15)
    assert pfts["trades_count"] == 2

    assert len(bars) == 5  # 3 exchange-days for ISIN1 + 1 for ISIN2 + 1 next day


def test_consolidation_model(trades: pd.DataFrame) -> None:
    bars = build_daily_bars(trades[trades["repo_part"] == "-"])
    cons = add_log_returns(consolidate(bars))

    day1 = cons[(cons["instrument_key"] == "UA4000000001")
                & (cons["trade_date"] == pd.Timestamp("2025-01-02"))].iloc[0]
    # UX has the larger volume (20 > 15)
    assert day1["volume_cons"] == pytest.approx(35.0)
    assert day1["price_cons"] == pytest.approx(101.0)
    assert day1["price_source"] == "UX"
    assert day1["volume_source"] == "PFTS+UX"
    assert day1["turnover_cons"] == pytest.approx(1510 + 2020)
    assert math.isnan(day1["log_return"])

    day2 = cons[(cons["instrument_key"] == "UA4000000001")
                & (cons["trade_date"] == pd.Timestamp("2025-01-03"))].iloc[0]
    # PFTS has the larger volume (5 > 2)
    assert day2["price_cons"] == pytest.approx(103.0)
    assert day2["volume_cons"] == pytest.approx(7.0)
    assert day2["price_source"] == "PFTS"
    assert day2["log_return"] == pytest.approx(math.log(103 / 101))

    only_persp = cons[cons["instrument_key"] == "UA4000000002"].iloc[0]
    assert only_persp["price_source"] == "PERSPEKTIVA"
    assert only_persp["volume_cons"] == pytest.approx(7.0)
    assert only_persp["price_cons"] == pytest.approx(50.0)


def test_tie_goes_to_pfts() -> None:
    frame = pd.DataFrame(
        [
            trade("UA4000000003", "2025-01-02", 100.0, 5, "PFTS", "r1"),
            trade("UA4000000003", "2025-01-02", 200.0, 5, "UX", "r2"),
        ]
    )
    cons = consolidate(build_daily_bars(frame))
    assert cons.iloc[0]["price_source"] == "PFTS"
    assert cons.iloc[0]["price_cons"] == pytest.approx(100.0)


def test_zero_pfts_and_ux_falls_back_to_perspektiva() -> None:
    frame = pd.DataFrame([trade("UA4000000004", "2025-01-02", 77.0, 3, "PERSPEKTIVA", "r1")])
    cons = consolidate(build_daily_bars(frame))
    row = cons.iloc[0]
    assert row["volume_pfts"] == 0
    assert row["volume_ux"] == 0
    assert row["price_source"] == "PERSPEKTIVA"
    assert row["price_cons"] == pytest.approx(77.0)


def test_log_return_chains_over_gaps() -> None:
    frame = pd.DataFrame(
        [
            trade("UA4000000005", "2025-01-02", 100.0, 1, "PFTS", "r1"),
            trade("UA4000000005", "2025-01-06", 110.0, 1, "PFTS", "r2"),
            trade("UA4000000005", "2025-01-07", 99.0, 1, "PFTS", "r3"),
        ]
    )
    cons = add_log_returns(consolidate(build_daily_bars(frame)))
    # only PFTS traded, so the volume source must not claim PFTS+UX
    assert set(cons["volume_source"]) == {"PFTS"}
    returns = cons["log_return"].tolist()
    assert math.isnan(returns[0])
    assert returns[1] == pytest.approx(math.log(110 / 100))
    assert returns[2] == pytest.approx(math.log(99 / 110))


def test_empty_inputs() -> None:
    empty = pd.DataFrame(columns=["trade_date", "repo_part", "issuer_name", "issuer_edrpou",
                                  "instrument_type", "isin", "quantity", "price",
                                  "exchange_code", "report_id"])
    assert build_daily_bars(empty).empty
    assert consolidate(build_daily_bars(empty)).empty
    assert add_log_returns(consolidate(build_daily_bars(empty))).empty


def test_run_aggregation_end_to_end(trades: pd.DataFrame, tmp_path: Path) -> None:
    settings = Settings.from_env(home=tmp_path)
    settings.ensure_dirs()
    write_dataset(normalize_raw_frame(trades), settings.raw_path)

    stats, path = run_aggregation(settings, year_from=2025, year_to=2025)

    assert path.exists()
    out = pd.read_parquet(path)
    assert {"price_cons", "volume_cons", "log_return", "price_source"} <= set(out.columns)
    assert len(out) == 3  # ISIN1 x2 days + ISIN2 x1 day
    assert stats.consolidated_rows == 3
    assert stats.repo_rows == 1
    assert stats.spot_rows == 6
    assert (settings.output_dir / "aggregation_report.json").exists()
    assert (settings.output_dir / "daily_bars.parquet").exists()
    assert (settings.output_dir / "repo_tape_2025_2025.parquet").exists()
