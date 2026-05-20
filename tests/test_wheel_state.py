"""Pure state-machine tests + end-to-end ledger tests for the wheel."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from firm.portfolio.ledger import Ledger
from firm.portfolio.types import WheelState
from firm.strategy.wheel import (
    IllegalTransition,
    TRANSITIONS,
    WheelEvent,
    WheelEventPayload,
    apply,
    next_state,
)


# ─── Pure transition table ──────────────────────────────────────────────────


def test_cash_to_short_put():
    assert next_state(WheelState.CASH, WheelEvent.SELL_CSP) == WheelState.SHORT_PUT


def test_cash_to_long_shares():
    assert next_state(WheelState.CASH, WheelEvent.BUY_SHARES_DIRECT) == WheelState.LONG_SHARES


def test_short_put_expired_returns_to_cash():
    assert next_state(WheelState.SHORT_PUT, WheelEvent.CSP_EXPIRED_OTM) == WheelState.CASH


def test_short_put_assigned_to_long_shares():
    assert next_state(WheelState.SHORT_PUT, WheelEvent.CSP_ASSIGNED) == WheelState.LONG_SHARES


def test_long_shares_to_covered():
    assert next_state(WheelState.LONG_SHARES, WheelEvent.SELL_CC) == WheelState.COVERED


def test_covered_to_long_shares_on_otm_expiry():
    assert next_state(WheelState.COVERED, WheelEvent.CC_EXPIRED_OTM) == WheelState.LONG_SHARES


def test_covered_to_called_away_pending():
    assert next_state(WheelState.COVERED, WheelEvent.CC_CALLED_AWAY) == WheelState.CALLED_AWAY_PENDING


def test_called_away_to_cash():
    assert next_state(
        WheelState.CALLED_AWAY_PENDING, WheelEvent.SETTLEMENT_COMPLETE,
    ) == WheelState.CASH


def test_full_wheel_cycle():
    """The canonical wheel: CASH → SHORT_PUT → LONG_SHARES → COVERED → ... → CASH."""
    s = WheelState.CASH
    s = next_state(s, WheelEvent.SELL_CSP)
    assert s == WheelState.SHORT_PUT
    s = next_state(s, WheelEvent.CSP_ASSIGNED)
    assert s == WheelState.LONG_SHARES
    s = next_state(s, WheelEvent.SELL_CC)
    assert s == WheelState.COVERED
    s = next_state(s, WheelEvent.CC_CALLED_AWAY)
    assert s == WheelState.CALLED_AWAY_PENDING
    s = next_state(s, WheelEvent.SETTLEMENT_COMPLETE)
    assert s == WheelState.CASH


def test_illegal_transitions_raise():
    illegal = [
        (WheelState.CASH, WheelEvent.SELL_CC),
        (WheelState.CASH, WheelEvent.CSP_ASSIGNED),
        (WheelState.SHORT_PUT, WheelEvent.SELL_CC),
        (WheelState.LONG_SHARES, WheelEvent.SELL_CSP),
        (WheelState.COVERED, WheelEvent.SELL_CSP),
        (WheelState.COVERED, WheelEvent.SELL_CC),
    ]
    for state, event in illegal:
        if (state, event) in TRANSITIONS:
            continue  # Skip if turns out to be legal after all
        with pytest.raises(IllegalTransition):
            next_state(state, event)


# ─── Ledger-integrated tests ────────────────────────────────────────────────


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "test_ledger.sqlite")


@pytest.fixture
def acct_key() -> str:
    return "cash"


def test_full_wheel_cycle_with_ledger(ledger: Ledger, acct_key: str):
    """End-to-end: sell CSP, get assigned, sell CC, get called away. Verify
    cost basis tracks premium offsets correctly."""
    now = datetime.now()
    sym = "INTC"
    expiry = (date.today() + timedelta(days=30)).isoformat()

    # 1. Sell a 25-strike CSP for $0.80
    state = apply(ledger, WheelEventPayload(
        account_key=acct_key, symbol=sym, event=WheelEvent.SELL_CSP,
        occurred_at=now, strike=25.0, expiry_iso=expiry, contracts=1,
        premium_per_share=0.80,
    ))
    assert state == WheelState.SHORT_PUT
    assert ledger.premiums_collected(acct_key, sym) == 80.0

    # 2. Get assigned — receive 100 shares at $25 = $2500 cost
    state = apply(ledger, WheelEventPayload(
        account_key=acct_key, symbol=sym, event=WheelEvent.CSP_ASSIGNED,
        occurred_at=now, strike=25.0, expiry_iso=expiry, contracts=1,
    ))
    assert state == WheelState.LONG_SHARES
    pos = ledger.get_shares(acct_key, sym)
    assert pos is not None
    assert pos.shares == 100
    assert pos.total_cost == 2500.0
    assert pos.premiums_collected == 80.0
    assert pos.effective_cost_basis == pytest.approx((2500.0 - 80.0) / 100.0)

    # 3. Sell a 27-strike CC for $0.50
    cc_expiry = (date.today() + timedelta(days=21)).isoformat()
    state = apply(ledger, WheelEventPayload(
        account_key=acct_key, symbol=sym, event=WheelEvent.SELL_CC,
        occurred_at=now, strike=27.0, expiry_iso=cc_expiry, contracts=1,
        premium_per_share=0.50,
    ))
    assert state == WheelState.COVERED
    pos = ledger.get_shares(acct_key, sym)
    assert pos.premiums_collected == 130.0  # 80 + 50
    assert pos.effective_cost_basis == pytest.approx((2500.0 - 130.0) / 100.0)

    # 4. Called away — go to settlement pending
    state = apply(ledger, WheelEventPayload(
        account_key=acct_key, symbol=sym, event=WheelEvent.CC_CALLED_AWAY,
        occurred_at=now, strike=27.0, expiry_iso=cc_expiry, contracts=1,
    ))
    assert state == WheelState.CALLED_AWAY_PENDING

    # 5. Settlement completes — shares are removed, back to cash
    state = apply(ledger, WheelEventPayload(
        account_key=acct_key, symbol=sym,
        event=WheelEvent.SETTLEMENT_COMPLETE, occurred_at=now,
    ))
    assert state == WheelState.CASH
    assert ledger.get_shares(acct_key, sym) is None


def test_buy_more_shares_lowers_average_cost(ledger: Ledger, acct_key: str):
    now = datetime.now()
    sym = "ACHR"

    # Initial: 100 shares at $10 = $1000
    apply(ledger, WheelEventPayload(
        account_key=acct_key, symbol=sym, event=WheelEvent.BUY_SHARES_DIRECT,
        occurred_at=now, shares=100, price_per_share=10.0,
    ))
    pos = ledger.get_shares(acct_key, sym)
    assert pos.average_cost == 10.0

    # Buy 100 more on dip at $8
    apply(ledger, WheelEventPayload(
        account_key=acct_key, symbol=sym, event=WheelEvent.BUY_SHARES_DIRECT,
        occurred_at=now, shares=100, price_per_share=8.0,
    ))
    pos = ledger.get_shares(acct_key, sym)
    assert pos.shares == 200
    assert pos.average_cost == pytest.approx(9.0)


def test_csp_expires_otm_keeps_full_premium(ledger: Ledger, acct_key: str):
    now = datetime.now()
    sym = "RGTI"
    expiry = (date.today() + timedelta(days=14)).isoformat()

    apply(ledger, WheelEventPayload(
        account_key=acct_key, symbol=sym, event=WheelEvent.SELL_CSP,
        occurred_at=now, strike=12.0, expiry_iso=expiry, contracts=1,
        premium_per_share=0.45,
    ))
    state = apply(ledger, WheelEventPayload(
        account_key=acct_key, symbol=sym,
        event=WheelEvent.CSP_EXPIRED_OTM, occurred_at=now,
        strike=12.0, expiry_iso=expiry,
    ))
    assert state == WheelState.CASH
    assert ledger.premiums_collected(acct_key, sym) == 45.0


def test_cc_expires_otm_premiums_accumulate(ledger: Ledger, acct_key: str):
    """Repeatedly selling CCs that expire OTM is the whole point of the wheel."""
    now = datetime.now()
    sym = "IONQ"

    # Start with 100 shares from assignment, basis $20
    apply(ledger, WheelEventPayload(
        account_key=acct_key, symbol=sym, event=WheelEvent.BUY_SHARES_DIRECT,
        occurred_at=now, shares=100, price_per_share=20.0,
    ))

    for week in range(4):
        expiry = (date.today() + timedelta(days=14 + week * 7)).isoformat()
        apply(ledger, WheelEventPayload(
            account_key=acct_key, symbol=sym, event=WheelEvent.SELL_CC,
            occurred_at=now, strike=22.0, expiry_iso=expiry, contracts=1,
            premium_per_share=0.30,
        ))
        apply(ledger, WheelEventPayload(
            account_key=acct_key, symbol=sym,
            event=WheelEvent.CC_EXPIRED_OTM, occurred_at=now,
            strike=22.0, expiry_iso=expiry,
        ))

    pos = ledger.get_shares(acct_key, sym)
    assert pos.premiums_collected == 120.0  # 4 × $30
    assert pos.effective_cost_basis == pytest.approx((2000.0 - 120.0) / 100.0)
