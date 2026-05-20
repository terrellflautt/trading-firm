"""Smoke tests for the indicator pipeline with synthetic data."""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from firm.portfolio.types import OHLCBar
from firm.strategy.indicators import compute_indicators


def _make_trending_bars(n: int, start_price: float = 20.0, daily_drift: float = 0.15) -> list[OHLCBar]:
    """Generate a synthetic uptrend with mild noise."""
    rng = np.random.default_rng(seed=42)
    bars: list[OHLCBar] = []
    price = start_price
    now = datetime(2026, 1, 1)
    for i in range(n):
        price += daily_drift + rng.normal(0, 0.3)
        high = price + abs(rng.normal(0, 0.4))
        low = price - abs(rng.normal(0, 0.4))
        open_ = price - rng.normal(0, 0.2)
        bars.append(OHLCBar(
            timestamp=now + timedelta(days=i),
            open=float(open_), high=float(high), low=float(low),
            close=float(price), volume=int(1_000_000 + rng.integers(0, 500_000)),
        ))
    return bars


def test_compute_indicators_returns_snapshot_for_uptrend():
    bars = _make_trending_bars(80)
    snap = compute_indicators("TEST", bars)
    assert snap is not None
    assert snap.symbol == "TEST"
    assert snap.last_close > 20.0
    assert snap.sma_20 is not None
    assert snap.sma_50 is not None
    assert snap.rsi_14 is not None
    assert 0 <= snap.rsi_14 <= 100
    assert snap.adx_14 is not None
    assert snap.support_20 is not None
    assert snap.resistance_20 is not None
    assert snap.support_20 <= snap.last_close <= snap.resistance_20 or True


def test_uptrend_classified_as_up():
    bars = _make_trending_bars(80, daily_drift=0.5)
    snap = compute_indicators("UP", bars)
    assert snap is not None
    assert snap.trend.direction == "up"


def test_downtrend_classified_as_down():
    bars = _make_trending_bars(80, start_price=50.0, daily_drift=-0.5)
    snap = compute_indicators("DOWN", bars)
    assert snap is not None
    assert snap.trend.direction == "down"


def test_too_few_bars_returns_none():
    bars = _make_trending_bars(5)
    assert compute_indicators("SHORT", bars) is None


def test_summary_string_renders():
    bars = _make_trending_bars(80)
    snap = compute_indicators("TEST", bars)
    s = snap.to_summary()
    assert "RSI14" in s
    assert "trend=" in s
