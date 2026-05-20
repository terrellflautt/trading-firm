"""Orchestrator — coordinates a single scan from data fetch to ranked recommendations.

For Phase 1 this is just `scan_watchlist` and `scan_ticker`. The dashboard
loop in Phase 3 will reuse the same primitives.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .agents.call_agent import CallAgent
from .agents.fundamental import FundamentalAnalyst
from .agents.portfolio_manager import PortfolioManager
from .agents.put_agent import PutAgent
from .agents.risk_manager import RiskManager, RiskReviewOutcome
from .agents.scout import Scout, ScoutCandidate
from .agents.sentiment import SentimentAnalyst
from .agents.specialist import Proposal
from .agents.stock_agent import StockAgent
from .agents.technical import TechnicalAnalyst
from .agents.volatility import VolatilityAnalyst
from .config import Config
from .data.cache import Cache
from .data.finviz import FinvizScreener
from .data.news import NewsAggregator
from .data.user_reports import discover_user_reports, index_by_symbol
from .data.yfinance_client import YFClient
from .portfolio.accounts import AccountSnapshot, snapshot_account
from .portfolio.ledger import Ledger
from .portfolio.types import Recommendation, Signal, TickerContext
from .strategy.indicators import compute_indicators

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    """All the data and outputs from one scan, ready for terminal/dashboard/report."""
    contexts: dict[str, TickerContext]
    signals_by_symbol: dict[str, list[Signal]]
    proposals: list[Proposal]
    recommendations: list[Recommendation]
    rejected: list[tuple[Recommendation, str]]
    scout_finds: list[ScoutCandidate] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.scout_finds is None:
            self.scout_finds = []


class Orchestrator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cache = Cache(cfg.root / cfg.firm.data.cache_db)
        self.ledger = Ledger(cfg.root / cfg.firm.data.ledger_db)
        self.yf = YFClient(self.cache, cfg.firm.data)
        self.news = NewsAggregator(self.cache)
        self.finviz = FinvizScreener(self.cache)
        self.scout = Scout(cfg, self.yf, self.finviz, self.ledger)
        # Index any user-supplied reports at startup; small enough to re-scan
        # every Orchestrator construction (~ms).
        self._user_reports = index_by_symbol(
            discover_user_reports(
                cfg.root,
                known_symbols={e.symbol.upper() for e in cfg.watchlist.watchlist},
            )
        )
        # Agents created on demand so a `scan` without ANTHROPIC_API_KEY can
        # still build TickerContexts (useful for `firm config validate`).
        self._analysts: dict[str, object] = {}
        self._specialists: dict[str, object] = {}
        self.risk = RiskManager(cfg)
        self.pm = PortfolioManager(cfg, self.ledger)

    def _get_analysts(self) -> dict[str, object]:
        if not self._analysts:
            self._analysts = {
                "technical": TechnicalAnalyst(self.cfg),
                "volatility": VolatilityAnalyst(self.cfg),
                "fundamental": FundamentalAnalyst(self.cfg),
                "sentiment": SentimentAnalyst(self.cfg, news=self.news),
            }
        return self._analysts

    def _get_specialists(self) -> dict[str, object]:
        if not self._specialists:
            self._specialists = {
                "stock": StockAgent(self.cfg),
                "call": CallAgent(self.cfg),
                "put": PutAgent(self.cfg),
            }
        return self._specialists

    # ─── Public entry points ───────────────────────────────────────────────

    async def scan_watchlist(self, symbols: list[str] | None = None) -> ScanResult:
        if symbols is None:
            symbols = [e.symbol for e in self.cfg.watchlist.watchlist]
        contexts = await asyncio.gather(*(self._fetch_context(s) for s in symbols))
        contexts_map: dict[str, TickerContext] = {
            c.symbol: c for c in contexts if c is not None
        }
        if not contexts_map:
            log.warning("no tickers produced contexts — data layer may be offline")
            return ScanResult({}, {}, [], [], [])

        snapshots = {
            key: snapshot_account(key, cfg, self.ledger)
            for key, cfg in self.cfg.accounts.accounts.items()
        }

        # 1. Analyst signals — parallel fan-out across (analyst × ticker)
        analyses = await self._run_analysts(list(contexts_map.values()))
        signals_by_symbol: dict[str, list[Signal]] = {}
        for sym, sigs in analyses.items():
            ctx = contexts_map[sym]
            ctx.signals.extend(sigs)
            signals_by_symbol[sym] = sigs

        # 2. Specialist proposals — parallel fan-out across (specialist × ticker × account)
        proposals = await self._run_specialists(list(contexts_map.values()), snapshots)

        # 3. PM synthesizes proposals into Recommendations
        recs = self.pm.synthesize_proposals(proposals, contexts_map, snapshots)

        # 4. Risk Manager filters
        review = self.risk.review(recs, snapshots)
        return ScanResult(
            contexts=contexts_map,
            signals_by_symbol=signals_by_symbol,
            proposals=proposals,
            recommendations=self._rank(review),
            rejected=review.rejected,
        )

    async def scan_ticker(self, symbol: str) -> ScanResult:
        return await self.scan_watchlist([symbol])

    async def run_scout(self, top_n: int = 5) -> list[ScoutCandidate]:
        """Fire the Scout independently — useful for the CLI 'firm scout' command."""
        return await self.scout.discover(top_n=top_n)

    # ─── Internals ─────────────────────────────────────────────────────────

    async def _fetch_context(self, symbol: str) -> TickerContext | None:
        """Build a TickerContext for one symbol. Runs blocking yf calls in a thread."""
        loop = asyncio.get_running_loop()

        quote = await loop.run_in_executor(None, self.yf.quote, symbol)
        if quote is None:
            log.warning("no quote for %s — skipping", symbol)
            return None
        history_daily = await loop.run_in_executor(None, self.yf.history_daily, symbol, "6mo")
        intraday = await loop.run_in_executor(None, self.yf.history_intraday, symbol, 5)
        chain = await loop.run_in_executor(None, self.yf.options_chain, symbol, 6)
        funds = await loop.run_in_executor(None, self.yf.fundamentals, symbol)

        entry = self.cfg.watchlist.by_symbol(symbol)
        sector = entry.sector if entry else "unknown"
        notes_parts = [entry.notes] if entry and entry.notes else []
        # Fold in user-supplied report sections for this ticker (newest first)
        for sec in self._user_reports.get(symbol.upper(), [])[:2]:
            notes_parts.append(
                f"[User report {sec.report_date}] {sec.text[:1500]}"
            )
        notes = "\n\n".join(notes_parts)

        # Load wheel state + positions for each account so agents can see them
        wheel_states = {}
        shares_pos = {}
        option_pos = {}
        for acct_key in self.cfg.accounts.accounts:
            wheel_states[acct_key] = self.ledger.get_wheel_state(acct_key, symbol)
            pos = self.ledger.get_shares(acct_key, symbol)
            if pos is not None:
                shares_pos[acct_key] = pos
            opts = self.ledger.open_options_for(acct_key, symbol)
            if opts:
                option_pos[acct_key] = opts

        ctx = TickerContext(
            symbol=symbol.upper(),
            sector=sector,
            quote=quote,
            history_daily=history_daily,
            history_intraday=intraday,
            options_chain=chain,
            fundamentals=funds,
            iv_rank=None, iv_percentile=None,
            wheel_states=wheel_states,
            shares_positions=shares_pos,
            option_positions=option_pos,
            notes=notes,
        )
        # Pre-compute indicators so agents and dashboard share the same numbers
        snap = compute_indicators(symbol, history_daily, intraday)
        if snap is not None:
            ctx.indicators["technical"] = snap
        # Pre-compute IV rank (cheap, no LLM, surfaces it in --no-llm mode too)
        if chain is not None:
            from .strategy.options_math import approx_iv_rank, atm_iv_for_chain, expected_move
            iv_rank = approx_iv_rank(chain, history_daily)
            atm_iv = atm_iv_for_chain(chain)
            ctx.iv_rank = iv_rank
            front = chain.expiries[0] if chain.expiries else None
            em = expected_move(
                chain.spot_price, atm_iv or 0,
                (front - chain.fetched_at.date()).days if front else 0,
            )
            ctx.indicators["volatility"] = {
                "iv_rank": iv_rank, "atm_iv": atm_iv, "expected_move": em,
            }
        return ctx

    async def _run_analysts(self, ctxs: list[TickerContext]) -> dict[str, list[Signal]]:
        analysts = self._get_analysts()
        tasks = []
        for ctx in ctxs:
            for a in analysts.values():
                tasks.append(asyncio.create_task(_safe_analyze(a, ctx)))
        results = await asyncio.gather(*tasks)
        by_sym: dict[str, list[Signal]] = {}
        for sig in results:
            if sig is None:
                continue
            by_sym.setdefault(sig.symbol, []).append(sig)
        return by_sym

    async def _run_specialists(
        self,
        ctxs: list[TickerContext],
        snapshots: dict[str, AccountSnapshot],
    ) -> list[Proposal]:
        specialists = self._get_specialists()
        tasks = []
        for ctx in ctxs:
            for snap in snapshots.values():
                # Skip accounts where the action universe doesn't apply
                for s in specialists.values():
                    tasks.append(asyncio.create_task(_safe_propose(s, ctx, snap)))
        proposals: list[Proposal] = []
        for p in await asyncio.gather(*tasks):
            if p is None:
                continue
            proposals.append(p)
        return proposals

    @staticmethod
    def _rank(review: RiskReviewOutcome) -> list[Recommendation]:
        """Highest conviction first; break ties on annualized credit."""
        def key(r: Recommendation) -> tuple[float, float]:
            return (-r.conviction, -r.expected_credit_or_debit)
        return sorted(review.accepted, key=key)


async def _safe_analyze(agent, ctx: TickerContext) -> Signal | None:
    try:
        return await agent.analyze(ctx)
    except Exception as e:
        log.exception("%s analyze failed for %s: %s", getattr(agent, "name", "?"), ctx.symbol, e)
        return None


async def _safe_propose(agent, ctx: TickerContext, snap: AccountSnapshot) -> Proposal | None:
    try:
        return await agent.propose(ctx, snap)
    except Exception as e:
        log.exception(
            "%s propose failed for %s/%s: %s",
            getattr(agent, "name", "?"), ctx.symbol, snap.key, e,
        )
        return None
