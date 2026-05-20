"""Wheel state machine.

Single source of truth for transitions between wheel states. Every transition
is a pure function of (current_state, event) → new_state. Side effects
(ledger writes, premium credits, cost-basis adjustments) live in `apply()`.

States:
    CASH               No position. Free to open CSP or buy shares directly.
    SHORT_PUT          Open CSP (assignment risk + premium collection).
    LONG_SHARES        Hold ≥100 shares, no CC open. Can sell CC or buy more.
    COVERED            Hold shares AND have a CC open.
    CALLED_AWAY_PENDING Short CC expired ITM, shares awaiting settlement → CASH.

The wheel does NOT include LONG_SHARES with no CC permanently — the strategy
is to always cycle (CSP → shares → CC → repeat). LONG_SHARES is a transient
state between assignment and the next CC sale.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ..portfolio.ledger import Ledger
from ..portfolio.types import (
    OptionPosition,
    OptionSide,
    OptionType,
    SharesPosition,
    WheelState,
)


class WheelEvent(str, Enum):
    """Discrete events that advance wheel state.

    Two flavors:
    - Trader-initiated: open/close/roll positions. We log when the user marks them filled.
    - Market-initiated: expiry outcomes, assignment, called away. Detected from option close.
    """

    SELL_CSP = "sell_csp"
    CSP_EXPIRED_OTM = "csp_expired_otm"
    CSP_ASSIGNED = "csp_assigned"
    CSP_BOUGHT_BACK = "csp_bought_back"
    BUY_SHARES_DIRECT = "buy_shares_direct"
    SELL_CC = "sell_cc"
    CC_EXPIRED_OTM = "cc_expired_otm"
    CC_CALLED_AWAY = "cc_called_away"
    CC_BOUGHT_BACK = "cc_bought_back"
    SHARES_SOLD = "shares_sold"           # User exited at a loss / thesis change
    SETTLEMENT_COMPLETE = "settlement_complete"  # CALLED_AWAY_PENDING → CASH


# Transition table: (current_state, event) -> next_state
# Any (state, event) not in this map is illegal.
TRANSITIONS: dict[tuple[WheelState, WheelEvent], WheelState] = {
    # From CASH
    (WheelState.CASH, WheelEvent.SELL_CSP): WheelState.SHORT_PUT,
    (WheelState.CASH, WheelEvent.BUY_SHARES_DIRECT): WheelState.LONG_SHARES,

    # From SHORT_PUT
    (WheelState.SHORT_PUT, WheelEvent.CSP_EXPIRED_OTM): WheelState.CASH,
    (WheelState.SHORT_PUT, WheelEvent.CSP_ASSIGNED): WheelState.LONG_SHARES,
    (WheelState.SHORT_PUT, WheelEvent.CSP_BOUGHT_BACK): WheelState.CASH,

    # From LONG_SHARES
    (WheelState.LONG_SHARES, WheelEvent.SELL_CC): WheelState.COVERED,
    (WheelState.LONG_SHARES, WheelEvent.SHARES_SOLD): WheelState.CASH,
    # Buying more shares stays in LONG_SHARES (lot is the relevant entity, not state)
    (WheelState.LONG_SHARES, WheelEvent.BUY_SHARES_DIRECT): WheelState.LONG_SHARES,

    # From COVERED
    (WheelState.COVERED, WheelEvent.CC_EXPIRED_OTM): WheelState.LONG_SHARES,
    (WheelState.COVERED, WheelEvent.CC_BOUGHT_BACK): WheelState.LONG_SHARES,
    (WheelState.COVERED, WheelEvent.CC_CALLED_AWAY): WheelState.CALLED_AWAY_PENDING,

    # From CALLED_AWAY_PENDING
    (WheelState.CALLED_AWAY_PENDING, WheelEvent.SETTLEMENT_COMPLETE): WheelState.CASH,
}


class IllegalTransition(Exception):
    """Raised when an event cannot be applied to the current state."""


def next_state(current: WheelState, event: WheelEvent) -> WheelState:
    """Pure function: look up the next state. No side effects."""
    try:
        return TRANSITIONS[(current, event)]
    except KeyError:
        raise IllegalTransition(
            f"Cannot apply event {event.value!r} to state {current.value!r}"
        ) from None


# ─────────────────────────────────────────────────────────────────────────────
# Side-effecting application of events to the ledger
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WheelEventPayload:
    """Bundle of inputs an event needs to produce ledger side effects."""
    account_key: str
    symbol: str
    event: WheelEvent
    occurred_at: datetime
    # CSP / CC details
    strike: float | None = None
    expiry_iso: str | None = None
    contracts: int = 0
    premium_per_share: float = 0.0  # Positive
    # Direct shares operations
    shares: int = 0
    price_per_share: float = 0.0
    # Close-side fields for buy-back / cover transactions
    close_price_per_share: float = 0.0
    # Optional pointer to an existing option position id for close events
    option_position_id: int | None = None


def apply(ledger: Ledger, payload: WheelEventPayload) -> WheelState:
    """Apply a wheel event end-to-end: update ledger, return new state.

    This is the single mutation point for wheel state. Agents recommend;
    the user marks fills; this function records the reality into the ledger.
    """
    current = ledger.get_wheel_state(payload.account_key, payload.symbol)
    new = next_state(current, payload.event)

    # Side-effect dispatch — each branch is small and explicit on purpose.
    match payload.event:
        case WheelEvent.SELL_CSP:
            _open_short_option(ledger, payload, OptionType.PUT)
        case WheelEvent.SELL_CC:
            _open_short_option(ledger, payload, OptionType.CALL)
        case WheelEvent.CSP_EXPIRED_OTM | WheelEvent.CC_EXPIRED_OTM:
            _close_open_option(ledger, payload, close_price=0.0, expired_otm=True)
        case WheelEvent.CSP_BOUGHT_BACK | WheelEvent.CC_BOUGHT_BACK:
            _close_open_option(ledger, payload, close_price=payload.close_price_per_share)
        case WheelEvent.CSP_ASSIGNED:
            _close_open_option(ledger, payload, close_price=0.0, expired_otm=False)
            _take_assignment(ledger, payload)
        case WheelEvent.CC_CALLED_AWAY:
            _close_open_option(ledger, payload, close_price=0.0, expired_otm=False)
            # Shares aren't removed yet; SETTLEMENT_COMPLETE handles that.
        case WheelEvent.SETTLEMENT_COMPLETE:
            ledger.delete_shares(payload.account_key, payload.symbol)
        case WheelEvent.BUY_SHARES_DIRECT:
            _add_shares(ledger, payload)
        case WheelEvent.SHARES_SOLD:
            ledger.delete_shares(payload.account_key, payload.symbol)

    ledger.set_wheel_state(payload.account_key, payload.symbol, new)
    return new


# ─── Internal side-effect helpers ───────────────────────────────────────────


def _open_short_option(ledger: Ledger, p: WheelEventPayload, opt_type: OptionType) -> None:
    if p.strike is None or p.expiry_iso is None or p.contracts <= 0:
        raise ValueError(f"{p.event.value} requires strike, expiry, contracts")
    from datetime import date as _date
    opt = OptionPosition(
        account_key=p.account_key, symbol=p.symbol,
        expiry=_date.fromisoformat(p.expiry_iso), strike=p.strike,
        type=opt_type, side=OptionSide.SHORT,
        contracts=p.contracts, entry_price=p.premium_per_share,
        opened_at=p.occurred_at,
    )
    opt_id = ledger.insert_option(opt)
    credit = p.premium_per_share * 100 * p.contracts
    source = "csp_open" if opt_type == OptionType.PUT else "cc_open"
    ledger.add_premium(p.account_key, p.symbol, credit, source, opt_id)

    # Premium reduces effective cost basis on existing shares (for CCs) and
    # is bookkept against future basis (for CSPs that may get assigned).
    if opt_type == OptionType.CALL:
        existing = ledger.get_shares(p.account_key, p.symbol)
        if existing is not None:
            updated = SharesPosition(
                account_key=existing.account_key, symbol=existing.symbol,
                shares=existing.shares, total_cost=existing.total_cost,
                premiums_collected=existing.premiums_collected + credit,
                opened_at=existing.opened_at, last_updated=p.occurred_at,
            )
            ledger.upsert_shares(updated)


def _close_open_option(
    ledger: Ledger, p: WheelEventPayload, close_price: float, expired_otm: bool = False,
) -> None:
    open_opts = ledger.open_options_for(p.account_key, p.symbol)
    target = None
    if p.option_position_id is not None:
        target = next((o for o in open_opts if id(o) == p.option_position_id), None)
    if target is None and open_opts:
        # Heuristic: most recently opened of matching strike/expiry, else first open.
        if p.strike is not None and p.expiry_iso is not None:
            from datetime import date as _date
            exp = _date.fromisoformat(p.expiry_iso)
            target = next(
                (o for o in open_opts if o.strike == p.strike and o.expiry == exp),
                open_opts[-1],
            )
        else:
            target = open_opts[-1]
    if target is None:
        return  # Nothing open to close — caller may be syncing state only
    # Realized PnL for a SHORT: entry credit - close cost. expired_otm => keep full credit.
    contracts = target.contracts
    entry_credit = target.entry_price * 100 * contracts
    close_cost = (0.0 if expired_otm else close_price * 100 * contracts)
    pnl = entry_credit - close_cost

    # Find the row id for this option (open_options_for returns rows but not IDs in dataclass).
    # We re-query to grab the row id.
    with ledger.connect() as conn:
        row = conn.execute(
            """SELECT id FROM option_positions
               WHERE account_key = ? AND symbol = ? AND closed_at IS NULL
               ORDER BY opened_at DESC LIMIT 1""",
            (p.account_key, p.symbol),
        ).fetchone()
    if row is not None:
        ledger.close_option(row["id"], close_price, pnl)


def _take_assignment(ledger: Ledger, p: WheelEventPayload) -> None:
    """CSP assignment: receive 100 × contracts shares at strike."""
    if p.strike is None or p.contracts <= 0:
        raise ValueError("CSP_ASSIGNED requires strike and contracts")
    shares_received = 100 * p.contracts
    cost = p.strike * shares_received

    existing = ledger.get_shares(p.account_key, p.symbol)
    if existing is None:
        # New position: premium collected from the assigned CSP offsets basis.
        # The premium was already recorded against the symbol; we surface it here too.
        new_pos = SharesPosition(
            account_key=p.account_key, symbol=p.symbol,
            shares=shares_received, total_cost=cost,
            premiums_collected=ledger.premiums_collected(p.account_key, p.symbol),
            opened_at=p.occurred_at, last_updated=p.occurred_at,
        )
        ledger.upsert_shares(new_pos)
    else:
        merged = SharesPosition(
            account_key=existing.account_key, symbol=existing.symbol,
            shares=existing.shares + shares_received,
            total_cost=existing.total_cost + cost,
            premiums_collected=existing.premiums_collected,
            opened_at=existing.opened_at, last_updated=p.occurred_at,
        )
        ledger.upsert_shares(merged)


def _add_shares(ledger: Ledger, p: WheelEventPayload) -> None:
    if p.shares <= 0 or p.price_per_share <= 0:
        raise ValueError("BUY_SHARES_DIRECT requires positive shares and price")
    cost = p.shares * p.price_per_share
    existing = ledger.get_shares(p.account_key, p.symbol)
    if existing is None:
        ledger.upsert_shares(SharesPosition(
            account_key=p.account_key, symbol=p.symbol,
            shares=p.shares, total_cost=cost, premiums_collected=0.0,
            opened_at=p.occurred_at, last_updated=p.occurred_at,
        ))
    else:
        merged = SharesPosition(
            account_key=existing.account_key, symbol=existing.symbol,
            shares=existing.shares + p.shares,
            total_cost=existing.total_cost + cost,
            premiums_collected=existing.premiums_collected,
            opened_at=existing.opened_at, last_updated=p.occurred_at,
        )
        ledger.upsert_shares(merged)
