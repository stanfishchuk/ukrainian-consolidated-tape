from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from smida_tape.parsing import (
    instrument_label,
    is_valid_isin,
    normalize_edrpou,
    normalize_repo_part,
    parse_date,
    parse_number,
    parse_report_form,
    parse_report_xml,
    parse_search_page,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 059,910000", 1059.91),
        ("24078.790000", 24078.79),
        ("2823", 2823.0),
        (" - ", None),
        ("", None),
        ("1 234 567,5", 1234567.5),
        ("1.234,56", 1234.56),
        ("1,234,567.89", 1234567.89),
        ("\u00a05\u00a0000", 5000.0),
        ("n/a", None),
    ],
)
def test_parse_number(raw: str, expected: float | None) -> None:
    result = parse_number(raw)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("30.12.2025", date(2025, 12, 30)), ("2025-12-30", date(2025, 12, 30)), ("", None), ("x", None)],
)
def test_parse_date(raw: str, expected: date | None) -> None:
    assert parse_date(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(" - ", "-"), ("-", "-"), ("1", "1"), (" 2 ", "2"), ("", "-"), (None, "-")],
)
def test_normalize_repo_part(raw: str | None, expected: str) -> None:
    assert normalize_repo_part(raw) == expected


def test_normalize_edrpou() -> None:
    assert normalize_edrpou(" 13480 ") == "00013480"
    assert normalize_edrpou("") == ""


def test_isin_validation() -> None:
    assert is_valid_isin("UA4000069603")
    assert is_valid_isin("UAUSD0000000")
    assert not is_valid_isin("UA40000696")
    assert not is_valid_isin("")


def test_parse_search_page() -> None:
    page = parse_search_page((FIXTURES / "search_page.html").read_text(encoding="utf-8"))

    assert len(page.rows) == 20
    assert page.total_pages == 88
    first = page.rows[0]
    assert first.report_id == "144131"
    assert first.edrpou == "21672206"
    assert first.published_at == datetime(2025, 12, 30, 17, 55, 1)
    assert "ПФТС" in first.entity_name
    assert "Щоденні дані" in first.info_type


def test_parse_report_form() -> None:
    form = parse_report_form((FIXTURES / "report_form.html").read_text(encoding="utf-8"))

    assert form.meta.exchange_edrpou == "21672206"
    assert form.meta.as_of_date == date(2025, 12, 30)
    assert "ПФТС" in form.meta.exchange_name
    assert not [w for w in form.warnings if "unmapped" in w]
    assert len(form.trades) == 6

    trade = form.trades[0]
    assert trade["trade_date"] == date(2025, 12, 30)
    assert trade["repo_part"] == "-"
    assert trade["issuer_edrpou"] == "00013480"
    assert trade["isin"] == "UA4000235378"
    assert trade["quantity"] == pytest.approx(2823.0)
    assert trade["price"] == pytest.approx(1059.91)

    repo_legs = [t for t in form.trades if t["repo_part"] in {"1", "2"}]
    assert repo_legs and repo_legs[0]["isin"] == "UAUSD0000000"

    # SMIDA lists every contract twice in the same form.
    signatures = {(t["isin"], t["quantity"], t["price"], t["repo_part"]) for t in form.trades}
    assert len(signatures) == 3


def test_parse_report_xml() -> None:
    form = parse_report_xml((FIXTURES / "report_form.xml").read_bytes())

    assert form.source == "xml"
    assert form.meta.exchange_edrpou == "21672206"
    assert form.meta.as_of_date == date(2025, 12, 30)
    assert form.warnings == []
    assert len(form.trades) == 6

    bond = form.trades[0]
    assert bond["trade_date"] == date(2025, 12, 30)
    assert bond["repo_part"] == "-"
    assert bond["issuer_edrpou"] == "00013480"
    assert bond["isin"] == "UA4000235378"
    assert bond["quantity"] == pytest.approx(2823.0)
    assert bond["price"] == pytest.approx(1059.91)
    # codes only: the XML carries the classifier, not the free-text description
    assert bond["instrument_type"] == "Облігація (CFI DBFTFR, код НКЦПФР 01120244)"

    repo = [t for t in form.trades if t["repo_part"] != "-"]
    assert repo and repo[0]["isin"] == "UAUSD0000000"
    assert repo[0]["instrument_type"].startswith("Своп")


def test_parse_report_xml_is_robust_to_garbage() -> None:
    form = parse_report_xml(b"<not-xml")
    assert form.trades == []
    assert form.warnings and "xml parse error" in form.warnings[0]


def test_instrument_label_handles_missing_parts() -> None:
    assert instrument_label("01120244", "DBFTFR").startswith("Облігація")
    assert instrument_label("01110100", "") == "код НКЦПФР 01110100"
    assert instrument_label("", "ESVUXR") == "Акція (CFI ESVUXR)"
    assert instrument_label("", "") == ""
