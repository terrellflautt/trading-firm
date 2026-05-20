"""Portfolio Manager — synthesizes specialist Proposals into ranked Recommendations.

In Phase 2 the PM is a deterministic synthesizer:
  - It receives the set of Proposals from Stock/Call/Put agents across all
    (ticker, account) pairs.
  - It de-duplicates per (account, symbol): if multiple agents propose
    incompatible actions on the same position, the PM picks one by priority.
  - It augments each surviving proposal with the analyst consensus rationale,
    and produces the final Recommendation list ranked by conviction × yield.

Why deterministic: each specialist already used the LLM to make its proposal;
adding another LLM step here mostly burns tokens to re-rank. Phase 4 may
revisit with an LLM portfolio-level review.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Iterable

from ..config import Action, Config
from ..portfolio.accounts import AccountSnapshot
from ..portfolio.ledger import Ledger
from ..portfolio.types import (
    Recommendation,
    Signal,
    SignalDirection,
    TickerContext,
    WheelState,
)
from .specialist import Proposal

log = logging.getLogger(__name__)


DIRECTION_SCORE = {
    SignalDirection.STRONG_BULL: 1.0,
    SignalDirection.BULL: 0.5,
    SignalDirection.NEUTRAL: 0.0,
    SignalDirection.BEAR: -0.5,
    SignalDirection.STRONG_BEAR: -1.0,
}


# Priority within a single (account, symbol) — higher wins when proposals collide.
# Logic:
#   - sell_cc beats sell_csp if we already own shares (state will be LONG_SHARES)
#   - sell_csp beats buy_shares for entry (premium first; shares-direct only if IV is too low for CSPs)
#   - hedges (buy_put) are last because they're rare
ACTION_PRIORITY: dict[str, int] = {
    "sell_cc": 100,
    "sell_csp": 80,
    "buy_shares": 60,
    "buy_call": 50,
    "buy_put": 40,
    "sell_shares": 30,
    "none": 0,
}


class PortfolioManager:
    def __init__(self, cfg: Config, ledger: Ledger):
        self.cfg = cfg
        self.ledger = ledger

    def synthesize_proposals(
        self,
        proposals: Iterable[Proposal],
        ctxs: dict[str, TickerContext],
        snapshots: dict[str, AccountSnapshot],   # noqa: ARG002 — kept for parity / future use
    ) -> list[Recommendation]:
        """Collapse proposals into a ranked list of Recommendations."""
        proposals = list(proposals)
        if not proposals:
            return []

        # Group by (account, symbol)
        by_pair: dict[tuple[str, str], list[Proposal]] = defaultdict(list)
        for p in proposals:
            by_pair[(p.account_key, p.symbol)].append(p)

        recs: list[Recommendation] = []
        for (acct, sym), group in by_pair.items():
            best = self._pick_best(group)
            if best is None:
                continue
            ctx = ctxs.get(sym)
            if ctx is None:
                continue
            recs.append(self._proposal_to_recommendation(best, ctx))
        return self._rank(recs)

    # ─── Internal helpers ──────────────────────────────────────────────────

    @staticmethod
    def _pick_best(group: list[Proposal]) -> Proposal | None:
        """Pick the highest-priority, highest-conviction proposal in a group."""
        scored = []
        for p in group:
            prio = ACTION_PRIORITY.get(p.action, 0)
            if prio == 0:
                continue
            scored.append((prio, p.conviction, p))
        if not scored:
            return None
        scored.sort(key=lambda t: (-t[0], -t[1]))
        return scored[0][2]

    def _proposal_to_recommendation(self, p: Proposal, ctx: TickerContext) -> Recommendation:
        """Combine the proposal with the analyst rationale into a final ticket."""
        score, avg_conv, rationales = _combine_signals(ctx.signals)
        risk_notes: list[str] = []
        # Composite conviction: average of agent's own conviction and analyst alignment.
        # If consensus disagrees strongly with the proposal direction, dampen.
        directional_agreement = self._directional_agreement(p.action, score)
        composite = max(0.0, min(1.0, 0.7 * p.conviction + 0.3 * directional_agreement))
        rationale = (
            f"[{p.agent} | lane={p.lane or 'n/a'}] {p.rationale} "
            f"| Consensus={score:+.2f} conv={avg_conv:.2f}. "
            + "; ".join(rationales)
        )[:800]
        return Recommendation(
            account_key=p.account_key, symbol=p.symbol,
            action=p.action, contracts_or_shares=p.contracts_or_shares,
            limit_price=p.limit_price if p.limit_price is not None else ctx.quote.price,
            expiry=p.expiry, strike=p.strike, option_type=p.option_type,
            rationale=rationale, conviction=composite,
            expected_credit_or_debit=p.expected_credit_or_debit,
            risk_notes=risk_notes,
        )

    @staticmethod
    def _directional_agreement(action: str, consensus_score: float) -> float:
        """How well does an action agree with the analyst consensus? Returns 0..1."""
        bullish_actions = {"buy_shares", "sell_csp", "buy_call"}
        bearish_actions = {"sell_shares", "buy_put"}
        neutral_actions = {"sell_cc"}  # sells CC is delta-neutralish; modestly bullish
        if action in neutral_actions:
            return 0.6
        if action in bullish_actions:
            return max(0.0, min(1.0, 0.5 + consensus_score / 2.0))
        if action in bearish_actions:
            return max(0.0, min(1.0, 0.5 - consensus_score / 2.0))
        return 0.5

    @staticmethod
    def _rank(recs: list[Recommendation]) -> list[Recommendation]:
        return sorted(
            recs,
            key=lambda r: (-r.conviction, -r.expected_credit_or_debit),
        )


def _combine_signals(signals: Iterable[Signal]) -> tuple[float, float, list[str]]:
    signals = list(signals)
    if not signals:
        return 0.0, 0.0, []
    weighted_sum = 0.0
    weight_total = 0.0
    rationales = []
    for s in signals:
        score = DIRECTION_SCORE[s.direction]
        weighted_sum += score * s.conviction
        weight_total += s.conviction
        rationales.append(f"[{s.agent}] {s.rationale[:150]}")
    consensus = weighted_sum / weight_total if weight_total > 0 else 0.0
    avg_conv = weight_total / len(signals)
    return consensus, avg_conv, rationales
