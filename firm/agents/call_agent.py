"""Call Agent — sell short CCs (primary) + buy LEAPS (rare)."""
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


class CallAgent(SpecialistAgent):
    name = "call_agent"
    skill_filename = "call.md"

    def build_user_prompt(self, ctx: TickerContext, snap: AccountSnapshot) -> str:
        return (
            f"Ticker: {ctx.symbol}  ({ctx.sector})\n"
            f"{_render_market_snapshot(ctx)}\n\n"
            f"{_render_account_state(snap, _max_pct(self.cfg, ctx.symbol, snap))}\n\n"
            f"{_render_wheel_position(ctx, snap.key)}\n\n"
            f"Analyst consensus:\n{_render_analyst_consensus(ctx)}\n\n"
            f"Options chain (front 3 expiries, near-ATM strikes):\n"
            f"{_front_chain_summary(ctx)}\n\n"
            f"Per-ticker notes: {ctx.notes or '(none)'}\n"
            f"Firm rules: target CC delta ≈ {self.cfg.firm.risk.target_cc_delta:.2f}, "
            f"min IV rank for CC selling ≈ {self.cfg.firm.risk.min_iv_rank_for_csp:.0f}, "
            f"min annualized premium {self.cfg.firm.risk.min_premium_pct_annualized:.0f}%.\n"
        )

    def _signal_to_proposal(
        self, sig: Signal, ctx: TickerContext, snap: AccountSnapshot,
    ) -> Proposal | None:
        d = sig.data or {}
        action = d.get("action", "none")
        if action not in {"sell_cc", "buy_call"}:
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
                limit * 100 * contracts * (1 if action == "sell_cc" else -1)
            )
        except (TypeError, ValueError):
            credit_f = limit * 100 * contracts * (1 if action == "sell_cc" else -1)
        return Proposal(
            agent=self.name, account_key=snap.key, symbol=ctx.symbol,
            action=action, contracts_or_shares=contracts,
            limit_price=round(limit, 2), expiry=expiry, strike=strike,
            option_type=OptionType.CALL, lane=d.get("lane"),
            rationale=sig.rationale, conviction=sig.conviction,
            expected_credit_or_debit=credit_f,
        )
