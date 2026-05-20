"""Risk Manager tests — every account rule must reject or accept correctly."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from firm.agents.risk_manager import RiskManager
from firm.config import (
    AccountsConfig,
    Config,
    DashboardConfig,
    DataConfig,
    FirmConfig,
    FirmMeta,
    LLMConfig,
    LoggingConfig,
    RiskConfig,
    ScanConfig,
    ScoutFilters,
    WatchlistConfig,
)
from firm.portfolio.accounts import AccountSnapshot
from firm.portfolio.types import (
    OptionType,
    Recommendation,
    SharesPosition,
)


# ─── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def config() -> Config:
    # Prefer .example templates (always include both account types) so the
    # test suite passes regardless of how the user personalised their real
    # configs via `firm init`.
    config_dir = Path(__file__).resolve().parent.parent / "config"
    firm_y = yaml.safe_load((config_dir / "firm.yaml").read_text())
    accts_path = config_dir / "accounts.yaml.example"
    if not accts_path.exists():
        accts_path = config_dir / "accounts.yaml"
    wl_path = config_dir / "watchlist.yaml.example"
    if not wl_path.exists():
        wl_path = config_dir / "watchlist.yaml"
    accts_y = yaml.safe_load(accts_path.read_text())
    wl_y = yaml.safe_load(wl_path.read_text())
    return Config(
        firm=FirmConfig(**firm_y),
        accounts=AccountsConfig(**accts_y),
        watchlist=WatchlistConfig(**wl_y),
        root=config_dir.parent,
    )


@pytest.fixture
def rm(config: Config) -> RiskManager:
    return RiskManager(config)


@pytest.fixture
def roth_snap(config: Config) -> AccountSnapshot:
    return AccountSnapshot(
        config=config.accounts.accounts["roth_ira"],
        key="roth_ira",
        shares_positions=[],
        open_options=[],
    )


@pytest.fixture
def cash_snap(config: Config) -> AccountSnapshot:
    return AccountSnapshot(
        config=config.accounts.accounts["cash"],
        key="cash",
        shares_positions=[],
        open_options=[],
    )


def _rec(**kwargs) -> Recommendation:
    defaults = dict(
        account_key="cash", symbol="INTC", action="buy_shares",
        contracts_or_shares=100, limit_price=25.0,
        expiry=None, strike=None, option_type=None,
        rationale="test", conviction=0.7,
        expected_credit_or_debit=-2500.0,
    )
    defaults.update(kwargs)
    return Recommendation(**defaults)


# ─── Rule 1: 100-share minimum ─────────────────────────────────────────────


def test_rejects_share_buy_under_100(rm: RiskManager, cash_snap: AccountSnapshot):
    rec = _rec(contracts_or_shares=50, limit_price=25.0)
    out = rm.review([rec], {"cash": cash_snap})
    assert not out.accepted
    assert "100 minimum" in out.rejected[0][1]


# ─── Rule 2: affordability ─────────────────────────────────────────────────


def test_rejects_unaffordable_share_buy(rm: RiskManager, cash_snap: AccountSnapshot):
    # cash account is $5k, no margin -> can afford ≤ $50/share for 100 shares
    rec = _rec(contracts_or_shares=100, limit_price=100.0)  # $10k
    out = rm.review([rec], {"cash": cash_snap})
    assert not out.accepted
    assert "insufficient free cash" in out.rejected[0][1]


def test_rejects_unaffordable_csp_collateral(rm: RiskManager, cash_snap: AccountSnapshot):
    # $200 strike on cash account: collateral $20k > $5k available
    rec = _rec(
        action="sell_csp", strike=200.0, contracts_or_shares=1,
        expiry=date.today() + timedelta(days=30), limit_price=2.50,
        option_type=OptionType.PUT,
    )
    out = rm.review([rec], {"cash": cash_snap})
    assert not out.accepted
    msg = out.rejected[0][1]
    # Either max-share-price or insufficient-cash rejection is correct
    assert "max share price" in msg or "insufficient free cash" in msg


def test_accepts_affordable_csp_on_cash(rm: RiskManager, cash_snap: AccountSnapshot):
    # $14 strike: $1400 collateral, within 30% cap ($1500) on $5k account
    rec = _rec(
        action="sell_csp", strike=14.0, contracts_or_shares=1,
        expiry=date.today() + timedelta(days=30),
        limit_price=0.60,  # rich premium so it passes the yield floor
        option_type=OptionType.PUT, expected_credit_or_debit=60.0,
    )
    out = rm.review([rec], {"cash": cash_snap})
    assert len(out.accepted) == 1, out.rejected


# ─── Rule 3: IRA constraints ───────────────────────────────────────────────


def test_rejects_naked_long_call_in_ira(rm: RiskManager, roth_snap: AccountSnapshot):
    rec = _rec(
        account_key="roth_ira", action="buy_call", strike=200.0,
        contracts_or_shares=1, limit_price=5.0,
        expiry=date.today() + timedelta(days=30),
        option_type=OptionType.CALL,
    )
    out = rm.review([rec], {"roth_ira": roth_snap})
    assert not out.accepted
    msg = out.rejected[0][1]
    assert "not permitted" in msg


# ─── Rule 4: covered call must be backed by shares ─────────────────────────


def test_rejects_uncovered_cc(rm: RiskManager, cash_snap: AccountSnapshot):
    rec = _rec(
        action="sell_cc", symbol="ACHR", strike=15.0, contracts_or_shares=1,
        expiry=date.today() + timedelta(days=30), limit_price=0.50,
        option_type=OptionType.CALL,
    )
    out = rm.review([rec], {"cash": cash_snap})
    assert not out.accepted
    assert "uncovered" in out.rejected[0][1]


def test_accepts_cc_when_shares_owned(rm: RiskManager, cash_snap: AccountSnapshot):
    cash_snap.shares_positions.append(SharesPosition(
        account_key="cash", symbol="ACHR",
        shares=100, total_cost=1000.0, premiums_collected=0.0,
        opened_at=datetime.now(), last_updated=datetime.now(),
    ))
    rec = _rec(
        action="sell_cc", symbol="ACHR", strike=12.0, contracts_or_shares=1,
        expiry=date.today() + timedelta(days=30), limit_price=0.30,
        option_type=OptionType.CALL, expected_credit_or_debit=30.0,
    )
    out = rm.review([rec], {"cash": cash_snap})
    assert len(out.accepted) == 1, out.rejected


# ─── Rule 5: CC strike must clear effective cost basis ─────────────────────


def test_rejects_cc_below_cost_basis(rm: RiskManager, cash_snap: AccountSnapshot):
    cash_snap.shares_positions.append(SharesPosition(
        account_key="cash", symbol="ACHR",
        shares=100, total_cost=1500.0,  # $15 basis
        premiums_collected=0.0,
        opened_at=datetime.now(), last_updated=datetime.now(),
    ))
    rec = _rec(
        action="sell_cc", symbol="ACHR", strike=12.0, contracts_or_shares=1,
        expiry=date.today() + timedelta(days=30), limit_price=0.30,
        option_type=OptionType.CALL, expected_credit_or_debit=30.0,
    )
    out = rm.review([rec], {"cash": cash_snap})
    assert not out.accepted
    assert "below effective cost basis" in out.rejected[0][1]


def test_premium_collected_lowers_effective_basis_enough_for_cc(
    rm: RiskManager, cash_snap: AccountSnapshot,
):
    # Bought 100 at $15 ($1500), collected $200 in premiums → effective basis $13
    # A $14 CC should now be allowed.
    cash_snap.shares_positions.append(SharesPosition(
        account_key="cash", symbol="ACHR",
        shares=100, total_cost=1500.0, premiums_collected=200.0,
        opened_at=datetime.now(), last_updated=datetime.now(),
    ))
    rec = _rec(
        action="sell_cc", symbol="ACHR", strike=14.0, contracts_or_shares=1,
        expiry=date.today() + timedelta(days=30), limit_price=0.30,
        option_type=OptionType.CALL, expected_credit_or_debit=30.0,
    )
    out = rm.review([rec], {"cash": cash_snap})
    assert len(out.accepted) == 1, out.rejected


# ─── Rule 6: concentration ─────────────────────────────────────────────────


def test_concentration_cap_rejects_overweight_position(
    rm: RiskManager, cash_snap: AccountSnapshot,
):
    # Cash account, $5k, 30% cap = $1500. Existing $1400 in ACHR + new $500
    # buy = $1900 > $1500.
    cash_snap.shares_positions.append(SharesPosition(
        account_key="cash", symbol="ACHR",
        shares=100, total_cost=1400.0, premiums_collected=0.0,
        opened_at=datetime.now(), last_updated=datetime.now(),
    ))
    rec = _rec(
        symbol="ACHR", action="buy_shares",
        contracts_or_shares=100, limit_price=5.0,
        expected_credit_or_debit=-500.0,
    )
    out = rm.review([rec], {"cash": cash_snap})
    assert not out.accepted
    assert "cap exceeded" in out.rejected[0][1]


# ─── Rule 8: premium yield floor ───────────────────────────────────────────


def test_rejects_csp_with_thin_premium(rm: RiskManager, cash_snap: AccountSnapshot):
    # $14 strike, 30 DTE, $0.05 premium = 0.36% return → 4.3% annualized < 15% floor
    rec = _rec(
        action="sell_csp", strike=14.0, contracts_or_shares=1,
        expiry=date.today() + timedelta(days=30), limit_price=0.05,
        option_type=OptionType.PUT, expected_credit_or_debit=5.0,
    )
    out = rm.review([rec], {"cash": cash_snap})
    assert not out.accepted
    assert "premium too thin" in out.rejected[0][1]


# ─── Informational warnings ────────────────────────────────────────────────


def test_assignment_risk_warning_added_for_short_dte(
    rm: RiskManager, cash_snap: AccountSnapshot,
):
    rec = _rec(
        action="sell_csp", strike=14.0, contracts_or_shares=1,
        expiry=date.today() + timedelta(days=3),  # ≤ 7 default threshold
        limit_price=0.40, option_type=OptionType.PUT,
        expected_credit_or_debit=40.0,
    )
    out = rm.review([rec], {"cash": cash_snap})
    assert len(out.accepted) == 1
    notes = out.accepted[0].risk_notes
    assert any("assignment risk" in n for n in notes)
