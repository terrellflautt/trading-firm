"""Tests for options math: IV rank, delta-based strike picking, yields, wheel entry."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from firm.portfolio.types import OptionContract, OptionsChain, OptionType
from firm.strategy.options_math import (
    bs_delta,
    cc_yield,
    csp_yield,
    expected_move,
    pick_cc_strike,
    pick_csp_strike,
    recommend_wheel_entry,
)


def _make_chain(spot: float, expiry: date, strikes: list[float], iv: float = 0.50) -> OptionsChain:
    contracts: list[OptionContract] = []
    for k in strikes:
        for kind in (OptionType.PUT, OptionType.CALL):
            mid_iv = iv * (1.0 + 0.02 * (k - spot) / spot)  # slight skew
            # crude option price using BS-ish approximation
            t = max((expiry - date.today()).days, 1) / 365.0
            sigma = mid_iv
            # placeholder "premium" — what matters for tests is rank not price accuracy
            premium = max(0.01, sigma * spot * (t ** 0.5) * 0.4)
            contracts.append(OptionContract(
                symbol="TEST", expiry=expiry, strike=k, type=kind,
                bid=premium * 0.95, ask=premium * 1.05, last=premium,
                volume=1000, open_interest=500, implied_volatility=mid_iv,
                delta=None, gamma=None, theta=None, vega=None,
            ))
    return OptionsChain(
        symbol="TEST", fetched_at=datetime.now(),
        spot_price=spot, expiries=[expiry], contracts=contracts,
    )


def test_atm_call_delta_is_about_half():
    d = bs_delta(spot=100, strike=100, iv=0.40, dte=30, opt_type=OptionType.CALL)
    assert 0.4 < d < 0.6


def test_otm_put_delta_negative_and_small_in_magnitude():
    d = bs_delta(spot=100, strike=90, iv=0.40, dte=30, opt_type=OptionType.PUT)
    assert -0.5 < d < 0.0


def test_pick_csp_picks_otm_put_below_spot():
    expiry = date.today() + timedelta(days=30)
    chain = _make_chain(spot=100, expiry=expiry, strikes=[80, 85, 90, 95, 100, 105, 110])
    pick = pick_csp_strike(chain, target_delta=0.25)
    assert pick is not None
    assert pick.type == OptionType.PUT
    assert pick.strike < 100  # OTM


def test_pick_csp_respects_happy_buy_price_cap():
    expiry = date.today() + timedelta(days=30)
    chain = _make_chain(spot=100, expiry=expiry, strikes=[80, 85, 90, 95, 100])
    pick = pick_csp_strike(chain, target_delta=0.25, happy_buy_price=90.0)
    assert pick is not None
    assert pick.strike <= 90.0


def test_pick_cc_respects_min_strike_above_cost():
    expiry = date.today() + timedelta(days=30)
    chain = _make_chain(spot=100, expiry=expiry, strikes=[95, 100, 105, 110, 115, 120])
    pick = pick_cc_strike(chain, target_delta=0.25, min_strike=105.0)
    assert pick is not None
    assert pick.strike >= 105.0


def test_csp_yield_annualizes_correctly():
    expiry = date.today() + timedelta(days=30)
    c = OptionContract(
        symbol="TEST", expiry=expiry, strike=25.0, type=OptionType.PUT,
        bid=0.50, ask=0.60, last=0.55, volume=100, open_interest=200,
        implied_volatility=0.5, delta=None, gamma=None, theta=None, vega=None,
    )
    y = csp_yield(c)
    assert y.capital_required == pytest.approx(2500.0)
    # mid ≈ 0.55, premium = 55, return = 55/2500 = 2.2%
    assert y.return_pct == pytest.approx(2.2, abs=0.1)
    # annualized = 2.2% * (365/30) ≈ 26.8%
    assert y.annualized_pct == pytest.approx(26.77, abs=0.2)


def test_cc_yield_uses_cost_basis():
    expiry = date.today() + timedelta(days=21)
    c = OptionContract(
        symbol="TEST", expiry=expiry, strike=30.0, type=OptionType.CALL,
        bid=0.40, ask=0.50, last=0.45, volume=100, open_interest=200,
        implied_volatility=0.5, delta=None, gamma=None, theta=None, vega=None,
    )
    y = cc_yield(c, cost_basis_per_share=25.0)
    assert y.capital_required == pytest.approx(2500.0)
    assert y.return_pct == pytest.approx(1.8, abs=0.1)


def test_expected_move_scales_with_sqrt_time():
    em30 = expected_move(spot=100, iv=0.40, dte=30)
    em60 = expected_move(spot=100, iv=0.40, dte=60)
    assert em30 is not None and em60 is not None
    # 60-day move should be ~ sqrt(2) * 30-day move
    ratio = em60 / em30
    assert 1.35 < ratio < 1.50


def test_wheel_entry_high_iv_picks_csp():
    assert recommend_wheel_entry(iv_rank=60.0, setup_quality=0.5) == "sell_csp"


def test_wheel_entry_low_iv_strong_setup_buys_shares():
    assert recommend_wheel_entry(iv_rank=15.0, setup_quality=0.8) == "buy_shares"


def test_wheel_entry_weak_signal_waits():
    assert recommend_wheel_entry(iv_rank=15.0, setup_quality=0.2) == "wait"
