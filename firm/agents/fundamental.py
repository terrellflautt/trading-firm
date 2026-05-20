"""Fundamental Analyst — balance sheet + earning power assessment for wheel suitability."""
from __future__ import annotations

import logging
from pathlib import Path

from ..portfolio.types import Signal, TickerContext
from .base import BaseAgent, load_skill

log = logging.getLogger(__name__)


SKILL_PATH = Path(__file__).resolve().parent / "skills" / "fundamental.md"


class FundamentalAnalyst(BaseAgent):
    name = "fundamental_analyst"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._role = load_skill(SKILL_PATH)

    def role_prompt(self) -> str:
        return self._role

    def build_user_prompt(self, ctx: TickerContext) -> str:
        f = ctx.fundamentals
        if f is None:
            return (
                f"Ticker: {ctx.symbol}\n"
                f"INSUFFICIENT_DATA — no fundamentals available. Return neutral / 0.2.\n"
            )

        def m(v: float | None, suffix: str = "") -> str:
            if v is None:
                return "—"
            if abs(v) >= 1e9:
                return f"${v/1e9:.2f}B{suffix}"
            if abs(v) >= 1e6:
                return f"${v/1e6:.2f}M{suffix}"
            if abs(v) >= 1e3:
                return f"${v/1e3:.2f}K{suffix}"
            return f"${v:.2f}{suffix}"

        runway = f.cash_runway_quarters
        runway_str = f"{runway:.1f} quarters" if runway is not None else "n/a (cashflow-positive or data missing)"

        lines = [
            f"Ticker: {ctx.symbol}  ({ctx.sector})",
            f"Market cap: {m(f.market_cap)}    Shares outstanding: {m(f.shares_outstanding)}",
            f"Cash: {m(f.cash)}    Total debt: {m(f.total_debt)}    Net cash: {m((f.cash or 0) - (f.total_debt or 0))}",
            f"Revenue TTM: {m(f.revenue_ttm)}    Net income TTM: {m(f.net_income_ttm)}",
            f"Operating CF TTM: {m(f.operating_cash_flow_ttm)}    Free CF TTM: {m(f.free_cash_flow_ttm)}",
            f"P/E: {f.pe_ratio:.1f}" if f.pe_ratio is not None else "P/E: —",
            f"Dividend yield: {f.dividend_yield*100:.2f}%" if f.dividend_yield is not None else "Dividend yield: —",
            f"Beta: {f.beta:.2f}" if f.beta is not None else "Beta: —",
            f"Cash runway: {runway_str}",
        ]
        if ctx.notes:
            lines += ["", f"Notes: {ctx.notes}"]
        return "\n".join(lines)

    async def analyze(self, ctx: TickerContext) -> Signal | None:
        prompt = self.build_user_prompt(ctx)
        raw = await self._call_llm(self.role_prompt(), prompt)
        sig = self._parse_signal(raw, ctx.symbol)
        if sig is not None:
            log.debug("%s %s -> %s (%.2f)", self.name, ctx.symbol, sig.direction.value, sig.conviction)
        return sig
