"""Technical analyst — reads computed indicators and emits a directional Signal."""
from __future__ import annotations

import logging
from textwrap import dedent

from ..portfolio.types import Signal, TickerContext
from ..strategy.indicators import IndicatorSnapshot, compute_indicators
from .base import BaseAgent

log = logging.getLogger(__name__)


class TechnicalAnalyst(BaseAgent):
    name = "technical_analyst"

    def role_prompt(self) -> str:
        return dedent(
            """\
            You are the Technical Analyst on a wheel-strategy trading firm. You work
            with the user's deterministic indicator engine — the numbers in the user
            prompt are computed from real OHLCV data; do not recompute them.

            Your job: read the indicator snapshot and produce a directional signal
            for the next 1-4 weeks (the typical wheel-cycle horizon). You are not
            picking strikes or sizing positions — Risk Manager and Portfolio Manager
            do that. You are answering: "is this ticker setting up for a rally, a
            grind sideways, or a roll-over?"

            Reasoning checklist:
              - Trend direction (price vs SMA20/SMA50, slope alignment)
              - Trend strength (ADX > 25 = trending, < 20 = chop)
              - Momentum (RSI extremes, MACD cross relative to signal)
              - Position in the Bollinger band (bb_pct near 1 = stretched up;
                near 0 = stretched down — both can mean exhaustion)
              - Proximity to 20-day support/resistance
              - VWAP relationship for intraday context

            Output a SINGLE JSON object, no prose, in this exact schema:

            {
              "direction": "strong_bull" | "bull" | "neutral" | "bear" | "strong_bear",
              "conviction": 0.0 to 1.0,
              "rationale": "2-3 sentences max, plain English, cite specific numbers",
              "data": {
                "key_level": "the nearest S or R level that matters",
                "what_would_flip_me": "what one indicator change would change your call"
              }
            }

            Calibration:
              - strong_bull/bear = conviction ≥ 0.75 AND multiple indicators aligned
              - bull/bear = directional bias but one or two signals dissent
              - neutral = chop or conflicting signals; honest is better than fake conviction

            Be concise. The Portfolio Manager will read this alongside 5 other signals.
            """
        ).strip()

    def build_user_prompt(self, ctx: TickerContext) -> str:
        snap: IndicatorSnapshot | None = ctx.indicators.get("technical")
        if snap is None:
            snap = compute_indicators(ctx.symbol, ctx.history_daily, ctx.history_intraday)
            if snap is not None:
                ctx.indicators["technical"] = snap

        if snap is None:
            return f"INSUFFICIENT_DATA for {ctx.symbol} — return direction=neutral, conviction=0.0."

        lines = [
            f"Ticker: {ctx.symbol}  ({ctx.sector})",
            f"Last close: ${snap.last_close:.2f}    Day move: {ctx.quote.day_change_pct:+.2f}%    Volume: {ctx.quote.volume:,}",
            "",
            "Indicators:",
            f"  SMA20={_fmt(snap.sma_20)}  SMA50={_fmt(snap.sma_50)}  EMA9={_fmt(snap.ema_9)}",
            f"  RSI14={_fmt(snap.rsi_14, 0)}  MACD={_fmt(snap.macd, 3)} vs signal {_fmt(snap.macd_signal, 3)} (hist {_fmt(snap.macd_hist, 3)})",
            f"  ATR14={_fmt(snap.atr_14)}  Bollinger {_fmt(snap.bb_lower)}/{_fmt(snap.bb_upper)} (pct={_fmt(snap.bb_pct, 2)})",
            f"  ADX14={_fmt(snap.adx_14, 1)}  DI+={_fmt(snap.di_plus, 1)} DI-={_fmt(snap.di_minus, 1)}",
            f"  20d S/R: support={_fmt(snap.support_20)} resistance={_fmt(snap.resistance_20)}",
            f"  VWAP (session): {_fmt(snap.vwap)}",
            f"  Trend: {snap.trend.direction} (strength {snap.trend.strength:.2f})",
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
