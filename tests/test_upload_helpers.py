"""Tests de los helpers de upload (parseo Excel, converters)."""
import io
import pytest
import openpyxl
from app.sync.upload_helpers import (
    read_excel, to_float, to_date, str_or_none,
    _parse_with_openpyxl, _parse_with_zip_xml,
)


def _make_xlsx(rows: list[list]) -> bytes:
    """Crea un xlsx en memoria con las filas dadas."""
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------- read_excel ----------------------
def test_read_excel_basic():
    content = _make_xlsx([["A", "B"], [1, 2], [3, 4]])
    rows = read_excel(content)
    assert len(rows) == 3
    assert rows[0] == ("A", "B")
    assert rows[1] == (1, 2)


def test_read_excel_fallback_when_openpyxl_fails(monkeypatch):
    """Si openpyxl falla, debe usar el parser XML directo."""
    content = _make_xlsx([["X"], [1]])

    def boom(*args, **kwargs):
        raise ValueError("simulated openpyxl failure")

    monkeypatch.setattr("app.sync.upload_helpers._parse_with_openpyxl", boom)
    rows = read_excel(content)
    # El XML parser produce al menos las mismas filas (puede variar en formato exacto)
    assert len(rows) >= 2


# ---------------------- to_float ----------------------
def test_to_float_int_float():
    assert to_float(42) == 42.0
    assert to_float(3.14) == 3.14


def test_to_float_string_clean():
    assert to_float("100") == 100.0
    assert to_float("1,000") == 1000.0
    assert to_float("15%") == 15.0


def test_to_float_none_empty():
    assert to_float(None) is None
    assert to_float("") is None
    assert to_float("  ") is None


def test_to_float_invalid():
    assert to_float("not-a-number") is None
    assert to_float("abc") is None


# ---------------------- to_date ----------------------
def test_to_date_formats():
    assert to_date("2026-05-24") == "2026-05-24"
    assert to_date("2026/05/24") == "2026-05-24"
    assert to_date("24/05/2026") == "2026-05-24"


def test_to_date_with_time():
    assert to_date("2026-05-24T10:30:00") == "2026-05-24"
    assert to_date("2026/05/24 10:30:00") == "2026-05-24"


def test_to_date_datetime_obj():
    from datetime import datetime, date
    assert to_date(datetime(2026, 5, 24, 10, 0)) == "2026-05-24"
    assert to_date(date(2026, 5, 24)) == "2026-05-24"


def test_to_date_invalid():
    assert to_date(None) is None
    assert to_date("") is None
    assert to_date("not-a-date") is None


# ---------------------- str_or_none ----------------------
def test_str_or_none():
    assert str_or_none("hello") == "hello"
    assert str_or_none("  spaces  ") == "spaces"
    assert str_or_none(None) is None
    assert str_or_none("") is None
    assert str_or_none("   ") is None
    assert str_or_none(123) == "123"
