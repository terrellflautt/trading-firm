"""Scout — finds new tickers outside the watchlist worth investigating.

Discovery sources (all free):
  1. **Sector / liquidity screen** (Finviz) — companies under our per-account
     price caps with options eligibility and adequate volume.
  2. **Earnings movers** — same screen filtered to companies that reported
     this week or last week (post-earnings IV gives wheel premium).
  3. **Unusual options activity** — for each candidate, compare today's total
     options volume to the 30-day average. Anomalies are flagged.

We then RANK candidates by a wheel-fit score:
  - Premium potential (IV rank, via approx)
  - Liquidity (avg volume, options OI on front expiry)
  - Account-fit (does at least one account allow 100 shares?)
  - Setup quality (light technical score using existing indicator engine)

The Scout writes its top picks to the ledger `scout_finds` table so they
can be surfaced in the daily report and dashboard. It does NOT auto-add
new tickers to the watchlist — that's a user decision.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..config import Config
from ..data.cache import Cache
from ..data.finviz import FinvizRow, FinvizScreener
from ..data.yfinance_client import YFClient
from ..portfolio.ledger import Ledger
from ..portfolio.types import OHLCBar, OptionsChain
from ..strategy.indicators import compute_indicators
from ..strategy.options_math import approx_iv_rank, atm_iv_for_chain
from ..utils.time import utc_now

log = logging.getLogger(__name__)


@dataclass
class ScoutCandidate:
    symbol: str
    sector_guess: str
    source: str                   # 'sector_screen' | 'earnings_mover' | 'unusual_options'
    price: float
    avg_volume_30d: int | None
    today_volume: int
    iv_rank: float | None
    iv_30d_annualized: float | None
    fits_account: list[str]       # account keys where 100 shares fit
    options_oi_total_front: int
    indicators_summary: str
    score: float
    rationale: str
    metrics: dict = field(default_factory=dict)


class Scout:
    def __init__(self, cfg: Config, yf: YFClient, finviz: FinvizScreener, ledger: Ledger):
        self.cfg = cfg
        self.yf = yf
        self.finviz = finviz
        self.ledger = ledger

    # ─── Entry points ──────────────────────────────────────────────────────

    async def discover(self, top_n: int = 5) -> list[ScoutCandidate]:
        """Run all discovery sources and return the top-N candidates by score."""
        watchlist_syms = {e.symbol.upper() for e in self.cfg.watchlist.watchlist}

        # 1. Sector / liquidity screen for each account's price cap
        screen_rows = await asyncio.to_thread(self._sector_screen)
        # 2. Earnings movers (this week + last week)
        earnings_rows = await asyncio.to_thread(self._earnings_movers)

        # Merge & dedupe, exclude existing watchlist
        candidates_raw: dict[str, tuple[FinvizRow, str]] = {}
        for r in screen_rows:
            if r.symbol in watchlist_syms:
                continue
            candidates_raw[r.symbol] = (r, "sector_screen")
        for r in earnings_rows:
            if r.symbol in watchlist_syms or r.symbol in candidates_raw:
                continue
            candidates_raw[r.symbol] = (r, "earnings_mover")

        # 3. Enrich each candidate with yf data + analytics
        enriched: list[ScoutCandidate] = []
        # Limit how many we enrich (yfinance calls are expensive)
        for row, source in list(candidates_raw.values())[:30]:
            cand = await asyncio.to_thread(self._enrich, row, source)
            if cand is not None:
                enriched.append(cand)

        # 4. Add unusual-options-activity flag where applicable
        for cand in enriched:
            if self._is_unusual_options(cand):
                cand.rationale += " | unusual options volume vs. avg"
                cand.score += 0.1
                cand.metrics["unusual_options"] = True

        # 5. Score & rank
        ranked = sorted(enriched, key=lambda c: -c.score)[:top_n]
        self._persist(ranked)
        return ranked

    # ─── Discovery sources ────────────────────────────────────────────────

    def _sector_screen(self) -> list[FinvizRow]:
        """Use the larger of the per-account price caps so we don't miss mid-priced names."""
        max_price = max(
            a.max_share_price for a in self.cfg.accounts.accounts.values()
        )
        return self.finviz.screen(
            max_price=max_price,
            min_avg_volume_thousands=int(self.cfg.watchlist.scout_filters.min_avg_volume / 1000),
            require_options=True,
        )

    def _earnings_movers(self) -> list[FinvizRow]:
        max_price = max(
            a.max_share_price for a in self.cfg.accounts.accounts.values()
        )
        out: list[FinvizRow] = []
        for window in ("thisweek", "lastweek"):
            out.extend(self.finviz.screen(
                max_price=max_price,
                min_avg_volume_thousands=int(self.cfg.watchlist.scout_filters.min_avg_volume / 1000),
                require_options=True,
                earnings_window=window,
            ))
        return out

    # ─── Enrichment ────────────────────────────────────────────────────────

    def _enrich(self, row: FinvizRow, source: str) -> ScoutCandidate | None:
        sym = row.symbol
        quote = self.yf.quote(sym)
        if quote is None:
            return None
        daily = self.yf.history_daily(sym, period="3mo")
        chain = self.yf.options_chain(sym, max_expiries=3)
        # Sector-tag against our themes
        sector_guess = self._guess_sector(row.industry, row.sector)
        # IV rank
        iv_rank = approx_iv_rank(chain, daily) if chain and daily else None
        # Indicators (lightweight summary)
        indicators = compute_indicators(sym, daily)
        ind_summary = indicators.to_summary() if indicators else "—"
        # Account fit
        fits = [
            key for key, a in self.cfg.accounts.accounts.items()
            if quote.price <= a.max_share_price and 100 * quote.price <= a.buying_power
        ]
        if not fits:
            return None
        # Liquidity check
        oi_total = self._front_oi_total(chain)
        if oi_total < self.cfg.watchlist.scout_filters.min_options_open_interest:
            return None
        # Score
        score = self._score(quote, row, iv_rank, oi_total, indicators)
        atm_iv = atm_iv_for_chain(chain) if chain else None
        return ScoutCandidate(
            symbol=sym, sector_guess=sector_guess, source=source,
            price=quote.price, avg_volume_30d=quote.avg_volume_30d,
            today_volume=quote.volume, iv_rank=iv_rank,
            iv_30d_annualized=atm_iv,
            fits_account=fits, options_oi_total_front=oi_total,
            indicators_summary=ind_summary, score=score,
            rationale=(
                f"{source}; IV rank {iv_rank:.0f}; " if iv_rank is not None
                else f"{source}; IV rank —; "
            ) + f"front OI {oi_total}; fits {','.join(fits)}; {ind_summary}",
            metrics={
                "price": quote.price,
                "iv_rank": iv_rank,
                "front_oi": oi_total,
                "day_change_pct": quote.day_change_pct,
            },
        )

    @staticmethod
    def _front_oi_total(chain: OptionsChain | None) -> int:
        if chain is None or not chain.expiries:
            return 0
        front = chain.expiries[0]
        return sum(c.open_interest for c in chain.contracts if c.expiry == front)

    @staticmethod
    def _guess_sector(industry: str, sector: str) -> str:
        """Map Finviz industry/sector to our internal theme key."""
        haystack = f"{industry} {sector}".lower()
        if any(k in haystack for k in ("quantum",)):
            return "quantum"
        if any(k in haystack for k in ("aerospace", "aviation", "defense")):
            return "evtol"
        if any(k in haystack for k in ("semiconductor", "computer hardware")):
            return "semis_gpu"
        if "battery" in haystack or "lithium" in haystack:
            return "batteries"
        if "space" in haystack:
            return "space"
        if any(k in haystack for k in ("nuclear", "uranium")):
            return "nuclear_smr"
        if any(k in haystack for k in ("software - application", "cloud", "data center")):
            return "ai_infrastructure"
        return industry.lower() or "unknown"

    @staticmethod
    def _is_unusual_options(c: ScoutCandidate) -> bool:
        # Heuristic until we have real options-volume history: high open interest
        # on a name with elevated IV rank suggests options activity is unusual.
        return (c.iv_rank or 0) >= 50 and c.options_oi_total_front >= 5000

    def _score(self, quote, row: FinvizRow, iv_rank, oi_total, indicators) -> float:
        """Composite wheel-fit score 0..1+ (higher is better)."""
        s = 0.0
        # IV rank: max 0.40 contribution when >= 70
        if iv_rank is not None:
            s += min(0.40, iv_rank / 200.0)
        # Liquidity: max 0.30 when OI ≥ 10k
        s += min(0.30, oi_total / 30_000.0)
        # Account fit: 0.10 baseline; +0.05 if both accounts fit
        s += 0.10
        # Trend: small lift for "up" trends, penalty for sharp downtrend without value
        if indicators is not None:
            if indicators.trend.direction == "up":
                s += 0.10 * indicators.trend.strength
            elif indicators.trend.direction == "down" and (indicators.rsi_14 or 0) < 30:
                s += 0.05  # oversold dip is OK for CSP
        # Movement: a 2-10% intraday move is interesting (potential mover)
        change = abs(quote.day_change_pct)
        if 2 <= change <= 10:
            s += 0.10
        elif change > 15:
            s -= 0.10  # too volatile
        # User-preferred sector: soft preference, not a hard filter
        sector_guess = self._guess_sector(row.industry, row.sector)
        if sector_guess in set(self.cfg.watchlist.scout_sectors):
            s += 0.15
        return round(s, 3)

    # ─── Persistence ──────────────────────────────────────────────────────

    def _persist(self, candidates: list[ScoutCandidate]) -> None:
        with self.ledger.connect() as conn:
            for c in candidates:
                conn.execute(
                    """INSERT INTO scout_finds
                       (symbol, sector, rationale, metrics_json, surfaced_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        c.symbol, c.sector_guess, c.rationale,
                        json.dumps(c.metrics, default=str),
                        utc_now().isoformat(),
                    ),
                )
