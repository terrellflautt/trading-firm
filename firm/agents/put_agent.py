"""Put Agent — sell CSPs (primary) + buy long-put hedge (rare)."""
from __future__ import annotations

import logging
from datetime import date as _date

from ..portfolio.accounts import AccountSnapshot
from ..portfolio.types import OptionType, Signal, TickerContext
from .specialist import (
    Proposal,
    SpecialistAgent,
    _front_chain_summary,
    _max_pct,
    _render_account_state,
    _render_analyst_consensus,
    _render_market_snapshot,
    _render_wheel_position,
)

log = logging.getLogger(__name__)


class PutAgent(SpecialistAgent):
    name = "put_agent"
    skill_filename = "put.md"

    def build_user_prompt(self, ctx: TickerContext, snap: AccountSnapshot) -> str:
        entry = self.cfg.watchlist.by_symbol(ctx.symbol)
        happy = entry.target_csp_strike if entry else None
        happy_str = f"${happy:.2f}" if happy else "(none set)"
        # Cap-aware strike ceiling for CSP collateral
        cap_pct = _max_pct(self.cfg, ctx.symbol, snap)
        cap_dollars = snap.config.buying_power * (cap_pct / 100.0)
        strike_ceiling = min(cap_dollars / 100.0, snap.config.max_share_price)
        return (
            f"Ticker: {ctx.symbol}  ({ctx.sector})\n"
            f"{_render_market_snapshot(ctx)}\n\n"
            f"{_render_account_state(snap, cap_pct)}\n\n"
            f"{_render_wheel_position(ctx, snap.key)}\n\n"
            f"Analyst consensus:\n{_render_analyst_consensus(ctx)}\n\n"
            f"Options chain (front 3 expiries, biased to show strikes within cap):\n"
            f"{_front_chain_summary(ctx, max_strike_ceiling=strike_ceiling)}\n\n"
            f"Per-ticker notes: {ctx.notes or '(none)'}\n"
            f"User's happy-buy strike for {ctx.symbol}: {happy_str}\n"
            f"Firm rules: target CSP delta ≈ {self.cfg.firm.risk.target_csp_delta:.2f}, "
            f"min IV rank for CSP ≈ {self.cfg.firm.risk.min_iv_rank_for_csp:.0f}, "
            f"min annualized premium {self.cfg.firm.risk.min_premium_pct_annualized:.0f}%.\n"
            f"Position-cap CSP strike ceiling: ${strike_ceiling:.2f} "
            f"(strikes ≤ this fit under the per-ticker cap).\n"
        )

    def _signal_to_proposal(
        self, sig: Signal, ctx: TickerContext, snap: AccountSnapshot,
    ) -> Proposal | None:
        d = sig.data or {}
        action = d.get("action", "none")
        if action not in {"sell_csp", "buy_put"}:
            return None
        try:
            contracts = int(d.get("contracts", 1))
            strike = float(d["strike"])
            limit = float(d["limit_price"])
            expiry = _date.fromisoformat(d["expiry_iso"])
        except (TypeError, ValueError, KeyError) as e:
            log.debug("%s incomplete proposal data: %s (%s)", self.name, d, e)
            return None
        credit = d.get("expected_credit_or_debit")
        try:
            credit_f = float(credit) if credit is not None else (
                limit * 100 * contracts * (1 if action == "sell_csp" else -1)
            )
        except (TypeError, ValueError):
            credit_f = limit * 100 * contracts * (1 if action == "sell_csp" else -1)
        return Proposal(
            agent=self.name, account_key=snap.key, symbol=ctx.symbol,
            action=action, contracts_or_shares=contracts,
            limit_price=round(limit, 2), expiry=expiry, strike=strike,
            option_type=OptionType.PUT, lane=d.get("lane"),
            rationale=sig.rationale, conviction=sig.conviction,
            expected_credit_or_debit=credit_f,
        )
