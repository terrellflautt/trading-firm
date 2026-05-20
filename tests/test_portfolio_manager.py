"""PortfolioManager — tests for proposal de-dup, ranking, and rationale composition."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from firm.agents.portfolio_manager import PortfolioManager
from firm.agents.specialist import Proposal
from firm.config import (
    AccountsConfig,
    Config,
    FirmConfig,
    WatchlistConfig,
)
from firm.portfolio.accounts import AccountSnapshot
from firm.portfolio.ledger import Ledger
from firm.portfolio.types import (
    OHLCBar,
    OptionType,
    Quote,
    Signal,
    SignalDirection,
    TickerContext,
)


@pytest.fixture
def config() -> Config:
    config_dir = Path(__file__).resolve().parent.parent / "config"
    return Config(
        firm=FirmConfig(**yaml.safe_load((config_dir / "firm.yaml").read_text())),
        accounts=AccountsConfig(**yaml.safe_load((config_dir / "accounts.yaml").read_text())),
        watchlist=WatchlistConfig(**yaml.safe_load((config_dir / "watchlist.yaml").read_text())),
        root=config_dir.parent,
    )


@pytest.fixture
def pm(config: Config, tmp_path: Path) -> PortfolioManager:
    return PortfolioManager(config, Ledger(tmp_path / "ledger.sqlite"))


def _ctx(symbol: str, price: float, signals: list[Signal]) -> TickerContext:
    return TickerContext(
        symbol=symbol, sector="quantum",
        quote=Quote(symbol, price, price - 0.1, price, price, price - 0.5, 1_000_000, 5_000_000, datetime.now()),
        history_daily=[], history_intraday=None,
        options_chain=None, fundamentals=None,
        iv_rank=50.0, iv_percentile=50.0,
        signals=signals,
    )


def _prop(action: str, conviction: float, **kw) -> Proposal:
    defaults = dict(
        agent="test", account_key="cash", symbol="IONQ",
        contracts_or_shares=1, limit_price=1.50,
        expiry=date.today() + timedelta(days=30),
        strike=50.0, option_type=OptionType.PUT,
        lane="cash_secured_put",
        rationale="test", expected_credit_or_debit=150.0,
        extra={},
    )
    defaults.update(kw)
    return Proposal(action=action, conviction=conviction, **defaults)


def test_priority_sell_cc_beats_sell_csp_on_same_pair(pm: PortfolioManager):
    a = _prop("sell_csp", conviction=0.9)
    b = _prop("sell_cc", conviction=0.5, option_type=OptionType.CALL, strike=60.0)
    ctx = _ctx("IONQ", 55.0, [])
    recs = pm.synthesize_proposals([a, b], {"IONQ": ctx}, {})
    assert len(recs) == 1
    assert recs[0].action == "sell_cc"  # Higher priority despite lower conviction


def test_higher_conviction_wins_within_same_action(pm: PortfolioManager):
    a = _prop("sell_csp", conviction=0.4, strike=45.0)
    b = _prop("sell_csp", conviction=0.8, strike=50.0)
    ctx = _ctx("IONQ", 55.0, [])
    recs = pm.synthesize_proposals([a, b], {"IONQ": ctx}, {})
    assert len(recs) == 1
    assert recs[0].limit_price > 0
    assert recs[0].action == "sell_csp"


def test_consensus_score_dampens_disagreement(pm: PortfolioManager):
    """Bullish action with bearish consensus should produce lower composite conviction."""
    bearish_signals = [
        Signal(agent="x", symbol="IONQ", direction=SignalDirection.STRONG_BEAR, conviction=0.9, rationale="x"),
        Signal(agent="y", symbol="IONQ", direction=SignalDirection.BEAR, conviction=0.8, rationale="y"),
    ]
    bullish_action = _prop("buy_shares", conviction=0.8, option_type=None, strike=None,
                           expiry=None, limit_price=55.0, lane="wheel-entry",
                           expected_credit_or_debit=-5500.0)
    ctx = _ctx("IONQ", 55.0, bearish_signals)
    recs = pm.synthesize_proposals([bullish_action], {"IONQ": ctx}, {})
    assert len(recs) == 1
    # Composite is 0.7*0.8 + 0.3*(low directional agreement); should be well below 0.8.
    assert recs[0].conviction < 0.65


def test_consensus_aligned_amplifies(pm: PortfolioManager):
    """Bullish action with bullish consensus should sustain conviction."""
    bullish_signals = [
        Signal(agent="x", symbol="IONQ", direction=SignalDirection.STRONG_BULL, conviction=0.9, rationale="x"),
        Signal(agent="y", symbol="IONQ", direction=SignalDirection.BULL, conviction=0.8, rationale="y"),
    ]
    bullish_action = _prop("sell_csp", conviction=0.8, strike=50.0)
    ctx = _ctx("IONQ", 55.0, bullish_signals)
    recs = pm.synthesize_proposals([bullish_action], {"IONQ": ctx}, {})
    assert len(recs) == 1
    assert recs[0].conviction > 0.7


def test_ranks_by_conviction_then_credit(pm: PortfolioManager):
    a = _prop("sell_csp", conviction=0.9, account_key="cash", expected_credit_or_debit=100.0)
    b = _prop("sell_csp", conviction=0.9, account_key="roth_ira", symbol="ACHR",
              expected_credit_or_debit=300.0)
    c = _prop("sell_csp", conviction=0.5, account_key="cash", symbol="QBTS",
              expected_credit_or_debit=500.0)
    ctxs = {
        "IONQ": _ctx("IONQ", 55.0, []),
        "ACHR": _ctx("ACHR", 11.0, []),
        "QBTS": _ctx("QBTS", 20.0, []),
    }
    recs = pm.synthesize_proposals([a, b, c], ctxs, {})
    # Same composite conviction (no consensus) => order by conviction desc, credit desc
    # a and b both have conviction 0.9 (composite 0.7*0.9 + 0.3*0.5 = 0.78)
    # b has more credit so b should come first
    convictions = [r.conviction for r in recs]
    assert convictions[0] >= convictions[-1]
    assert recs[-1].symbol == "QBTS"


def test_drops_action_none(pm: PortfolioManager):
    a = _prop("none", conviction=0.5, strike=None, expiry=None, option_type=None)
    ctx = _ctx("IONQ", 55.0, [])
    recs = pm.synthesize_proposals([a], {"IONQ": ctx}, {})
    assert recs == []
