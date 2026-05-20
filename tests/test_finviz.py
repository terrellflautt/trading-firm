"""Finviz parser tests (no network)."""
from __future__ import annotations

from firm.data.finviz import _parse_cap, _parse_float, _parse_int_with_commas, _parse_pct


def test_parse_cap_billions():
    assert _parse_cap("1.50B") == 1.5e9


def test_parse_cap_millions():
    assert _parse_cap("500.00M") == 500e6


def test_parse_cap_thousands():
    assert _parse_cap("50.00K") == 50_000


def test_parse_cap_empty():
    assert _parse_cap("-") is None
    assert _parse_cap("") is None


def test_parse_float_handles_commas():
    assert _parse_float("1,234.56") == 1234.56


def test_parse_int_with_commas():
    assert _parse_int_with_commas("1,000,000") == 1_000_000


def test_parse_pct_signed():
    assert _parse_pct("3.45%") == 3.45
    assert _parse_pct("-1.23%") == -1.23
