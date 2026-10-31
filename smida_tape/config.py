"""Configuration: target exchanges, periods, paths and runtime settings.

All values can be overridden with environment variables (12-factor style),
which is what docker-compose.yml does.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

BASE_URL = "https://smida.gov.ua"
FEED_URL = f"{BASE_URL}/db/feed"
REPORT_FORM_URL = BASE_URL + "/db/feed/showform/discl/{report_id}"
REPORT_CARD_URL = BASE_URL + "/db/feed/{report_id}"

#: SMIDA report kind that contains the exchange daily trade tape.
REPORT_KIND = "DisclExch"

#: Hard research window requested for the dissertation.
WINDOW_START = date(2015, 1, 1)
WINDOW_END = date(2025, 12, 31)


@dataclass(frozen=True)
class Exchange:
    """Description of one stock exchange as it is represented in SMIDA.

    ``edrpou`` holds every EDRPOU code under which the exchange disclosed
    reports.  SMIDA migrated some entities between registry codes, therefore
    the crawler accepts several codes per exchange and maps them all to the
    same ``code``.
    """

    code: str
    display_name: str
    edrpou: tuple[str, ...]
    search_names: tuple[str, ...]
    period_start: date
    period_end: date


EXCHANGES: dict[str, Exchange] = {
    "PFTS": Exchange(
        code="PFTS",
        display_name='АТ "ФОНДОВА БІРЖА ПФТС"',
        edrpou=("21672206",),
        search_names=("ПФТС",),
        period_start=WINDOW_START,
        period_end=WINDOW_END,
    ),
    "PERSPEKTIVA": Exchange(
        code="PERSPEKTIVA",
        display_name='ПрАТ "ФБ "Перспектива"',
        edrpou=("33718227",),
        search_names=("Перспектива",),
        period_start=WINDOW_START,
        period_end=WINDOW_END,
    ),
    "UX": Exchange(
        code="UX",
        # В ЄДРПОУ самої "Української біржі" в базі SMIDA — 36184092.
        # 34914352 залишено як alias: він згадується в постановці задачі,
        # і частина звітів могла публікуватися під ним.
        edrpou=("36184092", "34914352"),
        display_name='АТ "Українська біржа"',
        search_names=("Українська біржа",),
        period_start=WINDOW_START,
        period_end=date(2024, 12, 31),
    ),
}

EDRPOU_TO_EXCHANGE: dict[str, str] = {
    edrpou: exch.code for exch in EXCHANGES.values() for edrpou in exch.edrpou
}

EXCHANGE_CODES: tuple[str, ...] = tuple(EXCHANGES)

#: Order used by the consolidation model (PFTS and UX are the pricing pair).
PRICING_EXCHANGES: tuple[str, str] = ("PFTS", "UX")


@dataclass(frozen=True)
class Settings:
    home: Path
    storage_dir: Path
    output_dir: Path
    db_path: Path
    raw_path: Path
    max_rps: float = 2.0
    timeout: float = 60.0
    retries: int = 5
    #: Consecutive failures after which the whole session pauses once.
    slow_after: int = 5
    #: Upper bound of that pause, seconds.
    cooldown: float = 60.0
    #: Reports parsed between dataset checkpoints (0 = only at the end).
    checkpoint_every: int = 250
    user_agent: str = "smida-tape/1.0 (academic research pipeline; python-httpx)"

    @classmethod
    def from_env(
        cls,
        home: str | Path | None = None,
        max_rps: float | None = None,
    ) -> Settings:
        base = Path(home or os.environ.get("SMIDA_HOME", ".")).expanduser().resolve()
        storage = Path(os.environ.get("SMIDA_STORAGE_DIR", base / "storage"))
        output = Path(os.environ.get("SMIDA_OUTPUT_DIR", base / "output"))
        rps = max_rps if max_rps is not None else float(os.environ.get("SMIDA_MAX_RPS", "2"))
        # SMIDA is a public disclosure registry: never exceed 5 req/s, even if
        # the caller asks for more.
        rps = max(0.2, min(float(rps), 5.0))
        return cls(
            home=base,
            storage_dir=storage,
            output_dir=output,
            db_path=Path(os.environ.get("SMIDA_DB_PATH", storage / "metadata.db")),
            raw_path=Path(os.environ.get("SMIDA_RAW_PATH", storage / "raw_trades.parquet")),
            max_rps=rps,
            timeout=float(os.environ.get("SMIDA_TIMEOUT", "60")),
            retries=int(os.environ.get("SMIDA_RETRIES", "5")),
            slow_after=int(os.environ.get("SMIDA_SLOW_AFTER", "5")),
            cooldown=float(os.environ.get("SMIDA_COOLDOWN", "60")),
            checkpoint_every=int(os.environ.get("SMIDA_CHECKPOINT_EVERY", "250")),
            user_agent=os.environ.get(
                "SMIDA_USER_AGENT",
                "smida-tape/1.0 (academic research pipeline; python-httpx)",
            ),
        )

    def ensure_dirs(self) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)


def exchange_for_edrpou(edrpou: str | None) -> str | None:
    if not edrpou:
        return None
    return EDRPOU_TO_EXCHANGE.get(edrpou.strip())
