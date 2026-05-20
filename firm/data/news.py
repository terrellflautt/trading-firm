"""News/RSS aggregator.

Pulls headlines for a ticker from free sources:
  - Yahoo Finance per-ticker RSS feed
  - MarketWatch per-ticker RSS feed
  - SeekingAlpha per-ticker RSS feed

All entries go through the SQLite cache so we don't hit feeds repeatedly.
Headlines are returned as `NewsItem` dataclasses ranked newest-first.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import feedparser

from ..utils.time import utc_now
from .cache import Cache

log = logging.getLogger(__name__)

NEWS_TTL_SECONDS = 1800  # 30 min


@dataclass(frozen=True)
class NewsItem:
    title: str
    url: str
    source: str
    published_at: datetime
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "title": self.title, "url": self.url, "source": self.source,
            "published_at": self.published_at.isoformat(),
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "NewsItem":
        return cls(
            title=d["title"], url=d["url"], source=d["source"],
            published_at=datetime.fromisoformat(d["published_at"]),
            summary=d.get("summary", ""),
        )


# Feed URL templates — {sym} is the ticker symbol
FEEDS: dict[str, str] = {
    "yahoo": "https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US",
    "seekingalpha": "https://seekingalpha.com/api/sa/combined/{sym}.xml",
}


class NewsAggregator:
    def __init__(self, cache: Cache):
        self.cache = cache

    def fetch(self, symbol: str, limit: int = 15) -> list[NewsItem]:
        """Fetch and merge headlines for a single ticker."""
        sym = symbol.upper()
        key = f"news:{sym}"
        cached = self.cache.get(key)
        if cached is not None:
            data, _ = cached
            return [NewsItem.from_dict(d) for d in data][:limit]

        items: list[NewsItem] = []
        for source, tmpl in FEEDS.items():
            url = tmpl.format(sym=sym)
            try:
                parsed = feedparser.parse(url)
            except Exception as e:
                log.debug("news feed %s %s failed: %s", source, sym, e)
                continue
            for entry in parsed.entries or []:
                items.append(self._entry_to_item(entry, source))

        # De-dupe by URL
        seen: set[str] = set()
        deduped: list[NewsItem] = []
        for it in items:
            if it.url in seen:
                continue
            seen.add(it.url)
            deduped.append(it)

        deduped.sort(key=lambda x: x.published_at, reverse=True)
        deduped = deduped[: limit * 2]   # Cache more than we expose
        self.cache.set(key, [it.to_dict() for it in deduped], NEWS_TTL_SECONDS)
        return deduped[:limit]

    @staticmethod
    def _entry_to_item(entry, source: str) -> NewsItem:
        title = (entry.get("title") or "").strip()
        url = (entry.get("link") or "").strip()
        summary = (entry.get("summary") or "").strip()
        published: datetime = utc_now()
        for k in ("published_parsed", "updated_parsed"):
            t = entry.get(k)
            if t:
                try:
                    published = datetime(*t[:6])
                    break
                except Exception:
                    pass
        return NewsItem(title=title, url=url, source=source, published_at=published, summary=summary)


def format_headlines_for_prompt(items: Iterable[NewsItem], max_items: int = 10) -> str:
    items = list(items)[:max_items]
    if not items:
        return "(no recent headlines)"
    lines = []
    for i, it in enumerate(items, 1):
        when = it.published_at.strftime("%Y-%m-%d")
        lines.append(f"  {i}. [{when}|{it.source}] {it.title}")
    return "\n".join(lines)
