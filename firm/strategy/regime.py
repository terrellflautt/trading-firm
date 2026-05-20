"""Quant agent — Markov regime classifier.

Labels each trading day as Bear / Sideways / Bull from the rolling N-day
return, builds the 3×3 maximum-likelihood transition matrix, solves for the
stationary distribution, runs a walk-forward backtest that re-estimates the
matrix at every step (no lookahead), and returns a typed snapshot that the
MCP layer surfaces in `get_ticker_context` and `scout_now`.

Deterministic, local-only, zero LLM calls. Sits next to indicators.py /
options_math.py — the same architectural slot.

Inspiration: the markov-hedge-fund-method observable model (Roan/Lewis
Jackson). We diverge by using a ±5% rolling-return threshold (tighter
signal for the wheel) and a frozen RegimeSnapshot dataclass so the rest of
the firm can consume it without re-doing the pandas work.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from ..portfolio.types import OHLCBar

# State ordering: index 0 = Bear, 1 = Sideways, 2 = Bull.
# Keep this fixed — the transition matrix and stationary distribution are
# returned as numpy arrays indexed by this ordering.
STATES: tuple[str, str, str] = ("Bear", "Sideways", "Bull")
BEAR, SIDEWAYS, BULL = 0, 1, 2

RegimeLabel = Literal["Bear", "Sideways", "Bull"]


@dataclass(frozen=True)
class RegimeSnapshot:
    """Latest regime read for one ticker. Returned to the MCP layer."""
    symbol: str
    window: int                          # Lookback in trading days
    threshold: float                     # Rolling-return cutoff (e.g. 0.05)
    history_days: int                    # Total bars used to fit the matrix
    current_state: RegimeLabel
    current_rolling_return: float        # The actual N-day return at last bar
    transition_matrix: np.ndarray        # 3×3, rows sum to 1
    stationary_distribution: np.ndarray  # length 3, sums to 1
    next_step_distribution: np.ndarray   # P[current_state] — prob of next regime
    bull_probability_next: float         # P(Bull next | current_state)
    bear_probability_next: float         # P(Bear next | current_state)
    sideways_probability_next: float     # P(Sideways next | current_state)
    persistence_diagonal: tuple[float, float, float]  # (Bear→Bear, Side→Side, Bull→Bull)
    walk_forward_sharpe: float | None    # None when insufficient history
    walk_forward_max_drawdown: float | None
    walk_forward_n_trades: int

    def to_summary(self) -> str:
        """One-line summary for markdown / prompt rendering."""
        p_bull = self.bull_probability_next
        p_bear = self.bear_probability_next
        p_side = self.sideways_probability_next
        return (
            f"state={self.current_state} "
            f"(rolling {self.window}d return {self.current_rolling_return*100:+.2f}%) | "
            f"next-day: Bull {p_bull*100:.0f}% / Side {p_side*100:.0f}% / Bear {p_bear*100:.0f}% | "
            f"long-run mix: "
            f"Bull {self.stationary_distribution[BULL]*100:.0f}% / "
            f"Side {self.stationary_distribution[SIDEWAYS]*100:.0f}% / "
            f"Bear {self.stationary_distribution[BEAR]*100:.0f}%"
        )


# ── Core math ──────────────────────────────────────────────────────────────


def label_regimes(close: pd.Series, window: int = 20, threshold: float = 0.05) -> pd.Series:
    """Label each day as Bear (0), Sideways (1), or Bull (2) from rolling return.

    Bull   : N-day rolling return > +threshold
    Bear   : N-day rolling return < -threshold
    Sideways: otherwise

    Returns a label series indexed to match `close`, with the first `window`
    rows dropped (they have no rolling-return yet).
    """
    if window < 1:
        raise ValueError(f"window must be >= 1, got {window}")
    if threshold <= 0:
        raise ValueError(f"threshold must be > 0, got {threshold}")
    rolling_return = close.pct_change(window)
    labels = pd.Series(SIDEWAYS, index=close.index, dtype=int)
    labels[rolling_return > threshold] = BULL
    labels[rolling_return < -threshold] = BEAR
    # Drop the warm-up region where pct_change is NaN
    return labels[rolling_return.notna()]


def build_transition_matrix(labels: pd.Series) -> np.ndarray:
    """Maximum-likelihood 3×3 transition matrix from a label sequence.

    P[i, j] = count(i → j) / count(i → anything). Rows with no outgoing
    transitions fall back to a uniform row so downstream code never sees
    NaN. Returns a (3, 3) ndarray.
    """
    counts = np.zeros((3, 3), dtype=float)
    arr = labels.to_numpy()
    for i in range(len(arr) - 1):
        counts[arr[i], arr[i + 1]] += 1
    row_sums = counts.sum(axis=1, keepdims=True)
    # Uniform fallback for rows we never observed
    P = np.where(row_sums > 0, counts / np.where(row_sums == 0, 1, row_sums), 1.0 / 3.0)
    return P


def stationary_distribution(P: np.ndarray) -> np.ndarray:
    """Left eigenvector of P at eigenvalue 1, normalised to sum to 1.

    Equivalent to the long-run proportion of time spent in each state.
    """
    eigvals, eigvecs = np.linalg.eig(P.T)
    idx = int(np.argmin(np.abs(eigvals - 1.0)))
    vec = np.real(eigvecs[:, idx])
    vec = np.abs(vec)
    total = vec.sum()
    if total == 0 or not np.isfinite(total):
        return np.full(3, 1.0 / 3.0)
    return vec / total


def n_step_forecast(P: np.ndarray, n: int) -> np.ndarray:
    """Chapman-Kolmogorov: P^n is the n-step transition matrix."""
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    return np.linalg.matrix_power(P, n)


def walk_forward_backtest(
    close: pd.Series,
    labels: pd.Series,
    min_train: int = 252,
) -> dict:
    """Walk-forward: at each day t, fit the matrix on labels[:t], read the
    current state, take a +1/0/-1 position from sign(P(Bull) - P(Bear)),
    hold one day, score against next-day return.

    No lookahead. No parameter tuning. Returns Sharpe (annualised), max
    drawdown, and trade count. NaNs when history is too short.
    """
    daily_returns = close.pct_change().dropna()
    common = labels.index.intersection(daily_returns.index)
    labels = labels.loc[common]
    daily_returns = daily_returns.loc[common]

    if len(labels) < min_train + 30:
        return {"sharpe": None, "max_drawdown": None, "n_trades": 0}

    strategy_returns = []
    for t in range(min_train, len(labels) - 1):
        P_t = build_transition_matrix(labels.iloc[:t])
        current = int(labels.iloc[t])
        signal = float(P_t[current, BULL] - P_t[current, BEAR])
        position = float(np.sign(signal))
        next_ret = float(daily_returns.iloc[t + 1])
        strategy_returns.append(position * next_ret)

    sr = np.asarray(strategy_returns, dtype=float)
    if sr.size == 0:
        return {"sharpe": None, "max_drawdown": None, "n_trades": 0}

    std = float(sr.std(ddof=1)) if sr.size > 1 else 0.0
    if std == 0.0 or not np.isfinite(std):
        sharpe = None
    else:
        sharpe = float(sr.mean() / std * np.sqrt(252))

    equity = (1.0 + sr).cumprod()
    running_max = np.maximum.accumulate(equity)
    drawdown = (equity - running_max) / running_max
    max_dd = float(drawdown.min()) if drawdown.size else None

    return {"sharpe": sharpe, "max_drawdown": max_dd, "n_trades": int(sr.size)}


# ── Adapter for the firm ───────────────────────────────────────────────────


def _bars_to_close(bars: list[OHLCBar]) -> pd.Series:
    """Convert OHLCBar list (the firm's internal type) into a close-price series."""
    if not bars:
        return pd.Series(dtype=float)
    return pd.Series(
        [b.close for b in bars],
        index=[b.timestamp for b in bars],
        dtype=float,
        name="close",
    ).sort_index()


def compute_regime(
    symbol: str,
    bars: list[OHLCBar],
    *,
    window: int = 20,
    threshold: float = 0.05,
    min_train: int = 252,
) -> RegimeSnapshot | None:
    """Build a full RegimeSnapshot from daily bars.

    Returns None if there's not enough data to label even one window
    (the caller treats a None snapshot as 'no read').
    """
    close = _bars_to_close(bars)
    if len(close) <= window + 1:
        return None

    labels = label_regimes(close, window=window, threshold=threshold)
    if len(labels) < 2:
        return None

    P = build_transition_matrix(labels)
    pi = stationary_distribution(P)
    current = int(labels.iloc[-1])
    rolling_ret_last = float(close.pct_change(window).iloc[-1])
    next_dist = P[current]

    backtest = walk_forward_backtest(close, labels, min_train=min_train)

    return RegimeSnapshot(
        symbol=symbol.upper(),
        window=window,
        threshold=threshold,
        history_days=len(close),
        current_state=STATES[current],
        current_rolling_return=rolling_ret_last,
        transition_matrix=P,
        stationary_distribution=pi,
        next_step_distribution=next_dist,
        bull_probability_next=float(next_dist[BULL]),
        bear_probability_next=float(next_dist[BEAR]),
        sideways_probability_next=float(next_dist[SIDEWAYS]),
        persistence_diagonal=(float(P[0, 0]), float(P[1, 1]), float(P[2, 2])),
        walk_forward_sharpe=backtest["sharpe"],
        walk_forward_max_drawdown=backtest["max_drawdown"],
        walk_forward_n_trades=backtest["n_trades"],
    )


def regime_markdown(snap: RegimeSnapshot) -> str:
    """Render a RegimeSnapshot as a markdown block for ticker context."""
    lines = ["\n## Regime (Quant agent — Markov)\n"]
    lines.append(
        f"- **Current state**: `{snap.current_state}` "
        f"({snap.window}-day rolling return {snap.current_rolling_return*100:+.2f}%, "
        f"threshold ±{snap.threshold*100:.0f}%)"
    )
    p = snap.next_step_distribution
    lines.append(
        f"- **Next-day probability** from {snap.current_state}: "
        f"Bull {p[BULL]*100:.1f}% / Sideways {p[SIDEWAYS]*100:.1f}% / Bear {p[BEAR]*100:.1f}%"
    )
    pi = snap.stationary_distribution
    lines.append(
        f"- **Long-run regime mix**: "
        f"Bull {pi[BULL]*100:.1f}% / Sideways {pi[SIDEWAYS]*100:.1f}% / Bear {pi[BEAR]*100:.1f}%"
    )
    persist = snap.persistence_diagonal
    lines.append(
        f"- **State stickiness**: "
        f"Bear→Bear {persist[0]*100:.0f}%, "
        f"Sideways→Sideways {persist[1]*100:.0f}%, "
        f"Bull→Bull {persist[2]*100:.0f}%"
    )
    if snap.walk_forward_sharpe is not None:
        lines.append(
            f"- **Walk-forward backtest** ({snap.walk_forward_n_trades} trades): "
            f"Sharpe {snap.walk_forward_sharpe:.2f}, "
            f"max drawdown "
            f"{snap.walk_forward_max_drawdown*100:.1f}%"
        )
    else:
        lines.append(
            f"- Walk-forward backtest: insufficient history ({snap.history_days} bars)"
        )
    return "\n".join(lines)
