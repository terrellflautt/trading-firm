"""Barchart scraper tests — parser logic only (no network)."""
from __future__ import annotations

from pathlib import Path

import pytest

from firm.data.barchart import BarchartScraper
from firm.data.cache import Cache


def test_extract_percent_matches_iv_rank():
    html = "<html>IV Rank: 67.5% blah</html>"
    val = BarchartScraper._extract_percent(html, [r"IV\s*Rank[^0-9%]*([0-9.]+)\s*%"])
    assert val == 67.5


def test_extract_percent_returns_none_when_missing():
    val = BarchartScraper._extract_percent("nothing here", [r"IV\s*Rank[^0-9%]*([0-9.]+)\s*%"])
    assert val is None


def test_from_dict_round_trip(tmp_path: Path):
    cache = Cache(tmp_path / "c.sqlite")
    scraper = BarchartScraper(cache)
    cache.set("barchart:iv:TEST", {
        "symbol": "TEST", "iv_rank": 50.0, "iv_percentile": 70.0,
        "iv_30d": 0.6, "fetched_at": "2026-05-13",
    }, 60)
    got = scraper.fetch("TEST")
    assert got is not None
    assert got.iv_rank == 50.0
    assert got.iv_percentile == 70.0
    assert got.iv_30d == 0.6
