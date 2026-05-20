"""Specialist agents (Stock / Call / Put) — propose concrete trade actions.

Each specialist emits a Proposal which the Portfolio Manager turns into a
Recommendation. Specialists see:
  - all analyst Signals (consensus + per-agent rationales)
  - the current wheel state and positions in the target account
  - the options chain (Call/Put agents)
  - precomputed analytics (IV rank, indicator snapshot, expected move)

They do NOT see the entire watchlist — they're focused on one (ticker, account).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Any

from ..portfolio.accounts import AccountSnapshot
from ..portfolio.types import (
    OptionType,
    SharesPosition,
    Signal,
    TickerContext,
    WheelState,
)
from ..strategy.options_math import bs_delta
from .base import BaseAgent, load_skill

log = logging.getLogger(__name__)

SKILL_DIR = Path(__file__).resolve().parent / "skills"


def _max_pct(cfg, symbol: str, snap: AccountSnapshot) -> float:
    """Resolve effective per-ticker position cap percentage."""
    entry = cfg.watchlist.by_symbol(symbol)
    if entry is not None and entry.max_position_pct is not None:
        return entry.max_position_pct
    return snap.config.max_position_pct


@dataclass(frozen=True)
class Proposal:
    """A specialist's concrete trade idea — converted to a Recommendation by the PM."""
    agent: str
    account_key: str
    symbol: str
    action: str               # 'buy_shares' | 'sell_shares' | 'sell_csp' | 'sell_cc' | 'buy_call' | 'buy_put' | 'none'
    contracts_or_shares: int
    limit_price: float | None
    expiry: _date | None
    strike: float | None
    option_type: OptionType | None
    lane: str | None          # 'covered_call' | 'leaps' | 'cash_secured_put' | 'long_put_hedge' | 'wheel-entry' | etc.
    rationale: str
    conviction: float
    expected_credit_or_debit: float
    extra: dict[str, Any] = field(default_factory=dict)


class SpecialistAgent(BaseAgent):
    """Common path: render context for the LLM and parse its proposal back out."""

    skill_filename: str = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.skill_filename:
            raise NotImplementedError(f"{type(self).__name__} must set skill_filename")
        self._role = load_skill(SKILL_DIR / self.skill_filename)

    def role_prompt(self) -> str:
        return self._role

    async def propose(self, ctx: TickerContext, snap: AccountSnapshot) -> Proposal | None:
        prompt = self.build_user_prompt(ctx, snap)
        raw = await self._call_llm(self.role_prompt(), prompt)
        sig = self._parse_signal(raw, ctx.symbol)
        if sig is None:
            return None
        return self._signal_to_proposal(sig, ctx, snap)

    def build_user_prompt(self, ctx: TickerContext, snap: AccountSnapshot) -> str:  # noqa: ARG002
        raise NotImplementedError

    def _signal_to_proposal(  # noqa: ARG002
        self, sig: Signal, ctx: TickerContext, snap: AccountSnapshot,
    ) -> Proposal | None:
        raise NotImplementedError

    # ─── BaseAgent expects analyze(); specialists use propose() instead ──
    async def analyze(self, ctx: TickerContext) -> Signal | None:  # pragma: no cover
        raise NotImplementedError("Specialists use propose(ctx, snap), not analyze(ctx)")

    def build_user_prompt(self, ctx: TickerContext) -> str:  # type: ignore[override] # pragma: no cover
        raise NotImplementedError


# ─── Shared prompt fragments ────────────────────────────────────────────────


def _render_analyst_consensus(ctx: TickerContext) -> str:
    if not ctx.signals:
        return "(no analyst signals yet)"
    lines = []
    for s in ctx.signals:
        lines.append(f"  - [{s.agent}] {s.direction.value} (conv {s.conviction:.2f}): {s.rationale}")
    return "\n".join(lines)


def _render_wheel_position(ctx: TickerContext, account_key: str) -> str:
    state = ctx.wheel_states.get(account_key, WheelState.CASH)
    pos = ctx.shares_positions.get(account_key)
    opts = ctx.option_positions.get(account_key, [])
    lines = [f"Wheel state: {state.value}"]
    if pos is not None:
        lines.append(
            f"Shares held: {pos.shares} @ avg ${pos.average_cost:.2f}, "
            f"effective basis ${pos.effective_cost_basis:.2f} "
            f"(premiums collected ${pos.premiums_collected:.2f})"
        )
    else:
        lines.append("Shares held: 0")
    if opts:
        opt_descs = [
            f"{o.side.value} {o.contracts}x {o.type.value} ${o.strike:.2f} exp {o.expiry}"
            for o in opts
        ]
        lines.append("Open options: " + "; ".join(opt_descs))
    else:
        lines.append("Open options: none")
    return "\n".join(lines)


def _render_account_state(snap: AccountSnapshot, ticker_max_position_pct: float | None = None) -> str:
    cfg = snap.config
    cap_pct = ticker_max_position_pct or cfg.max_position_pct
    cap_dollars = cfg.buying_power * (cap_pct / 100.0)
    return (
        f"Account: {snap.key} ({cfg.display_name})\n"
        f"Capital: ${cfg.capital:,.2f}  Buying power: ${cfg.buying_power:,.2f}  "
        f"Free cash: ${snap.free_cash:,.2f}\n"
        f"Max share price (100-share rule): ${cfg.max_share_price:,.2f}\n"
        f"Per-ticker position cap: {cap_pct:.0f}% = ${cap_dollars:,.2f}. "
        f"For a CSP, strike × 100 must fit under this cap.\n"
        f"Allowed actions: {', '.join(a.value for a in cfg.allowed_actions)}"
    )


def _render_market_snapshot(ctx: TickerContext) -> str:
    q = ctx.quote
    vol_data = ctx.indicators.get("volatility", {}) or {}
    iv_rank = vol_data.get("iv_rank")
    em = vol_data.get("expected_move")
    tech = ctx.indicators.get("technical")
    parts = [
        f"Spot: ${q.price:.2f}    Day Δ: {q.day_change_pct:+.2f}%    Volume: {q.volume:,}",
        f"IV rank: {iv_rank:.0f}" if iv_rank is not None else "IV rank: —",
        f"1-std expected move (front exp): ${em:.2f}" if em is not None else "Expected move: —",
    ]
    if tech is not None:
        parts.append(f"Trend: {tech.trend.direction} (strength {tech.trend.strength:.2f})  |  {tech.to_summary()}")
    return "\n".join(parts)


def _front_chain_summary(
    ctx: TickerContext,
    max_strike_ceiling: float | None = None,
    strikes_per_expiry: int = 10,
) -> str:
    """Render a slice of the options chain for specialist agents.

    If `max_strike_ceiling` is provided (e.g. position-cap collateral ceiling),
    we bias the strike selection so the agent sees enough strikes BELOW that
    ceiling to make valid CSP picks — not just near-ATM strikes that may exceed it.
    """
    chain = ctx.options_chain
    if chain is None or not chain.expiries:
        return "(no options chain)"
    out: list[str] = []
    spot = chain.spot_price

    for exp in chain.expiries[:3]:
        puts = chain.puts(exp)
        calls = chain.calls(exp)
        if not puts and not calls:
            continue
        all_strikes = sorted({c.strike for c in puts + calls})
        if not all_strikes:
            continue

        # Build the strike selection.
        if max_strike_ceiling is not None and max_strike_ceiling < spot:
            # Cap-constrained mode: ensure we surface strikes BELOW the ceiling.
            below_ceiling = [s for s in all_strikes if s <= max_strike_ceiling]
            above_ceiling = [s for s in all_strikes if s > max_strike_ceiling]
            # Take the top N strikes below the ceiling (closest to it from below)
            # and a few strikes above for context.
            picked = list(below_ceiling[-max(strikes_per_expiry - 3, 4):]) + above_ceiling[:3]
            picked = sorted(set(picked))
        else:
            # Default: strikes nearest to ATM, balanced around it
            picked = sorted(
                all_strikes, key=lambda s: abs(s - spot),
            )[:strikes_per_expiry]
            picked.sort()

        dte = (exp - _date.today()).days
        if max_strike_ceiling is not None and max_strike_ceiling < spot:
            out.append(
                f"  Expiry {exp} ({dte} DTE) — strikes ≤ ${max_strike_ceiling:.2f} are within position cap:"
            )
        else:
            out.append(f"  Expiry {exp} ({dte} DTE):")
        for k in picked:
            p = next((c for c in puts if c.strike == k), None)
            c_ = next((c for c in calls if c.strike == k), None)
            p_str = f"P ${p.mid:.2f} iv={p.implied_volatility*100:.0f}% oi={p.open_interest}" if p else "P —"
            c_str = f"C ${c_.mid:.2f} iv={c_.implied_volatility*100:.0f}% oi={c_.open_interest}" if c_ else "C —"
            p_delta = bs_delta(spot, k, p.implied_volatility, p.dte, OptionType.PUT) if p else 0
            c_delta = bs_delta(spot, k, c_.implied_volatility, c_.dte, OptionType.CALL) if c_ else 0
            within = ""
            if max_strike_ceiling is not None and k <= max_strike_ceiling:
                within = " ✓within-cap"
            out.append(
                f"    K=${k:.2f}{within}  {p_str} (Δ{p_delta:+.2f})  {c_str} (Δ{c_delta:+.2f})"
            )
    return "\n".join(out) if out else "(empty chain)"
