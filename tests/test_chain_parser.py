"""Options chain paste parser tests."""
from __future__ import annotations

from datetime import date

import pytest

from firm.data.chain_parser import ChainParseError, parse_chain
from firm.portfolio.types import OptionType


def test_stacked_csv_puts():
    raw = """Strike,Bid,Ask,Last,Volume,OpenInterest,IV
45.00,1.20,1.30,1.25,100,500,55%
50.00,2.40,2.60,2.50,80,400,52%
55.00,4.10,4.30,4.20,50,200,50%"""
    chain = parse_chain(raw, "IONQ", date(2026, 5, 30), spot_price=55.0)
    puts = chain.puts()
    assert len(puts) == 3
    assert puts[0].strike == 45.0
    assert 1.15 < puts[0].bid < 1.25
    assert 0.5 < puts[0].implied_volatility < 0.6   # 55% -> 0.55


def test_side_by_side_layout():
    raw = """Last,Bid,Ask,Volume,OI,IV,Strike,IV,OI,Volume,Ask,Bid,Last
4.00,3.90,4.10,10,50,40%,50.00,50%,100,15,1.30,1.20,1.25
2.00,1.90,2.10,20,80,45%,55.00,55%,80,12,3.20,3.10,3.15"""
    chain = parse_chain(raw, "IONQ", date(2026, 5, 30), spot_price=53.0)
    puts = chain.puts()
    calls = chain.calls()
    assert len(puts) == 2
    assert len(calls) == 2
    # Verify calls and puts on each row produce one of each at the same strike
    assert {c.strike for c in calls} == {50.0, 55.0}
    assert {p.strike for p in puts} == {50.0, 55.0}


def test_generic_numeric_fallback():
    """No header — just numeric rows, treated as puts by default."""
    raw = "45.00 1.20 1.30 1.25 100 500 55"
    chain = parse_chain(raw, "IONQ", date(2026, 5, 30), spot_price=55.0)
    assert len(chain.contracts) == 1
    c = chain.contracts[0]
    assert c.strike == 45.0
    assert c.bid == 1.20


def test_empty_raises():
    with pytest.raises(ChainParseError):
        parse_chain("", "IONQ", date(2026, 5, 30), spot_price=55.0)


def test_drops_zero_priced_rows():
    raw = """Strike,Bid,Ask,Last,Volume,OI,IV
45.00,0,0,0,0,0,0
50.00,2.40,2.60,2.50,80,400,52%"""
    chain = parse_chain(raw, "IONQ", date(2026, 5, 30), spot_price=55.0)
    # Zero-priced row should be skipped
    assert len(chain.contracts) == 1
    assert chain.contracts[0].strike == 50.0
