"""Core domain types shared across the firm.

Kept in their own module so agents, strategy, data, and dashboard can all
import without circular dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any

from ..utils.time import utc_now


class WheelState(str, Enum):
    """State of the wheel for a (ticker, account) pair."""

    CASH = "cash"                      # No position; free to open CSP or buy shares
    SHORT_PUT = "short_put"            # Open CSP waiting for expiry / assignment
    LONG_SHARES = "long_shares"        # Hold 100+ shares, no CC open
    COVERED = "covered"                # Hold shares AND have CC open
    CALLED_AWAY_PENDING = "called_pending"  # CC expired ITM, awaiting settlement


class OptionType(str, Enum):
    PUT = "put"
    CALL = "call"


class OptionSide(str, Enum):
    SHORT = "short"   # We sold the option (CSP or CC)
    LONG = "long"     # We bought the option (hedge or directional)


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    prev_close: float
    day_open: float
    day_high: float
    day_low: float
    volume: int
    avg_volume_30d: int | None
    timestamp: datetime

    @property
    def day_change_pct(self) -> float:
        if self.prev_close == 0:
            return 0.0
        return (self.price - self.prev_close) / self.prev_close * 100.0


@dataclass(frozen=True)
class OHLCBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class OptionContract:
    """A single strike on an option chain."""
    symbol: str               # Underlying
    expiry: date
    strike: float
    type: OptionType
    bid: float
    ask: float
    last: float
    volume: int
    open_interest: int
    implied_volatility: float
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.last

    @property
    def dte(self) -> int:
        return (self.expiry - date.today()).days


@dataclass(frozen=True)
class OptionsChain:
    symbol: str
    fetched_at: datetime
    spot_price: float
    expiries: list[date]
    contracts: list[OptionContract]

    def puts(self, expiry: date | None = None) -> list[OptionContract]:
        out = [c for c in self.contracts if c.type == OptionType.PUT]
        if expiry is not None:
            out = [c for c in out if c.expiry == expiry]
        return sorted(out, key=lambda c: (c.expiry, c.strike))

    def calls(self, expiry: date | None = None) -> list[OptionContract]:
        out = [c for c in self.contracts if c.type == OptionType.CALL]
        if expiry is not None:
            out = [c for c in out if c.expiry == expiry]
        return sorted(out, key=lambda c: (c.expiry, c.strike))


@dataclass(frozen=True)
class Fundamentals:
    symbol: str
    market_cap: float | None
    shares_outstanding: float | None
    cash: float | None
    total_debt: float | None
    revenue_ttm: float | None
    net_income_ttm: float | None
    free_cash_flow_ttm: float | None
    operating_cash_flow_ttm: float | None
    pe_ratio: float | None
    dividend_yield: float | None
    beta: float | None
    fetched_at: datetime

    @property
    def cash_runway_quarters(self) -> float | None:
        """Quarters of operating cash burn the company has left (None if profitable/missing data)."""
        if self.cash is None or self.operating_cash_flow_ttm is None:
            return None
        if self.operating_cash_flow_ttm >= 0:
            return None  # Cash-flow positive — runway is infinite for our purposes
        quarterly_burn = abs(self.operating_cash_flow_ttm) / 4.0
        if quarterly_burn == 0:
            return None
        return self.cash / quarterly_burn


# ─────────────────────────────────────────────────────────────────────────────
# Positions
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class SharesPosition:
    """A long shares position in a specific account.

    Cost basis tracks lot-level adjustments from share buys AND from premiums
    collected (premiums reduce effective basis for the wheel).
    """
    account_key: str
    symbol: str
    shares: int
    total_cost: float           # Cumulative dollars spent acquiring (after premium offsets)
    premiums_collected: float   # Lifetime premium credited to this position
    opened_at: datetime
    last_updated: datetime

    @property
    def average_cost(self) -> float:
        if self.shares == 0:
            return 0.0
        return self.total_cost / self.shares

    @property
    def effective_cost_basis(self) -> float:
        """Cost basis after premium offsets — the floor for CC strike selection."""
        if self.shares == 0:
            return 0.0
        return max(0.0, (self.total_cost - self.premiums_collected) / self.shares)


@dataclass
class OptionPosition:
    """An open option contract in a specific account."""
    account_key: str
    symbol: str                # Underlying
    expiry: date
    strike: float
    type: OptionType
    side: OptionSide
    contracts: int              # Always positive; side encodes direction
    entry_price: float          # Per-share premium (so credit = entry_price * 100 * contracts)
    opened_at: datetime
    # Set when the position closes (expiry, buy-back, assignment, exercise)
    closed_at: datetime | None = None
    close_price: float | None = None
    realized_pnl: float | None = None

    @property
    def credit_received(self) -> float:
        """Total premium credited at open (positive for SHORT, negative for LONG)."""
        sign = 1 if self.side == OptionSide.SHORT else -1
        return sign * self.entry_price * 100 * self.contracts

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


# ─────────────────────────────────────────────────────────────────────────────
# Per-ticker analysis context (passed between agents)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class TickerContext:
    """Everything agents need to know about one ticker at one moment in time."""
    symbol: str
    sector: str
    quote: Quote
    history_daily: list[OHLCBar]
    history_intraday: list[OHLCBar] | None
    options_chain: OptionsChain | None
    fundamentals: Fundamentals | None
    iv_rank: float | None
    iv_percentile: float | None
    # Computed indicators (populated by Technical analyst)
    indicators: dict[str, Any] = field(default_factory=dict)
    # Per-account wheel state and positions
    wheel_states: dict[str, WheelState] = field(default_factory=dict)
    shares_positions: dict[str, SharesPosition] = field(default_factory=dict)
    option_positions: dict[str, list[OptionPosition]] = field(default_factory=dict)
    # Agent outputs accumulate here
    signals: list[Signal] = field(default_factory=list)
    notes: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Agent outputs
# ─────────────────────────────────────────────────────────────────────────────


class SignalDirection(str, Enum):
    STRONG_BULL = "strong_bull"
    BULL = "bull"
    NEUTRAL = "neutral"
    BEAR = "bear"
    STRONG_BEAR = "strong_bear"


@dataclass(frozen=True)
class Signal:
    """A single agent's assessment of a ticker, fed into the Portfolio Manager."""
    agent: str
    symbol: str
    direction: SignalDirection
    conviction: float         # 0.0 - 1.0
    rationale: str            # Short, human-readable
    data: dict[str, Any] = field(default_factory=dict)
    issued_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True)
class Recommendation:
    """A concrete trade ticket the Portfolio Manager outputs."""
    account_key: str
    symbol: str
    action: str               # Mirrors Action enum value
    contracts_or_shares: int
    limit_price: float
    expiry: date | None
    strike: float | None
    option_type: OptionType | None
    rationale: str
    conviction: float
    expected_credit_or_debit: float    # Positive = credit, negative = debit
    risk_notes: list[str] = field(default_factory=list)
    issued_at: datetime = field(default_factory=utc_now)

    def order_ticket(self) -> str:
        """Plain-text ticket the dashboard's 'copy' button outputs."""
        parts = [
            f"[{self.account_key.upper()}] {self.action.upper()}",
            f"{self.contracts_or_shares}x {self.symbol}",
        ]
        if self.expiry and self.strike and self.option_type:
            parts.append(
                f"{self.expiry.strftime('%Y-%m-%d')} "
                f"{self.strike:.2f}{self.option_type.value[0].upper()}"
            )
        parts.append(f"@ ${self.limit_price:.2f} LIMIT")
        return "  ".join(parts)
