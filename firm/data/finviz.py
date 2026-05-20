"""Finviz screener adapter.

Finviz's screener page exposes a URL-encoded filter language. We compose a
filter for our wheel-suitable universe (price under cap, options-eligible,
enough volume, in our sector themes) and parse the HTML table back out.

Example URL:
  https://finviz.com/screener.ashx?v=111&f=cap_smallover,sh_avgvol_o500,
                                       sh_opt_option,sh_price_u50

Filter codes we use:
  cap_smallover     — small cap or larger
  sh_avgvol_oN      — average daily volume > N  (N in thousands)
  sh_opt_option     — has options available
  sh_price_uN       — price under $N
  sh_price_oN       — price over $N
  ind_<industry>    — industry filter
  earningsdate_thisweek / nextweek / lastweek
  fa_pe_under30     — P/E under 30
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

FINVIZ_TTL_SECONDS = 1800  # 30 min
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class FinvizRow:
    symbol: str
    company: str
    sector: str
    industry: str
    market_cap: float | None
    price: float
    volume: int
    change_pct: float

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "FinvizRow":
        return cls(**d)


# Maps our internal sector keys to Finviz industry filters where there is a
# direct equivalent. For broader themes we use the industry param on a comma
# concatenation server-side, but most of our themes are diffuse — so the
# common pattern is "everything matching liquidity filters" then we sector-
# tag client-side from yfinance.
THEME_KEYWORDS: dict[str, list[str]] = {
    "quantum": ["Quantum"],
    "evtol": ["Aerospace", "Aviation"],
    "semis_gpu": ["Semiconductor"],
    "batteries": ["Battery", "Lithium"],
    "space": ["Space"],
    "ai_infrastructure": ["AI", "Data Center", "Cloud"],
    "nuclear_smr": ["Nuclear", "Uranium"],
}


class FinvizScreener:
    URL = "https://finviz.com/screener.ashx"

    def __init__(self, cache: Cache, timeout: float = 10.0):
        self.cache = cache
        self.timeout = timeout

    def screen(
        self,
        max_price: float,
        min_avg_volume_thousands: int = 500,
        require_options: bool = True,
        earnings_window: str | None = None,  # "thisweek" | "nextweek" | "lastweek"
    ) -> list[FinvizRow]:
        """Run a wheel-suitable screen. Returns rows ranked by Finviz default."""
        filters = ["cap_smallover", f"sh_avgvol_o{min_avg_volume_thousands}"]
        if require_options:
            filters.append("sh_opt_option")
        # Round price ceiling to nearest $1; Finviz only supports integer thresholds.
        ceil = max(1, int(round(max_price)))
        filters.append(f"sh_price_u{ceil}")
        if earnings_window:
            filters.append(f"earningsdate_{earnings_window}")

        params = {"v": "111", "f": ",".join(filters), "o": "-change"}
        key = f"finviz:{','.join(filters)}"
        cached = self.cache.get(key)
        if cached is not None:
            data, _ = cached
            return [FinvizRow.from_dict(r) for r in data]

        try:
            with httpx.Client(timeout=self.timeout, headers={"User-Agent": USER_AGENT}) as c:
                r = c.get(self.URL, params=params, follow_redirects=True)
                r.raise_for_status()
                html = r.text
        except Exception as e:
            log.warning("finviz screener fetch failed: %s", e)
            stale = self.cache.get(key, allow_stale=True)
            if stale is not None:
                return [FinvizRow.from_dict(r) for r in stale[0]]
            return []

        rows = self._parse(html)
        self.cache.set(key, [r.to_dict() for r in rows], FINVIZ_TTL_SECONDS)
        return rows

    @staticmethod
    def _parse(html: str) -> list[FinvizRow]:
        soup = BeautifulSoup(html, "lxml")
        # The screener table has class names that change occasionally; we look for
        # any <table> with header row containing "Ticker".
        tables = soup.find_all("table")
        target = None
        for t in tables:
            head = t.find("tr")
            if head and "Ticker" in head.get_text():
                target = t
                break
        if target is None:
            return []

        rows: list[FinvizRow] = []
        for tr in target.find_all("tr")[1:]:
            cells = [c.get_text(strip=True) for c in tr.find_all("td")]
            if len(cells) < 11:
                continue
            try:
                rows.append(FinvizRow(
                    symbol=cells[1].upper(),
                    company=cells[2],
                    sector=cells[3],
                    industry=cells[4],
                    market_cap=_parse_cap(cells[6]),
                    price=_parse_float(cells[8]),
                    change_pct=_parse_pct(cells[9]),
                    volume=_parse_int_with_commas(cells[10]),
                ))
            except (ValueError, IndexError) as e:
                log.debug("finviz row parse fail: %s -- %s", cells, e)
                continue
        return rows


def _parse_float(s: str) -> float:
    return float(s.replace(",", ""))


def _parse_int_with_commas(s: str) -> int:
    return int(s.replace(",", ""))


def _parse_pct(s: str) -> float:
    return float(s.rstrip("%").replace(",", ""))


def _parse_cap(s: str) -> float | None:
    """Parse Finviz cap strings: '1.50B', '500.00M', '50.00K', or '-'. Returns dollars."""
    s = s.strip()
    if not s or s == "-":
        return None
    match = re.match(r"^([0-9.]+)([BMK]?)$", s)
    if not match:
        return None
    num = float(match.group(1))
    suffix = match.group(2)
    multiplier = {"B": 1e9, "M": 1e6, "K": 1e3, "": 1.0}[suffix]
    return num * multiplier
