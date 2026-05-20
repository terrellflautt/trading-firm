"""Volatility analyst — assesses options 'richness' for the wheel.

For wheel mechanics, IV is the edge: rich IV = fat premiums = sell options.
Cheap IV = poor premium ÷ collateral; buy shares instead (if setup is good)
or sit on cash.
"""
from __future__ import annotations

import logging
from textwrap import dedent

from ..portfolio.types import Signal, TickerContext
from ..strategy.options_math import (
    approx_iv_rank,
    atm_iv_for_chain,
    expected_move,
    realized_volatility_30d,
)
from .base import BaseAgent

log = logging.getLogger(__name__)


class VolatilityAnalyst(BaseAgent):
    name = "volatility_analyst"

    def role_prompt(self) -> str:
        return dedent(
            """\
            You are the Volatility Analyst on a wheel-strategy trading firm. Your
            job is to tell the Portfolio Manager whether **this ticker's options
            are pricey, fair, or cheap right now**.

            Reasoning model: the wheel works best when IV rank is elevated (rich
            premiums) AND the underlying does not have a binary event imminent
            (earnings, FDA, etc.) that justifies the high IV. Selling options
            into elevated IV that "deserves" to be that high is just selling
            insurance to a buyer who is going to be right.

            What you have in the prompt:
              - approximate IV rank (0-100 — fallback from realized vol range)
              - ATM IV (current options market expectation, annualized)
              - 30-day realized vol (annualized)
              - 1-std expected move over the front expiry
              - chain summary: front-expiry put/call counts, ATM bid-ask spreads
              - any user-supplied notes about catalysts

            Output a SINGLE JSON object, no prose, in this schema:

            {
              "direction": "strong_bull" | "bull" | "neutral" | "bear" | "strong_bear",
              "conviction": 0.0 to 1.0,
              "rationale": "2-3 sentences — explicitly call IV rank rich/cheap/fair",
              "data": {
                "iv_assessment": "rich" | "fair" | "cheap",
                "favored_wheel_action": "sell_csp" | "sell_cc_if_long" | "wait" | "buy_shares_only",
                "warning": "any caveats — earnings imminent, illiquid chain, etc."
              }
            }

            "Direction" semantics here are SLIGHTLY different from other agents:
            interpret it as "should we lean into this ticker's premium right now":
              - strong_bull = IV rank > 70 AND clean setup, big edge selling premium
              - bull = IV rank 40-70, decent edge
              - neutral = IV rank 20-40, take it or leave it
              - bear = IV rank < 20, premium is poor; only buy stock if setup is great
              - strong_bear = IV rank < 10 AND ugly chain liquidity — pass entirely

            Be honest about uncertainty when IV rank is approximated (no Barchart data).
            """
        ).strip()

    def build_user_prompt(self, ctx: TickerContext) -> str:
        if ctx.options_chain is None:
            return f"INSUFFICIENT_DATA for {ctx.symbol} — no options chain. Return neutral / 0.0."

        chain = ctx.options_chain
        atm_iv = atm_iv_for_chain(chain)
        rv30 = realized_volatility_30d(ctx.history_daily)
        # Use precomputed IV rank if present (from Barchart scraper), else approximate
        iv_rank = ctx.iv_rank
        if iv_rank is None:
            iv_rank = approx_iv_rank(chain, ctx.history_daily)

        front_expiry = chain.expiries[0] if chain.expiries else None
        em = expected_move(chain.spot_price, atm_iv or 0, (front_expiry - chain.fetched_at.date()).days if front_expiry else 0)

        front_puts = chain.puts(front_expiry) if front_expiry else []
        front_calls = chain.calls(front_expiry) if front_expiry else []
        # ATM bid-ask spread
        spread_pct = None
        if front_puts and front_calls:
            mids = []
            for c in (front_puts, front_calls):
                atm = min(c, key=lambda x: abs(x.strike - chain.spot_price))
                if atm.ask > 0 and atm.bid > 0:
                    mid = (atm.ask + atm.bid) / 2.0
                    mids.append((atm.ask - atm.bid) / mid * 100.0)
            if mids:
                spread_pct = sum(mids) / len(mids)

        # Record into context for downstream agents
        ctx.iv_rank = iv_rank
        ctx.indicators["volatility"] = {
            "iv_rank": iv_rank, "atm_iv": atm_iv, "rv30": rv30,
            "expected_move": em, "spread_pct": spread_pct,
        }

        rank_origin = "Barchart" if ctx.iv_rank is not None and "barchart" in ctx.indicators else "approx (rv range)"

        lines = [
            f"Ticker: {ctx.symbol}  ({ctx.sector})",
            f"Spot: ${chain.spot_price:.2f}",
            f"IV rank: {_fmt(iv_rank, 0)} (source: {rank_origin})",
            f"ATM IV (annualized): {_fmt(atm_iv, 2)}    30d RV (annualized): {_fmt(rv30, 2)}",
            f"Front expiry: {front_expiry} ({(front_expiry - chain.fetched_at.date()).days if front_expiry else '?'} DTE)",
            f"1-std expected move to front expiry: ${_fmt(em)}",
            f"Chain: {len(front_puts)} puts / {len(front_calls)} calls on front expiry",
            f"ATM bid-ask spread: {_fmt(spread_pct, 1)}%",
        ]
        if ctx.notes:
            lines += ["", f"Context notes: {ctx.notes}"]
        return "\n".join(lines)

    async def analyze(self, ctx: TickerContext) -> Signal | None:
        prompt = self.build_user_prompt(ctx)
        raw = await self._call_llm(self.role_prompt(), prompt)
        sig = self._parse_signal(raw, ctx.symbol)
        if sig is not None:
            log.debug("%s %s -> %s (%.2f)", self.name, ctx.symbol, sig.direction.value, sig.conviction)
        return sig


def _fmt(v: float | None, decimals: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v:.{decimals}f}"
