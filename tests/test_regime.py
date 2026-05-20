"""Tests for the Quant agent (Markov regime classifier)."""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from firm.portfolio.types import OHLCBar
from firm.strategy.regime import (
    BEAR,
    BULL,
    SIDEWAYS,
    STATES,
    build_transition_matrix,
    compute_regime,
    label_regimes,
    n_step_forecast,
    regime_markdown,
    stationary_distribution,
    walk_forward_backtest,
)


def _bars(prices: list[float], start: datetime | None = None) -> list[OHLCBar]:
    start = start or datetime(2024, 1, 2)
    out = []
    for i, p in enumerate(prices):
        out.append(OHLCBar(
            timestamp=start + timedelta(days=i),
            open=p, high=p, low=p, close=p, volume=1_000_000,
        ))
    return out


# ── label_regimes ───────────────────────────────────────────────────────────


def test_label_regimes_bull_when_rally_exceeds_threshold():
    # 22 bars: a flat 20-day base then a >5% move on day 21
    prices = [100.0] * 21 + [108.0]
    close = pd.Series([b.close for b in _bars(prices)])
    labels = label_regimes(close, window=20, threshold=0.05)
    # Last label is the 22nd point — 20-day return is +8% (100 -> 108)
    assert labels.iloc[-1] == BULL


def test_label_regimes_bear_when_decline_exceeds_threshold():
    prices = [100.0] * 21 + [90.0]
    close = pd.Series([b.close for b in _bars(prices)])
    labels = label_regimes(close, window=20, threshold=0.05)
    assert labels.iloc[-1] == BEAR


def test_label_regimes_sideways_inside_threshold():
    prices = [100.0] * 21 + [102.0]   # +2% over 20 days, inside ±5%
    close = pd.Series([b.close for b in _bars(prices)])
    labels = label_regimes(close, window=20, threshold=0.05)
    assert labels.iloc[-1] == SIDEWAYS


def test_label_regimes_rejects_bad_args():
    close = pd.Series([100.0] * 30)
    with pytest.raises(ValueError):
        label_regimes(close, window=0)
    with pytest.raises(ValueError):
        label_regimes(close, threshold=0.0)


# ── build_transition_matrix ────────────────────────────────────────────────


def test_transition_matrix_rows_sum_to_one():
    labels = pd.Series([BULL, BULL, BEAR, SIDEWAYS, BULL, BEAR, BEAR, SIDEWAYS, BULL])
    P = build_transition_matrix(labels)
    assert P.shape == (3, 3)
    np.testing.assert_allclose(P.sum(axis=1), np.ones(3), atol=1e-12)


def test_transition_matrix_unobserved_row_falls_back_uniform():
    # No Bear day at all → Bear row should be uniform (1/3 each), not NaN
    labels = pd.Series([BULL, SIDEWAYS, BULL, BULL, SIDEWAYS])
    P = build_transition_matrix(labels)
    np.testing.assert_allclose(P[BEAR], np.full(3, 1 / 3), atol=1e-12)


def test_transition_matrix_counts_correctly():
    # Bull → Bear once, Bull → Bull once, Bear → Sideways once.
    labels = pd.Series([BULL, BEAR, SIDEWAYS, BULL, BULL])
    P = build_transition_matrix(labels)
    # Bull row: one transition to Bear, one to Bull, none to Sideways → [0.5, 0, 0.5]
    np.testing.assert_allclose(P[BULL], [0.5, 0.0, 0.5], atol=1e-12)


# ── stationary_distribution ────────────────────────────────────────────────


def test_stationary_is_a_distribution():
    labels = pd.Series([BULL, BEAR, SIDEWAYS] * 50)
    P = build_transition_matrix(labels)
    pi = stationary_distribution(P)
    assert pi.shape == (3,)
    assert pi.min() >= 0
    np.testing.assert_allclose(pi.sum(), 1.0, atol=1e-9)


def test_stationary_satisfies_eigenvector_property():
    labels = pd.Series([BULL, BEAR, SIDEWAYS, BULL, BULL, BEAR, SIDEWAYS] * 30)
    P = build_transition_matrix(labels)
    pi = stationary_distribution(P)
    # pi @ P should equal pi (left eigenvector at eigenvalue 1)
    np.testing.assert_allclose(pi @ P, pi, atol=1e-9)


# ── n_step_forecast ────────────────────────────────────────────────────────


def test_n_step_forecast_one_step_is_P():
    labels = pd.Series([BULL, BEAR, SIDEWAYS] * 20)
    P = build_transition_matrix(labels)
    np.testing.assert_allclose(n_step_forecast(P, 1), P, atol=1e-12)


def test_n_step_forecast_converges_to_stationary():
    labels = pd.Series([BULL, BEAR, SIDEWAYS, BULL, BULL, SIDEWAYS, BEAR] * 30)
    P = build_transition_matrix(labels)
    pi = stationary_distribution(P)
    Pn = n_step_forecast(P, 200)
    # Every row of P^n should approach pi
    for row in Pn:
        np.testing.assert_allclose(row, pi, atol=1e-4)


# ── walk_forward_backtest ──────────────────────────────────────────────────


def test_walk_forward_returns_none_when_history_too_short():
    close = pd.Series(np.linspace(100, 110, 100))
    labels = label_regimes(close, window=20, threshold=0.02)
    out = walk_forward_backtest(close, labels, min_train=252)
    assert out["n_trades"] == 0
    assert out["sharpe"] is None
    assert out["max_drawdown"] is None


def test_walk_forward_runs_with_enough_history():
    rng = np.random.default_rng(7)
    # Sustained drift so labels actually flip — 800 days
    drift = 0.001
    log_returns = rng.normal(drift, 0.02, size=800)
    close = pd.Series(100 * np.exp(np.cumsum(log_returns)))
    labels = label_regimes(close, window=20, threshold=0.05)
    out = walk_forward_backtest(close, labels, min_train=252)
    assert out["n_trades"] > 0
    # Sharpe may be positive or negative, but it must be a real number
    assert out["sharpe"] is None or np.isfinite(out["sharpe"])
    assert out["max_drawdown"] is None or out["max_drawdown"] <= 0


# ── compute_regime (end-to-end) ────────────────────────────────────────────


def test_compute_regime_returns_none_when_bars_too_short():
    assert compute_regime("TEST", _bars([100.0] * 10)) is None


def test_compute_regime_full_snapshot_on_realistic_history():
    rng = np.random.default_rng(11)
    n = 600
    returns = rng.normal(0.0005, 0.018, size=n)
    closes = 100 * np.exp(np.cumsum(returns))
    snap = compute_regime("TEST", _bars(list(closes)), window=20, threshold=0.05)
    assert snap is not None
    assert snap.symbol == "TEST"
    assert snap.current_state in STATES
    # Probability distributions are sane
    assert 0.0 <= snap.bull_probability_next <= 1.0
    assert 0.0 <= snap.bear_probability_next <= 1.0
    assert 0.0 <= snap.sideways_probability_next <= 1.0
    np.testing.assert_allclose(
        snap.bull_probability_next + snap.bear_probability_next + snap.sideways_probability_next,
        1.0, atol=1e-9,
    )
    np.testing.assert_allclose(snap.stationary_distribution.sum(), 1.0, atol=1e-9)
    # Markdown rendering must not crash
    md = regime_markdown(snap)
    assert "Regime" in md
    assert snap.current_state in md


def test_compute_regime_labels_steady_uptrend_as_bull():
    # Smooth 0.5%/day rally over a year → ends firmly Bull
    closes = [100.0 * (1.005 ** i) for i in range(300)]
    snap = compute_regime("UP", _bars(closes), window=20, threshold=0.05)
    assert snap is not None
    assert snap.current_state == "Bull"
    # 20-day rolling return on a 0.5%/day rally ≈ 10.4%
    assert snap.current_rolling_return > 0.05


def test_compute_regime_labels_steady_downtrend_as_bear():
    closes = [100.0 * (0.995 ** i) for i in range(300)]
    snap = compute_regime("DOWN", _bars(closes), window=20, threshold=0.05)
    assert snap is not None
    assert snap.current_state == "Bear"
    assert snap.current_rolling_return < -0.05
