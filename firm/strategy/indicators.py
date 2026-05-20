"""Technical indicators: VWAP, ADX, RSI, MACD, ATR, plus support/resistance levels.

Built on pandas-ta-classic. Returns typed snapshots so the Technical Analyst
agent can render them in a prompt without re-doing pandas operations.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd
import pandas_ta_classic as ta

from ..portfolio.types import OHLCBar


@dataclass(frozen=True)
class TrendDirection:
    direction: Literal["up", "down", "flat"]
    strength: float  # 0..1


@dataclass(frozen=True)
class IndicatorSnapshot:
    """Latest values for the indicators we surface to agents."""
    symbol: str
    last_close: float

    # Trend
    sma_20: float | None
    sma_50: float | None
    ema_9: float | None

    # Momentum
    rsi_14: float | None
    macd: float | None
    macd_signal: float | None
    macd_hist: float | None

    # Volatility & risk
    atr_14: float | None
    bb_upper: float | None
    bb_lower: float | None
    bb_pct: float | None  # 0..1 — where price sits in the band

    # Volume / VWAP (intraday only)
    vwap: float | None

    # Trend strength
    adx_14: float | None
    di_plus: float | None
    di_minus: float | None

    # Support / resistance (rolling highs/lows)
    resistance_20: float | None
    support_20: float | None

    trend: TrendDirection

    def to_summary(self) -> str:
        """Compact one-line summary the agents can paste into prompts."""
        bits = []
        if self.rsi_14 is not None:
            bits.append(f"RSI14={self.rsi_14:.0f}")
        if self.adx_14 is not None:
            bits.append(f"ADX={self.adx_14:.0f}")
        if self.macd is not None and self.macd_signal is not None:
            cross = "above" if self.macd > self.macd_signal else "below"
            bits.append(f"MACD {cross} signal")
        if self.vwap is not None:
            rel = "above" if self.last_close > self.vwap else "below"
            bits.append(f"price {rel} VWAP ({self.vwap:.2f})")
        if self.support_20 is not None and self.resistance_20 is not None:
            bits.append(f"S/R: {self.support_20:.2f}/{self.resistance_20:.2f}")
        bits.append(f"trend={self.trend.direction}({self.trend.strength:.2f})")
        return " | ".join(bits)


def _bars_to_df(bars: list[OHLCBar]) -> pd.DataFrame:
    return pd.DataFrame([{
        "Open": b.open, "High": b.high, "Low": b.low,
        "Close": b.close, "Volume": b.volume,
        "ts": b.timestamp,
    } for b in bars]).set_index("ts")


def _last(series: pd.Series) -> float | None:
    if series is None or len(series) == 0:
        return None
    v = series.iloc[-1]
    if pd.isna(v):
        return None
    return float(v)


def compute_indicators(
    symbol: str,
    daily_bars: list[OHLCBar],
    intraday_bars: list[OHLCBar] | None = None,
) -> IndicatorSnapshot | None:
    """Compute all indicators from daily bars (with optional intraday for VWAP)."""
    if len(daily_bars) < 20:
        return None
    df = _bars_to_df(daily_bars)

    # Trend
    sma20 = ta.sma(df["Close"], length=20)
    sma50 = ta.sma(df["Close"], length=50) if len(df) >= 50 else None
    ema9 = ta.ema(df["Close"], length=9)

    # Momentum
    rsi = ta.rsi(df["Close"], length=14)
    macd_df = ta.macd(df["Close"], fast=12, slow=26, signal=9)
    macd_line = macd_signal = macd_hist = None
    if macd_df is not None and not macd_df.empty:
        macd_line = _last(macd_df.iloc[:, 0])
        macd_hist = _last(macd_df.iloc[:, 1])
        macd_signal = _last(macd_df.iloc[:, 2])

    # Volatility
    atr = ta.atr(df["High"], df["Low"], df["Close"], length=14)
    bb_df = ta.bbands(df["Close"], length=20, std=2)
    bb_upper = bb_lower = bb_pct = None
    if bb_df is not None and not bb_df.empty:
        bb_lower = _last(bb_df.iloc[:, 0])
        bb_upper = _last(bb_df.iloc[:, 2])
        if bb_lower and bb_upper and bb_upper > bb_lower:
            bb_pct = (df["Close"].iloc[-1] - bb_lower) / (bb_upper - bb_lower)

    # ADX / DI
    adx_df = ta.adx(df["High"], df["Low"], df["Close"], length=14)
    adx_val = di_plus = di_minus = None
    if adx_df is not None and not adx_df.empty:
        # pandas-ta-classic naming: ADX_14, DMP_14, DMN_14
        for col in adx_df.columns:
            if col.startswith("ADX"):
                adx_val = _last(adx_df[col])
            elif col.startswith("DMP"):
                di_plus = _last(adx_df[col])
            elif col.startswith("DMN"):
                di_minus = _last(adx_df[col])

    # Support / Resistance — rolling 20-day high/low excluding the current bar
    if len(df) >= 21:
        resistance_20 = float(df["High"].iloc[-21:-1].max())
        support_20 = float(df["Low"].iloc[-21:-1].min())
    else:
        resistance_20 = float(df["High"].iloc[:-1].max())
        support_20 = float(df["Low"].iloc[:-1].min())

    # VWAP from intraday only (daily VWAP is meaningless)
    vwap_val = None
    if intraday_bars and len(intraday_bars) > 5:
        idf = _bars_to_df(intraday_bars)
        # Group by date for session VWAP — take last session
        idf["date"] = idf.index.date if hasattr(idf.index, "date") else [t.date() for t in idf.index]
        last_date = idf["date"].iloc[-1]
        session = idf[idf["date"] == last_date]
        if len(session) >= 2:
            tp = (session["High"] + session["Low"] + session["Close"]) / 3.0
            vol = session["Volume"]
            cum_pv = (tp * vol).cumsum()
            cum_v = vol.cumsum()
            if cum_v.iloc[-1] > 0:
                vwap_val = float(cum_pv.iloc[-1] / cum_v.iloc[-1])

    trend = _classify_trend(_last(sma20), _last(sma50), df["Close"].iloc[-1], adx_val)

    return IndicatorSnapshot(
        symbol=symbol,
        last_close=float(df["Close"].iloc[-1]),
        sma_20=_last(sma20),
        sma_50=_last(sma50) if sma50 is not None else None,
        ema_9=_last(ema9),
        rsi_14=rsi.iloc[-1] if rsi is not None and not rsi.empty and not pd.isna(rsi.iloc[-1]) else None,
        macd=macd_line, macd_signal=macd_signal, macd_hist=macd_hist,
        atr_14=_last(atr),
        bb_upper=bb_upper, bb_lower=bb_lower, bb_pct=bb_pct,
        vwap=vwap_val,
        adx_14=adx_val, di_plus=di_plus, di_minus=di_minus,
        resistance_20=resistance_20, support_20=support_20,
        trend=trend,
    )


def _classify_trend(
    sma20: float | None, sma50: float | None, price: float, adx: float | None,
) -> TrendDirection:
    """Heuristic: price vs. SMAs + ADX magnitude."""
    if sma20 is None:
        return TrendDirection(direction="flat", strength=0.0)
    above_20 = price > sma20
    above_50 = sma50 is None or price > sma50
    sma_aligned = sma50 is None or (sma20 > sma50 if above_20 else sma20 < sma50)
    if above_20 and above_50 and sma_aligned:
        direction = "up"
    elif (not above_20) and (not above_50) and sma_aligned:
        direction = "down"
    else:
        direction = "flat"
    # ADX > 25 is a strong trend by Wilder's convention
    strength = 0.5
    if adx is not None:
        strength = float(np.clip(adx / 50.0, 0.0, 1.0))
    return TrendDirection(direction=direction, strength=strength)
