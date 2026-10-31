"""Parquet persistence helpers (schema-enforced, atomic dataset rewrites)."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterable, Sequence
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger(__name__)

RAW_COLUMNS: tuple[str, ...] = (
    "trade_date",
    "repo_part",
    "issuer_name",
    "issuer_edrpou",
    "instrument_type",
    "isin",
    "quantity",
    "price",
    "exchange_code",
    "report_id",
)

RAW_SCHEMA = pa.schema(
    [
        ("trade_date", pa.timestamp("ns")),
        ("repo_part", pa.string()),
        ("issuer_name", pa.string()),
        ("issuer_edrpou", pa.string()),
        ("instrument_type", pa.string()),
        ("isin", pa.string()),
        ("quantity", pa.float64()),
        ("price", pa.float64()),
        ("exchange_code", pa.string()),
        ("report_id", pa.string()),
        ("year", pa.int32()),
    ]
)


def empty_raw_frame() -> pd.DataFrame:
    frame = pd.DataFrame({column: pd.Series(dtype="object") for column in RAW_COLUMNS})
    frame["trade_date"] = pd.Series(dtype="datetime64[ns]")
    frame["quantity"] = pd.Series(dtype="float64")
    frame["price"] = pd.Series(dtype="float64")
    frame["year"] = pd.Series(dtype="int32")
    return frame


def normalize_raw_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Cast to the canonical dtypes and add the ``year`` partition column."""
    if frame.empty:
        return empty_raw_frame()

    frame = frame.copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").astype("datetime64[ns]")
    for column in ("repo_part", "issuer_name", "issuer_edrpou", "instrument_type",
                   "isin", "exchange_code", "report_id"):
        frame[column] = frame[column].astype("string").fillna("")
    frame["quantity"] = pd.to_numeric(frame["quantity"], errors="coerce").astype("float64")
    frame["price"] = pd.to_numeric(frame["price"], errors="coerce").astype("float64")
    frame["year"] = frame["trade_date"].dt.year.fillna(0).astype("int32")
    return frame[list(RAW_COLUMNS) + ["year"]]


def read_raw_dataset(
    path: Path,
    *,
    years: Sequence[int] | None = None,
    exchanges: Sequence[str] | None = None,
) -> pd.DataFrame:
    if not Path(path).exists():
        return empty_raw_frame()

    filters = []
    if years:
        filters.append(("year", ">=", int(min(years))))
        filters.append(("year", "<=", int(max(years))))
    if exchanges:
        filters.append(("exchange_code", "in", list(exchanges)))
    try:
        table = pq.read_table(str(path), filters=filters or None)
    except (FileNotFoundError, pa.ArrowInvalid, OSError) as exc:
        log.warning("cannot read %s: %s", path, exc)
        return empty_raw_frame()
    if table.num_rows == 0:
        return empty_raw_frame()
    frame = table.to_pandas()
    frame["exchange_code"] = frame["exchange_code"].astype("string")
    return frame


def write_dataset(
    frame: pd.DataFrame,
    path: Path,
    *,
    partition_cols: Iterable[str] = ("year", "exchange_code"),
    schema: pa.Schema = RAW_SCHEMA,
) -> int:
    """Rewrite a partitioned dataset atomically (write temp dir, swap, delete old)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    old = path.parent / f".{path.name}.old"
    for stale in (tmp, old):
        if stale.exists():
            shutil.rmtree(stale)

    table = pa.Table.from_pandas(frame, schema=schema, preserve_index=False)
    pq.write_to_dataset(
        table,
        root_path=str(tmp),
        partition_cols=list(partition_cols),
        compression="zstd",
        compression_level=7,
        use_dictionary=[
            "repo_part",
            "issuer_edrpou",
            "instrument_type",
            "isin",
            "exchange_code",
            "report_id",
        ],
        row_group_size=200_000,
        version="2.6",
    )
    if path.exists():
        path.replace(old)
    tmp.replace(path)
    if old.exists():
        shutil.rmtree(old)
    return table.num_rows


def merge_raw_dataset(new_rows: pd.DataFrame, path: Path) -> tuple[pd.DataFrame, int]:
    """Replace all rows of the reports present in ``new_rows`` and rewrite."""
    new_rows = normalize_raw_frame(new_rows)
    if new_rows.empty:
        return empty_raw_frame(), 0

    existing = read_raw_dataset(path)
    replaced_ids = set(new_rows["report_id"].unique())
    if not existing.empty:
        existing = existing[~existing["report_id"].isin(replaced_ids)]

    merged = pd.concat([existing, new_rows], ignore_index=True) if not existing.empty else new_rows
    merged = merged.sort_values(["trade_date", "exchange_code", "report_id"], kind="stable").reset_index(drop=True)
    written = write_dataset(merged, path)
    return merged, written
