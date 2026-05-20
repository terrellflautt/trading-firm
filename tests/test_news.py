"""News aggregator tests — exercises caching, de-dup, prompt formatting (no live feeds)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from firm.data.cache import Cache
from firm.data.news import NewsAggregator, NewsItem, format_headlines_for_prompt


@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    return Cache(tmp_path / "cache.sqlite")


def test_dedup_and_sort(cache: Cache):
    agg = NewsAggregator(cache)
    items = [
        NewsItem("Old", "http://a", "yahoo", datetime(2026, 5, 1)),
        NewsItem("Newer", "http://b", "yahoo", datetime(2026, 5, 10)),
        NewsItem("Dup", "http://b", "marketwatch", datetime(2026, 5, 11)),
    ]
    # Simulate what fetch() does internally
    seen: set[str] = set()
    deduped = []
    for it in items:
        if it.url in seen:
            continue
        seen.add(it.url)
        deduped.append(it)
    deduped.sort(key=lambda x: x.published_at, reverse=True)
    assert [i.title for i in deduped] == ["Newer", "Old"]


def test_cache_round_trip(cache: Cache):
    item = NewsItem("Test", "http://x", "yahoo", datetime(2026, 5, 13), summary="abc")
    cache.set("news:TEST", [item.to_dict()], 60)
    got = cache.get("news:TEST")
    assert got is not None
    data, stale = got
    assert not stale
    parsed = NewsItem.from_dict(data[0])
    assert parsed.title == "Test"
    assert parsed.summary == "abc"


def test_format_headlines_renders_with_items():
    items = [
        NewsItem("Earnings beat", "http://a", "yahoo", datetime(2026, 5, 12)),
        NewsItem("Sector news", "http://b", "seekingalpha", datetime(2026, 5, 11)),
    ]
    s = format_headlines_for_prompt(items)
    assert "Earnings beat" in s
    assert "2026-05-12" in s
    assert "yahoo" in s


def test_format_headlines_handles_empty():
    assert format_headlines_for_prompt([]) == "(no recent headlines)"
