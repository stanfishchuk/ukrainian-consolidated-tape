"""UCT Terminal — Streamlit dashboard for the Ukrainian Consolidated Tape.

Reads the artefacts produced by ``aggregate``:

* ``output/daily_bars.parquet``                      — per-venue OHLCV bars
* ``output/consolidated_tape_<from>_<to>.parquet``   — consolidated tape (model output)

The layout follows a market-terminal convention: asset class → instrument →
market, a strip of key metrics above the chart, and a candle/volume chart with
non-trading days collapsed so the price action is continuous.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

HOME = Path(os.environ.get("SMIDA_HOME", Path(__file__).parent)).resolve()
OUTPUT_DIR = Path(os.environ.get("SMIDA_OUTPUT_DIR", HOME / "output"))

CONSOLIDATED = "CONSOLIDATED"

# Terminal palette
UP = "#00c853"
DOWN = "#ff1744"
VWAP_COLOR = "#ffb300"
GRID = "#1f2733"
MUTED = "#8b95a5"


# --------------------------------------------------------------------------- #
# Asset classes
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AssetClass:
    key: str
    label: str


CLASS_BASKET = "basket"
CLASS_SOVEREIGN = "sovereign"
CLASS_CORPORATE = "corporate"
CLASS_CERTIFICATE = "certificate"

ASSET_CLASSES: tuple[AssetClass, ...] = (
    AssetClass(CLASS_BASKET, "Акції (Індексний кошик)"),
    AssetClass(CLASS_SOVEREIGN, "Державні облігації (ОВДП)"),
    AssetClass(CLASS_CORPORATE, "Корпоративні облігації"),
    AssetClass(CLASS_CERTIFICATE, "Інвестиційні сертифікати"),
)

#: Pinned blue-chip basket, in this exact order. Ticker → issuer pattern; the
#: ISIN is resolved from the dataset because SMIDA discloses names, not tickers.
INDEX_BASKET: tuple[tuple[str, str, str], ...] = (
    ("BAVL", "Райффайзен Банк Аваль", r"РАЙФФАЙЗЕН"),
    ("CEEN", "Центренерго", r"ЦЕНТРЕНЕРГО"),
    ("DOEN", "Донбасенерго", r"ДОНБАСЕНЕРГО"),
    ("KVBZ", "Крюківський вагонобудівний завод", r"КРЮКІВ"),
    ("TATM", "Татнафта", r"ТАТНАФТА"),
    ("UTLM", "Укртелеком", r"УКРТЕЛЕКОМ"),
)


def classify(instrument_type: str, issuer_name: str) -> str:
    """Map a disclosed instrument description onto one of the four classes."""
    kind = (instrument_type or "").lower()
    issuer = (issuer_name or "").lower()
    if "акція" in kind:
        return CLASS_BASKET
    if "сертифікат" in kind or "спільного інвестування" in kind:
        return CLASS_CERTIFICATE
    if "державних позик" in kind or "міністерство фінансів" in issuer:
        return CLASS_SOVEREIGN
    if "облігація" in kind:
        return CLASS_CORPORATE
    return "other"


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def fmt_price(value: object) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):,.2f}".replace(",", "\u2009")


def fmt_int(value: object) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):,.0f}".replace(",", "\u2009")


def fmt_money(value: object) -> str:
    """Turnover in UAH, scaled to млн/тис for readability."""
    if value is None or pd.isna(value):
        return "—"
    amount = float(value)
    if abs(amount) >= 1_000_000:
        return f"{amount / 1_000_000:,.2f} млн ₴".replace(",", "\u2009")
    if abs(amount) >= 1_000:
        return f"{amount / 1_000:,.1f} тис ₴".replace(",", "\u2009")
    return f"{amount:,.0f} ₴".replace(",", "\u2009")


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False)
def load_bars(path: str, mtime: float) -> pd.DataFrame:
    """``mtime`` is unused by the body — it only busts the Streamlit cache."""
    frame = pd.read_parquet(path)
    if frame.empty:
        return frame
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    for column in ("isin", "issuer_name", "instrument_type", "exchange_code", "instrument_key"):
        if column in frame.columns:
            frame[column] = frame[column].astype(str)
    return frame


@st.cache_data(show_spinner=False)
def load_consolidated(path: str, mtime: float) -> pd.DataFrame:
    """Same cache-busting contract as :func:`load_bars`."""
    frame = pd.read_parquet(path)
    if frame.empty:
        return frame
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    frame["isin"] = frame["isin"].astype(str)
    return frame


def _latest(pattern: str) -> Path | None:
    candidates = sorted(glob.glob(str(OUTPUT_DIR / pattern)))
    return Path(candidates[-1]) if candidates else None


def load_all() -> tuple[pd.DataFrame, pd.DataFrame | None]:
    bars_path = OUTPUT_DIR / "daily_bars.parquet"
    if not bars_path.exists():
        return pd.DataFrame(), None
    bars = load_bars(str(bars_path), bars_path.stat().st_mtime)
    cons_path = _latest("consolidated_tape_*.parquet")
    cons = (
        load_consolidated(str(cons_path), cons_path.stat().st_mtime)
        if cons_path is not None
        else None
    )
    return bars, cons


@st.cache_data(show_spinner=False)
def build_catalog(bars_path: str, mtime: float) -> pd.DataFrame:
    """One row per instrument with class, lifecycle and total turnover."""
    bars = load_bars(bars_path, mtime)
    if bars.empty:
        return bars

    bars = bars.assign(turnover=bars["turnover_uah"].fillna(0.0))
    catalog = (
        bars.groupby("instrument_key", as_index=False)
        .agg(
            isin=("isin", "last"),
            issuer_name=("issuer_name", "last"),
            instrument_type=("instrument_type", "last"),
            observations=("trade_date", "nunique"),
            turnover=("turnover", "sum"),
            first_date=("trade_date", "min"),
            last_date=("trade_date", "max"),
        )
        .sort_values("turnover", ascending=False)
    )
    catalog["asset_class"] = [
        classify(kind, name)
        for kind, name in zip(catalog["instrument_type"], catalog["issuer_name"], strict=False)
    ]
    return catalog.reset_index(drop=True)


def basket_members(catalog: pd.DataFrame) -> pd.DataFrame:
    """Resolve the pinned tickers to catalogue rows, preserving basket order."""
    equities = catalog[catalog["asset_class"] == CLASS_BASKET]
    picked: list[dict] = []
    for ticker, label, pattern in INDEX_BASKET:
        match = equities[equities["issuer_name"].str.upper().str.contains(pattern, na=False, regex=True)]
        if match.empty:
            continue  # not disclosed in this tape (e.g. TATM)
        row = match.sort_values("observations", ascending=False).iloc[0]
        picked.append(
            {
                "instrument_key": row["instrument_key"],
                "ticker": ticker,
                "label": f"{ticker} · {label}",
                "turnover": row["turnover"],
            }
        )
    return pd.DataFrame(picked)


# --------------------------------------------------------------------------- #
# Frames
# --------------------------------------------------------------------------- #
def consolidated_frame(bars: pd.DataFrame, cons: pd.DataFrame, key: str) -> pd.DataFrame:
    """Candles of the venue that actually priced each consolidated day."""
    model = cons[cons["instrument_key"] == key][
        ["trade_date", "price_source", "price_cons", "volume_cons", "turnover_cons", "log_return"]
    ]
    if model.empty:
        return model

    merged = model.merge(
        bars[bars["instrument_key"] == key],
        left_on=["trade_date", "price_source"],
        right_on=["trade_date", "exchange_code"],
        how="left",
    )
    for column in ("open", "high", "low", "close"):
        if column not in merged.columns:
            merged[column] = merged["price_cons"]
        merged[column] = merged[column].fillna(merged["price_cons"])

    merged["volume_units"] = merged["volume_cons"].fillna(merged.get("volume_units"))
    merged["turnover_uah"] = merged["turnover_cons"].fillna(merged.get("turnover_uah"))
    merged["trades_count"] = merged["trades_count"].fillna(0)
    # Consolidated VWAP must match the consolidated aggregates, not one venue's.
    merged["vwap"] = (
        merged["turnover_uah"]
        .where(merged["volume_units"] > 0)
        .div(merged["volume_units"].where(merged["volume_units"] > 0))
    )
    return merged.sort_values("trade_date").reset_index(drop=True)


def venue_frame(bars: pd.DataFrame, key: str, venue: str) -> pd.DataFrame:
    subset = bars[(bars["instrument_key"] == key) & (bars["exchange_code"] == venue)]
    return subset.sort_values("trade_date").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Chart
# --------------------------------------------------------------------------- #
def trading_breaks(dates: pd.Series) -> list[dict]:
    """Collapse weekends and any calendar gap so candles sit side by side."""
    present = pd.DatetimeIndex(pd.to_datetime(dates).dt.normalize().unique()).sort_values()
    if len(present) < 2:
        return []
    full = pd.date_range(present.min(), present.max(), freq="D")
    missing = full.difference(present)
    missing = missing[missing.dayofweek < 5]  # weekends are already covered
    breaks: list[dict] = [{"bounds": ["sat", "mon"]}]
    if len(missing):
        breaks.append({"values": [day.strftime("%Y-%m-%d") for day in missing]})
    return breaks


def terminal_chart(frame: pd.DataFrame, title: str, subtitle: str) -> go.Figure:
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.74, 0.26],
    )

    fig.add_trace(
        go.Candlestick(
            x=frame["trade_date"],
            open=frame["open"],
            high=frame["high"],
            low=frame["low"],
            close=frame["close"],
            name="OHLC",
            increasing_line_color=UP,
            increasing_fillcolor=UP,
            decreasing_line_color=DOWN,
            decreasing_fillcolor=DOWN,
            whiskerwidth=0.35,
        ),
        row=1,
        col=1,
    )

    if "vwap" in frame.columns and frame["vwap"].notna().any():
        fig.add_trace(
            go.Scatter(
                x=frame["trade_date"],
                y=frame["vwap"],
                mode="lines",
                name="VWAP",
                line={"color": VWAP_COLOR, "width": 1.6},
                hovertemplate="VWAP %{y:,.2f}<extra></extra>",
            ),
            row=1,
            col=1,
        )

    rising = frame["close"] >= frame["open"]
    fig.add_trace(
        go.Bar(
            x=frame["trade_date"],
            y=frame["volume_units"].fillna(0),
            name="Обсяг",
            marker_color=[UP if flag else DOWN for flag in rising],
            marker_line_width=0,
            opacity=0.85,
            hovertemplate="Обсяг %{y:,.0f} шт<extra></extra>",
        ),
        row=2,
        col=1,
    )

    breaks = trading_breaks(frame["trade_date"])
    fig.update_xaxes(rangebreaks=breaks, gridcolor=GRID, showgrid=False)
    fig.update_yaxes(
        gridcolor=GRID,
        zeroline=False,
        tickfont={"color": MUTED, "size": 11},
        title_font={"color": MUTED, "size": 11},
    )
    fig.update_yaxes(title_text="Ціна, ₴", row=1, col=1)
    fig.update_yaxes(title_text="Обсяг, шт", row=2, col=1)

    fig.update_layout(
        title={
            "text": (
                f"<span style='font-size:15px;color:#e6e6e6'>{title}</span>"
                f"<br><span style='font-size:11px;color:{MUTED}'>{subtitle}</span>"
            ),
            "x": 0.01,
            "xanchor": "left",
        },
        height=680,
        margin={"l": 8, "r": 8, "t": 62, "b": 8},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": "ui-monospace, SFMono-Regular, Menlo, monospace", "color": "#e6e6e6"},
        hovermode="x unified",
        hoverlabel={"bgcolor": "#121722", "bordercolor": GRID, "font": {"color": "#e6e6e6"}},
        showlegend=True,
        legend={
            "orientation": "h",
            "y": 1.0,
            "x": 1,
            "xanchor": "right",
            "bgcolor": "rgba(0,0,0,0)",
            "font": {"size": 11, "color": MUTED},
        },
        xaxis_rangeslider_visible=False,
        bargap=0.25,
    )
    return fig


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
def pick_instrument(
    catalog: pd.DataFrame,
) -> tuple[str, str, str, str, pd.Timestamp, pd.Timestamp]:
    """Return ``(instrument_key, label, asset_class, mode, start, end)``."""
    with st.sidebar:
        st.markdown("### Інструмент")
        class_label = st.radio(
            "Клас активів",
            [item.label for item in ASSET_CLASSES],
            label_visibility="collapsed",
        )
        class_key = next(item.key for item in ASSET_CLASSES if item.label == class_label)

        if class_key == CLASS_BASKET:
            basket = basket_members(catalog)
            if basket.empty:
                st.warning("Індексний кошик відсутній у датасеті")
                st.stop()
            options = basket["instrument_key"].tolist()
            labels = dict(zip(basket["instrument_key"], basket["label"], strict=False))
            key = st.selectbox(
                "ISIN",
                options,
                format_func=lambda item: labels.get(item, item),
            )
            label = labels[key]
            present = {row["ticker"] for _, row in basket.iterrows()}
            missing = [ticker for ticker, _, _ in INDEX_BASKET if ticker not in present]
            if missing:
                st.caption(f"Немає в датасеті SMIDA: {', '.join(missing)}")
        else:
            pool = catalog[catalog["asset_class"] == class_key].sort_values(
                "turnover", ascending=False
            )
            if pool.empty:
                st.warning("У цьому класі немає інструментів")
                st.stop()
            labels = {
                row.instrument_key: (
                    f"{row.isin} · {row.issuer_name[:42]} · {row.last_date.year}"
                )
                for row in pool.itertuples()
            }
            key = st.selectbox(
                "ISIN",
                pool["instrument_key"].tolist(),
                format_func=lambda item: labels.get(item, item),
            )
            label = labels[key]

        st.markdown("---")
        st.markdown("### Період")
        venue_bars = catalog.loc[catalog["instrument_key"] == key]
        date_min = pd.Timestamp(venue_bars["first_date"].iloc[0]).date()
        date_max = pd.Timestamp(venue_bars["last_date"].iloc[0]).date()
        picked = st.date_input(
            "Діапазон",
            value=(date_min, date_max),
            min_value=date_min,
            max_value=date_max,
            label_visibility="collapsed",
        )

        st.markdown("---")
        st.markdown("### Майданчик")
        modes = [CONSOLIDATED, "PFTS", "PERSPEKTIVA", "UX"]
        mode = st.selectbox(
            "Джерело ціни",
            modes,
            index=0,
            help="CONSOLIDATED — ціна біржі, що визначила консолідовану ціну дня",
        )

        if isinstance(picked, (list, tuple)):
            start, end = pd.Timestamp(picked[0]), pd.Timestamp(picked[-1])
        else:
            start = end = pd.Timestamp(picked)

    return key, label, class_key, mode, start, end


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def metric_strip(frame: pd.DataFrame) -> None:
    last = frame.iloc[-1]
    previous_close = frame["close"].iloc[-2] if len(frame) > 1 else None

    change = None
    if previous_close and not pd.isna(previous_close) and previous_close > 0:
        change = (last["close"] / previous_close - 1.0) * 100.0

    columns = st.columns(5, gap="small")
    columns[0].metric(
        "Close",
        fmt_price(last["close"]),
        delta=None if change is None else f"{change:+.2f}%",
    )
    columns[1].metric("VWAP", fmt_price(last.get("vwap")))
    columns[2].metric("Volume", f"{fmt_int(last.get('volume_units'))} шт")
    columns[3].metric("Turnover", fmt_money(last.get("turnover_uah")))
    columns[4].metric("Trades", fmt_int(last.get("trades_count")))


def data_table(frame: pd.DataFrame) -> pd.DataFrame:
    """Presentation frame: 2 decimals for prices, grouped integers for size."""
    table = pd.DataFrame(
        {
            "Дата": frame["trade_date"].dt.strftime("%Y-%m-%d"),
            "Open": frame["open"].map(fmt_price),
            "High": frame["high"].map(fmt_price),
            "Low": frame["low"].map(fmt_price),
            "Close": frame["close"].map(fmt_price),
            "VWAP": frame.get("vwap", pd.Series(index=frame.index, dtype="float64")).map(fmt_price),
            "Обсяг, шт": frame.get("volume_units", pd.Series(index=frame.index)).map(fmt_int),
            "Оборот, ₴": frame.get("turnover_uah", pd.Series(index=frame.index)).map(fmt_money),
            "Угод": frame.get("trades_count", pd.Series(index=frame.index)).map(fmt_int),
        }
    )
    if "log_return" in frame.columns:
        table["ln R"] = frame["log_return"].map(
            lambda value: "—" if pd.isna(value) else f"{value:+.4f}"
        )
    if "price_source" in frame.columns:
        table["Ціна з"] = frame["price_source"]
    return table.iloc[::-1].reset_index(drop=True)


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
def main() -> None:
    st.set_page_config(page_title="UCT Terminal", page_icon="📈", layout="wide")

    # Terminal chrome: hide Streamlit's deploy/menu toolbar and the coloured
    # top decoration so the viewport is only data.
    st.markdown(
        """
        <style>
          [data-testid="stToolbar"] {display: none;}
          [data-testid="stDecoration"] {display: none;}
          [data-testid="stHeaderActionElements"] {display: none;}
          header[data-testid="stHeader"] {background: transparent; height: 0;}
          .block-container {padding-top: 1.6rem; padding-bottom: 1rem;}
          [data-testid="stMetricValue"] {font-size: 1.45rem;}
          [data-testid="stMetricLabel"] {font-size: 0.78rem; letter-spacing: .04em;}
          [data-testid="stSidebar"] {border-right: 1px solid #1f2733;}
        </style>
        """,
        unsafe_allow_html=True,
    )

    bars, cons = load_all()
    if bars.empty:
        st.error(
            "Датасет не знайдено. Згенеруйте його командою "
            "`python -m smida_tape aggregate`."
        )
        st.stop()

    bars_path = OUTPUT_DIR / "daily_bars.parquet"
    catalog = build_catalog(str(bars_path), bars_path.stat().st_mtime)
    key, label, class_key, mode, start, end = pick_instrument(catalog)

    if mode == CONSOLIDATED:
        if cons is None:
            st.error("Немає консолідованої стрічки — виконайте `aggregate`.")
            st.stop()
        frame = consolidated_frame(bars, cons, key)
    else:
        frame = venue_frame(bars, key, mode)

    frame = frame[(frame["trade_date"] >= start) & (frame["trade_date"] <= end)]
    if frame.empty:
        st.warning("У вибраному періоді немає спостережень")
        st.stop()

    meta = catalog[catalog["instrument_key"] == key].iloc[0]
    class_label = next(item.label for item in ASSET_CLASSES if item.key == class_key)

    head_left, head_right = st.columns([0.72, 0.28])
    with head_left:
        st.markdown(f"#### {label}")
        st.caption(
            f"{meta['isin']} · {meta['instrument_type']} · "
            f"{frame['trade_date'].min():%d.%m.%Y} – {frame['trade_date'].max():%d.%m.%Y}"
        )
    with head_right:
        st.markdown(
            f"<div style='text-align:right;color:{MUTED};font-size:12px;padding-top:14px'>"
            f"{class_label}<br>"
            f"<span style='color:#e6e6e6'>{mode}</span> · SMIDA 2019–2025</div>",
            unsafe_allow_html=True,
        )

    metric_strip(frame)

    chart_tab, data_tab = st.tabs(["Графік", "Дані"])

    with chart_tab:
        subtitle = f"{mode} · {len(frame)} спостережень"
        if mode == CONSOLIDATED and "price_source" in frame.columns:
            shares = frame["price_source"].value_counts(normalize=True)
            subtitle += " · ціна: " + ", ".join(
                f"{venue} {share:.0%}" for venue, share in shares.head(3).items()
            )
        st.plotly_chart(
            terminal_chart(frame, f"{meta['isin']} · {mode}", subtitle),
            use_container_width=True,
        )

    with data_tab:
        st.dataframe(
            data_table(frame),
            use_container_width=True,
            hide_index=True,
            height=520,
        )
        st.download_button(
            "Завантажити CSV",
            data=frame.to_csv(index=False).encode("utf-8"),
            file_name=f"{meta['isin']}_{mode}.csv",
            mime="text/csv",
        )


if __name__ == "__main__":
    main()
