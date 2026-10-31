"""HTML parsing for SMIDA search results and exchange disclosure forms.

The module is pure (no I/O), which makes it trivially unit-testable against
saved fixtures.  Selectors are pinned to the real SMIDA markup:

* search results  -> ``table.xsTableReport`` where a row links to ``/db/feed/<id>``
* report meta     -> ``table.xsTableReportLine`` (EDRPOU / name / "as of" date)
* trade table     -> ``table.xsTableReport`` with the 8-column contract header

Both the 2019 and the 2025 revisions of the form use the same structure, which
was verified against live reports from all three exchanges.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

try:  # selectolax >= 1.0 ships the lexbor backend under its own import path
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
except ImportError:  # pragma: no cover - selectolax < 1.0 (deprecated modest backend)
    from selectolax.parser import HTMLParser

SMIDA_BASE_URL = "https://smida.gov.ua"

_WS_RE = re.compile(r"\s+")
_PAGE_RE = re.compile(r"[?&](?:amp;)?page=(\d+)")
_REPORT_ID_RE = re.compile(r"/db/feed/(\d+)")
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_DATE_FORMATS = (
    "%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y",
    "%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
)


def clean_text(value: str | None) -> str:
    """Collapse whitespace and strip NBSPs / zero-width characters."""
    if not value:
        return ""
    value = value.replace("\u00a0", " ").replace("\u202f", " ").replace("\u200b", "")
    return _WS_RE.sub(" ", value).strip()


def parse_number(raw: str | float | None) -> float | None:
    """Convert a SMIDA numeric cell to ``float``.

    Handles the separators that actually occur in the registry:
    ``"1 059,910000"``, ``"24078.790000"``, ``"2823"``, ``"-"`` (empty).
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)

    text = clean_text(raw).replace(" ", "").replace("\u00a0", "")
    if text in {"", "-", "--", "—", "–", "n/a", "null", "None"}:
        return None

    text = text.replace("'", "").replace("`", "")
    has_comma, has_dot = "," in text, "." in text
    if has_comma and has_dot:
        # The right-most separator is the decimal one.
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif has_comma:
        text = text.replace(",", ".")

    text = re.sub(r"[^0-9eE+\-.]", "", text)
    if text.count(".") > 1:  # 1.234.567 -> 1234567
        head, _, tail = text.rpartition(".")
        text = head.replace(".", "") + "." + tail
    try:
        return float(text)
    except ValueError:
        return None


def parse_date(raw: str | None) -> date | None:
    text = clean_text(raw)
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_datetime(raw: str | None) -> datetime | None:
    text = clean_text(raw)
    if not text:
        return None
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def normalize_repo_part(raw: str | None) -> str:
    """``" - "`` -> ``"-"``; ``"1"``/``"2"`` stay as-is."""
    text = clean_text(raw).strip("«»\"' ")
    if text in {"", "-", "—", "–", "0", "_"}:
        return "-"
    if text.startswith("1"):
        return "1"
    if text.startswith("2"):
        return "2"
    return text


def normalize_edrpou(raw: str | None) -> str:
    digits = re.sub(r"\D", "", clean_text(raw))
    return digits.zfill(8) if digits else ""


def is_valid_isin(raw: str | None) -> bool:
    return bool(raw) and bool(_ISIN_RE.match(raw.strip().upper()))


# --------------------------------------------------------------------------- #
# Search results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class SearchRow:
    report_id: str
    published_at: datetime | None
    edrpou: str
    entity_name: str
    info_type: str
    xml_url: str = ""


@dataclass
class SearchPage:
    rows: list[SearchRow] = field(default_factory=list)
    total_pages: int = 1

    @property
    def is_empty(self) -> bool:
        return not self.rows


def _text(node: Any) -> str:
    if node is None:
        return ""
    return clean_text(node.text(deep=True, strip=False))


def parse_search_page(html: str) -> SearchPage:
    tree = HTMLParser(html)
    table = None
    for candidate in tree.css("table.xsTableReport"):
        if candidate.css_first('a[href^="/db/feed/"]'):
            table = candidate
            break

    rows: list[SearchRow] = []
    if table is not None:
        for tr in table.css("tr"):
            cells = tr.css("td")
            if len(cells) < 4:
                continue
            link = cells[-1].css_first('a[href^="/db/feed/"]') or tr.css_first(
                'a[href^="/db/feed/"]'
            )
            href = (link.attributes.get("href") if link is not None else "") or ""
            match = _REPORT_ID_RE.search(href)
            if not match:
                continue
            xml_link = tr.css_first('a[href$="report.xml"]')
            xml_url = (xml_link.attributes.get("href") if xml_link is not None else "") or ""
            if xml_url.startswith("/"):
                xml_url = f"{SMIDA_BASE_URL}{xml_url}"
            rows.append(
                SearchRow(
                    report_id=match.group(1),
                    published_at=parse_datetime(_text(cells[0])),
                    edrpou=normalize_edrpou(_text(cells[1])),
                    entity_name=_text(cells[2]),
                    info_type=_text(cells[3]),
                    xml_url=xml_url,
                )
            )

    total_pages = 1
    for anchor in tree.css(".pagination a"):
        match = _PAGE_RE.search(anchor.attributes.get("href", "") or "")
        if match:
            total_pages = max(total_pages, int(match.group(1)))
    return SearchPage(rows=rows, total_pages=total_pages)


# --------------------------------------------------------------------------- #
# Disclosure form
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ReportMeta:
    exchange_edrpou: str
    exchange_name: str
    as_of_date: date | None


TRADE_COLUMNS = (
    "trade_date",
    "repo_part",
    "issuer_name",
    "issuer_edrpou",
    "instrument_type",
    "isin",
    "quantity",
    "price",
)

#: Keyword fragments used to map a header cell to a canonical column.  Header
#: wording changed slightly between 2019 and 2024, hence the fuzzy matching.
_HEADER_PATTERNS: dict[str, tuple[str, ...]] = {
    "trade_date": ("дата вчинення", "дата укладання", "дата правочину"),
    "repo_part": ("частина операції", "репо"),
    "issuer_name": ("найменування емітента",),
    "issuer_edrpou": ("єдрпоу емітента", "код за єдрпоу"),
    "instrument_type": ("вид фінансового інструменту", "вид інструменту"),
    "isin": ("міжнародний ідентифікаційний", "isin"),
    "quantity": ("кількість",),
    "price": ("ціна",),
}


@dataclass
class ReportForm:
    meta: ReportMeta
    trades: list[dict[str, Any]] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: transport that produced the rows: ``"html"`` or ``"xml"``
    source: str = "html"


def _map_headers(headers: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    lowered = [h.lower() for h in headers]
    for column, keywords in _HEADER_PATTERNS.items():
        for idx, header in enumerate(lowered):
            if idx in mapping.values():
                continue
            if any(keyword in header for keyword in keywords):
                mapping[column] = idx
                break
    return mapping


def parse_report_form(html: str) -> ReportForm:
    tree = HTMLParser(html)
    warnings: list[str] = []

    meta_edrpou, meta_name, as_of = "", "", None
    meta_table = tree.css_first("table.xsTableReportLine")
    if meta_table is not None:
        for tr in meta_table.css("tr"):
            key = _text(tr.css_first("th")).lower()
            value = _text(tr.css_first("td"))
            if "єдрпоу" in key:
                meta_edrpou = normalize_edrpou(value)
            elif "найменування" in key:
                meta_name = value
            elif "дата" in key:
                as_of = parse_date(value)

    trades: list[dict[str, Any]] = []
    headers: list[str] = []
    table = None
    for candidate in tree.css("table.xsTableReport"):
        # The trade table is the one whose header mentions the contract date.
        if "дата вчинення" in _text(candidate).lower():
            table = candidate
            break
    if table is None:
        warnings.append("trade table not found")
        return ReportForm(ReportMeta(meta_edrpou, meta_name, as_of), trades, headers, warnings)

    trs = table.css("tr")
    if trs:
        headers = [_text(th) for th in trs[0].css("th")]
    mapping = _map_headers(headers)
    missing = [c for c in TRADE_COLUMNS if c not in mapping]
    if missing:
        warnings.append(f"unmapped columns: {','.join(missing)}")

    for position, tr in enumerate(trs[1:], start=2):
        cells = tr.css("td")
        if not cells:
            continue
        if len(cells) < len(mapping):
            warnings.append(f"row {position}: expected >= {len(mapping)} cells, got {len(cells)}")
            continue
        raw = {column: _text(cells[idx]) for column, idx in mapping.items()}
        trade_date = parse_date(raw.get("trade_date"))
        if trade_date is None:
            warnings.append(f"row {position}: unparsable date {raw.get('trade_date')!r}")
            continue
        quantity = parse_number(raw.get("quantity"))
        price = parse_number(raw.get("price"))
        isin = clean_text(raw.get("isin")).upper().replace(" ", "")
        trades.append(
            {
                "trade_date": trade_date,
                "repo_part": normalize_repo_part(raw.get("repo_part")),
                "issuer_name": clean_text(raw.get("issuer_name")),
                "issuer_edrpou": normalize_edrpou(raw.get("issuer_edrpou")),
                "instrument_type": clean_text(raw.get("instrument_type")),
                "isin": isin,
                "quantity": quantity,
                "price": price,
            }
        )
    return ReportForm(ReportMeta(meta_edrpou, meta_name, as_of), trades, headers, warnings)


# --------------------------------------------------------------------------- #
# Machine-readable disclosure (report.xml)
# --------------------------------------------------------------------------- #
XML_NS = "{http://nssmc.gov.ua/Schem/DisclExch}"

#: ISO 10962 (CFI) category letter -> Ukrainian asset-class label.  The XML
#: carries the standardised classification and the НКЦПФР instrument code,
#: not the free-text description the HTML form shows.
CFI_CATEGORY_UK: dict[str, str] = {
    "E": "Акція",
    "C": "Інститут спільного інвестування",
    "D": "Облігація",
    "R": "Право (entitlement)",
    "O": "Опціон",
    "F": "Ф'ючерс",
    "S": "Своп",
    "J": "Форвард",
    "H": "Позалістинговий складний інструмент",
    "I": "Референсний інструмент",
    "K": "Стратегія",
    "L": "Фінансування",
    "T": "Інше",
}


def instrument_label(fininstr: str, st_cfi: str) -> str:
    """Human-readable stand-in for the HTML description, built from codes.

    ``"Акція (CFI ESVUXR, код НКЦПФР 01110100)"`` — honest about its origin, so
    rows sourced from XML are never confused with the free-text form labels.
    """
    fininstr = clean_text(fininstr)
    st_cfi = clean_text(st_cfi).upper()
    category = CFI_CATEGORY_UK.get(st_cfi[:1], "") if st_cfi else ""
    parts = []
    if category:
        parts.append(category)
    if st_cfi:
        parts.append(f"CFI {st_cfi}")
    if fininstr:
        parts.append(f"код НКЦПФР {fininstr}")
    return f"{parts[0]} ({', '.join(parts[1:])})" if len(parts) > 1 else (parts[0] if parts else "")


def parse_report_xml(data: bytes) -> ReportForm:
    """Parse ``/files/feed/.../report.xml`` — the same tape as the HTML form.

    The file is a static resource, so it is orders of magnitude cheaper for
    SMIDA to serve than the rendered form (0.1 s vs up to a minute) and it is
    the only way to obtain reports whose HTML page exceeds the server timeout.
    """
    warnings: list[str] = []
    try:
        root = ET.fromstring(data.decode("windows-1251", errors="replace"))
    except ET.ParseError as exc:
        return ReportForm(ReportMeta("", "", None), [], [], [f"xml parse error: {exc}"])

    meta_edrpou = normalize_edrpou(root.attrib.get("D_EDRPOU"))
    meta_name = clean_text(root.attrib.get("D_NAME"))
    as_of = parse_date(root.attrib.get("STD"))

    # ISIN -> (CFI, ST_TYPE) from the exchange's securities list in the same file.
    securities: dict[str, tuple[str, str]] = {}
    dod2 = root.find(XML_NS + "DTSO_DOD_2")
    if dod2 is not None:
        for row in dod2.findall(XML_NS + "row"):
            isin = clean_text(row.attrib.get("ISIN")).upper()
            if isin:
                securities[isin] = (
                    clean_text(row.attrib.get("ST_CFI")),
                    clean_text(row.attrib.get("ST_TYPE")),
                )

    trades: list[dict[str, Any]] = []
    section = root.find(XML_NS + "DTSDISCL")
    if section is None:
        warnings.append("trade section DTSDISCL not found")
        return ReportForm(ReportMeta(meta_edrpou, meta_name, as_of), [], [], warnings)

    for position, row in enumerate(section.findall(XML_NS + "row"), start=1):
        trade_date = parse_date(row.attrib.get("D_BCONTR"))
        if trade_date is None:
            if len(warnings) < 20:
                warnings.append(f"xml row {position}: unparsable D_BCONTR")
            continue
        isin = clean_text(row.attrib.get("ISIN")).upper()
        cfi, st_type = securities.get(isin, ("", ""))
        trades.append(
            {
                "trade_date": trade_date,
                "repo_part": normalize_repo_part(row.attrib.get("PARTREPO")),
                "issuer_name": clean_text(row.attrib.get("EMNAME")),
                "issuer_edrpou": normalize_edrpou(row.attrib.get("EMEDRPOU")),
                "instrument_type": instrument_label(
                    row.attrib.get("FININSTR") or st_type, cfi
                ),
                "isin": isin,
                "quantity": parse_number(row.attrib.get("CNT")),
                "price": parse_number(row.attrib.get("PRICE")),
            }
        )
    return ReportForm(
        ReportMeta(meta_edrpou, meta_name, as_of),
        trades,
        list(TRADE_COLUMNS),
        warnings,
        source="xml",
    )
