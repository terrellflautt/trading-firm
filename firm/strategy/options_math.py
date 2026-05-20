"""Options math: IV rank/percentile, delta-based strike selection, expected move, premium yields.

Notes on the data we have to work with:
- yfinance gives us IV per contract but **not** the underlying's IV history.
- Barchart's free pages publish IV rank/percentile; we'll scrape that separately.
- As a fallback, we approximate IV rank from the current ATM IV vs. 52w realized
  volatility range. This is the "approx_iv_rank" path.
- yfinance does NOT publish greeks. We compute delta from a Black-Scholes-with-
  zero-rate approximation for the strike picker — good enough for picking the
  ~0.25 delta contract, not for live risk.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Literal

import numpy as np
import pandas as pd

from ..portfolio.types import OHLCBar, OptionContract, OptionsChain, OptionType

NORM = lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))  # noqa: E731


# ─── Volatility ─────────────────────────────────────────────────────────────


def realized_volatility_30d(bars: list[OHLCBar]) -> float | None:
    """Annualized 30-day realized vol from close-to-close returns."""
    if len(bars) < 30:
        return None
    closes = pd.Series([b.close for b in bars[-31:]])
    rets = np.log(closes / closes.shift(1)).dropna()
    if len(rets) < 2:
        return None
    return float(rets.std() * math.sqrt(252))


def realized_volatility_52w_range(bars: list[OHLCBar]) -> tuple[float, float] | None:
    """Min and max of rolling 30-day realized vol over ~52 weeks. (low, high)."""
    if len(bars) < 60:
        return None
    closes = pd.Series([b.close for b in bars])
    rets = np.log(closes / closes.shift(1)).dropna()
    rolling = rets.rolling(window=30).std() * math.sqrt(252)
    rolling = rolling.dropna()
    if rolling.empty:
        return None
    return float(rolling.min()), float(rolling.max())


def approx_iv_rank(chain: OptionsChain, daily_bars: list[OHLCBar]) -> float | None:
    """Fallback IV rank when Barchart unavailable.

    Uses the nearest-expiry ATM IV (avg of ATM put + call) vs. the 52w realized
    vol range. NOT a true IV rank — but directionally useful: high values still
    mean "options are pricey relative to recent realized."
    """
    atm_iv = atm_iv_for_chain(chain)
    if atm_iv is None:
        return None
    rng = realized_volatility_52w_range(daily_bars)
    if rng is None:
        return None
    lo, hi = rng
    if hi <= lo:
        return None
    rank = (atm_iv - lo) / (hi - lo) * 100.0
    return float(np.clip(rank, 0.0, 100.0))


def atm_iv_for_chain(chain: OptionsChain) -> float | None:
    if not chain.expiries:
        return None
    near = chain.expiries[0]
    puts = chain.puts(near)
    calls = chain.calls(near)
    if not puts and not calls:
        return None

    def closest(contracts: list[OptionContract]) -> OptionContract | None:
        if not contracts:
            return None
        return min(contracts, key=lambda c: abs(c.strike - chain.spot_price))

    p = closest(puts)
    c = closest(calls)
    ivs = [x.implied_volatility for x in (p, c) if x and x.implied_volatility > 0]
    if not ivs:
        return None
    return float(np.mean(ivs))


# ─── Strike picker ──────────────────────────────────────────────────────────


def bs_delta(spot: float, strike: float, iv: float, dte: int, opt_type: OptionType) -> float:
    """Black-Scholes delta with r=0, q=0. Good enough for ranking strikes by delta."""
    if iv <= 0 or dte <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    t = dte / 365.0
    d1 = (math.log(spot / strike) + 0.5 * iv * iv * t) / (iv * math.sqrt(t))
    if opt_type == OptionType.CALL:
        return NORM(d1)
    return NORM(d1) - 1.0


def pick_strike_by_delta(
    chain: OptionsChain,
    target_delta: float,
    opt_type: OptionType,
    expiry: date | None = None,
    min_strike: float | None = None,
    max_strike: float | None = None,
) -> OptionContract | None:
    """Pick the contract whose computed delta is closest to target_delta.

    For puts, target_delta should be positive (e.g. 0.25 means ~0.25 short put).
    Internally we use |delta| for comparison.
    """
    if expiry is None:
        if not chain.expiries:
            return None
        # Default to the first expiry that has at least 14 DTE (avoids weeklies for wheel)
        expiry = next(
            (e for e in chain.expiries if (e - date.today()).days >= 14),
            chain.expiries[0],
        )

    contracts = (chain.puts(expiry) if opt_type == OptionType.PUT else chain.calls(expiry))
    if not contracts:
        return None

    filtered = [
        c for c in contracts
        if (min_strike is None or c.strike >= min_strike)
        and (max_strike is None or c.strike <= max_strike)
        and c.implied_volatility > 0
        and c.dte > 0
    ]
    if not filtered:
        return None

    def score(c: OptionContract) -> float:
        d = abs(bs_delta(chain.spot_price, c.strike, c.implied_volatility, c.dte, opt_type))
        # Penalize illiquid contracts: bid-ask spread > 25% of mid
        liq_penalty = 0.0
        mid = c.mid
        if mid > 0 and c.bid > 0 and c.ask > 0:
            spread_pct = (c.ask - c.bid) / mid
            if spread_pct > 0.25:
                liq_penalty = 0.05
        return abs(d - target_delta) + liq_penalty

    return min(filtered, key=score)


def pick_csp_strike(
    chain: OptionsChain,
    target_delta: float = 0.25,
    happy_buy_price: float | None = None,
) -> OptionContract | None:
    """Pick a cash-secured put strike. Caps at happy_buy_price if provided."""
    return pick_strike_by_delta(
        chain, target_delta, OptionType.PUT, max_strike=happy_buy_price,
    )


def pick_cc_strike(
    chain: OptionsChain,
    target_delta: float = 0.25,
    min_strike: float | None = None,
) -> OptionContract | None:
    """Pick a covered-call strike. min_strike enforces 'above cost basis'."""
    return pick_strike_by_delta(
        chain, target_delta, OptionType.CALL, min_strike=min_strike,
    )


# ─── Yields and expected move ───────────────────────────────────────────────


@dataclass(frozen=True)
class PremiumYield:
    premium_per_share: float
    capital_required: float          # Per contract
    return_pct: float                # premium / capital_required
    annualized_pct: float            # return_pct * (365 / dte)
    dte: int


def csp_yield(contract: OptionContract) -> PremiumYield:
    """How rich is this CSP? Premium ÷ collateral required, annualized."""
    capital = contract.strike * 100.0
    premium = contract.mid * 100.0
    pct = (premium / capital) if capital > 0 else 0.0
    dte = max(1, contract.dte)
    annualized = pct * (365.0 / dte)
    return PremiumYield(
        premium_per_share=contract.mid,
        capital_required=capital,
        return_pct=pct * 100.0,
        annualized_pct=annualized * 100.0,
        dte=dte,
    )


def cc_yield(contract: OptionContract, cost_basis_per_share: float) -> PremiumYield:
    """For a CC, capital tied up is the cost basis of the underlying shares."""
    capital = cost_basis_per_share * 100.0
    premium = contract.mid * 100.0
    pct = (premium / capital) if capital > 0 else 0.0
    dte = max(1, contract.dte)
    annualized = pct * (365.0 / dte)
    return PremiumYield(
        premium_per_share=contract.mid,
        capital_required=capital,
        return_pct=pct * 100.0,
        annualized_pct=annualized * 100.0,
        dte=dte,
    )


def expected_move(spot: float, iv: float, dte: int) -> float | None:
    """1-std expected dollar move over `dte` days."""
    if iv <= 0 or dte <= 0 or spot <= 0:
        return None
    t = dte / 365.0
    return spot * iv * math.sqrt(t)


# ─── Wheel-entry decision ───────────────────────────────────────────────────


WheelEntryMode = Literal["sell_csp", "buy_shares", "wait"]


def recommend_wheel_entry(
    iv_rank: float | None,
    setup_quality: float,
    min_iv_rank_for_csp: float = 30.0,
    setup_quality_floor: float = 0.4,
) -> WheelEntryMode:
    """Decide whether a new wheel should start with a CSP or by buying shares.

    Logic:
      - If IV rank is rich (>= threshold), prefer CSP — premium is the edge.
      - Else if the technical setup is strong, buy shares directly.
      - Else wait — neither premium nor a great entry.
    """
    if iv_rank is not None and iv_rank >= min_iv_rank_for_csp:
        return "sell_csp"
    if setup_quality >= setup_quality_floor:
        return "buy_shares"
    return "wait"
