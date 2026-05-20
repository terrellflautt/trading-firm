"""Barchart IV-rank / IV-percentile scraper.

Barchart's `iv-implied-volatility` page publishes the IV rank and IV percentile
for free, but they're rendered in HTML rather than via a documented API.
We scrape politely (1 req per ticker per 30 min) and fall back to None on any
failure — callers degrade to `approx_iv_rank` from realized vol.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup

from ..utils.time import utc_now
from .cache import Cache

log = logging.getLogger(__name__)

BARCHART_TTL_SECONDS = 1800  # 30 min

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class BarchartIV:
    symbol: str
    iv_rank: float | None
    iv_percentile: float | None
    iv_30d: float | None         # 30-day historical/implied volatility (annualized)


class BarchartScraper:
    """Pull IV rank/percentile from Barchart's public stock pages.

    Note: Barchart's HTML changes occasionally; the regex/parsing is permissive
    and will simply return None if the page layout shifts. The yfinance-based
    `approx_iv_rank` is the safety net used by callers.
    """

    URL = "https://www.barchart.com/stocks/quotes/{sym}/options-overview"

    def __init__(self, cache: Cache, timeout: float = 8.0):
        self.cache = cache
        self.timeout = timeout

    def fetch(self, symbol: str) -> BarchartIV | None:
        sym = symbol.upper()
        key = f"barchart:iv:{sym}"
        cached = self.cache.get(key)
        if cached is not None:
            data, _ = cached
            return self._from_dict(data)

        url = self.URL.format(sym=sym)
        try:
            with httpx.Client(timeout=self.timeout, headers={"User-Agent": USER_AGENT}) as c:
                r = c.get(url, follow_redirects=True)
                r.raise_for_status()
                html = r.text
        except Exception as e:
            log.debug("barchart fetch failed for %s: %s", sym, e)
            stale = self.cache.get(key, allow_stale=True)
            if stale is not None:
                return self._from_dict(stale[0])
            return None

        iv_rank = self._extract_percent(html, [
            r"IV\s*Rank[^0-9%]*([0-9.]+)\s*%",
            r"iv_rank[^0-9%]*([0-9.]+)\s*%",
        ])
        iv_percentile = self._extract_percent(html, [
            r"IV\s*Percentile[^0-9%]*([0-9.]+)\s*%",
            r"iv_percentile[^0-9%]*([0-9.]+)\s*%",
        ])
        iv_30d = self._extract_percent(html, [
            r"30\s*-?Day\s*IV[^0-9%]*([0-9.]+)\s*%",
            r"30-Day\s*Historical[^0-9%]*([0-9.]+)\s*%",
        ])

        if iv_rank is None and iv_percentile is None:
            # Probably hit a bot block or layout change
            log.debug("barchart page parsed but no IV figures found for %s", sym)
            return None

        result = BarchartIV(
            symbol=sym, iv_rank=iv_rank,
            iv_percentile=iv_percentile,
            iv_30d=iv_30d / 100.0 if iv_30d is not None else None,
        )
        self.cache.set(
            key,
            {"symbol": sym, "iv_rank": iv_rank, "iv_percentile": iv_percentile,
             "iv_30d": result.iv_30d, "fetched_at": utc_now().isoformat()},
            BARCHART_TTL_SECONDS,
        )
        return result

    @staticmethod
    def _from_dict(d: dict) -> BarchartIV:
        return BarchartIV(
            symbol=d["symbol"], iv_rank=d.get("iv_rank"),
            iv_percentile=d.get("iv_percentile"), iv_30d=d.get("iv_30d"),
        )

    @staticmethod
    def _extract_percent(html: str, patterns: list[str]) -> float | None:
        for pat in patterns:
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                try:
                    return float(m.group(1))
                except ValueError:
                    continue
        return None
