"""Stock Agent — buy_shares / sell_shares / no-op decisions, wheel-aware."""
from __future__ import annotations

import logging
from datetime import date as _date

from ..portfolio.accounts import AccountSnapshot
from ..portfolio.types import Signal, TickerContext
from .specialist import (
    Proposal,
    SpecialistAgent,
    _max_pct,
    _render_account_state,
    _render_analyst_consensus,
    _render_market_snapshot,
    _render_wheel_position,
)

log = logging.getLogger(__name__)


class StockAgent(SpecialistAgent):
    name = "stock_agent"
    skill_filename = "stock.md"

    def build_user_prompt(self, ctx: TickerContext, snap: AccountSnapshot) -> str:
        return (
            f"Ticker: {ctx.symbol}  ({ctx.sector})\n"
            f"{_render_market_snapshot(ctx)}\n\n"
            f"{_render_account_state(snap, _max_pct(self.cfg, ctx.symbol, snap))}\n\n"
            f"{_render_wheel_position(ctx, snap.key)}\n\n"
            f"Analyst consensus:\n{_render_analyst_consensus(ctx)}\n\n"
            f"Per-ticker notes: {ctx.notes or '(none)'}\n"
        )

    def _signal_to_proposal(
        self, sig: Signal, ctx: TickerContext, snap: AccountSnapshot,
    ) -> Proposal | None:
        d = sig.data or {}
        action = d.get("action", "none")
        if action == "none" or action not in {"buy_shares", "sell_shares"}:
            return None
        shares = int(d.get("shares", 100))
        if shares != 100:
            log.debug("%s rejected non-100 share size: %s", self.name, shares)
            return None
        limit = d.get("limit_price")
        try:
            limit_f = float(limit) if limit is not None else float(ctx.quote.price)
        except (TypeError, ValueError):
            limit_f = float(ctx.quote.price)
        cost = shares * limit_f
        return Proposal(
            agent=self.name, account_key=snap.key, symbol=ctx.symbol,
            action=action, contracts_or_shares=shares,
            limit_price=round(limit_f, 2),
            expiry=None, strike=None, option_type=None,
            lane=d.get("trigger"),
            rationale=sig.rationale,
            conviction=sig.conviction,
            expected_credit_or_debit=-cost if action == "buy_shares" else cost,
        )
