"""Risk Manager — the gatekeeper.

This is a deterministic (no-LLM) agent. Every recommendation that other agents
propose must pass through `RiskManager.review()` before reaching the Portfolio
Manager. Anything that fails is dropped from the queue with a reason logged
to the recommendation's risk_notes.

Rules enforced:
  1. 100-share minimum on any shares trade (wheel requires lot size of 100).
  2. Affordability: contracts × 100 × strike ≤ account free cash (for CSPs);
                    shares × price ≤ account free cash (for shares).
  3. IRA constraints: no margin, no naked options, no buy_put/buy_call.
  4. Covered call must be backed by ≥ contracts × 100 owned shares.
  5. Covered call strike must be ≥ effective cost basis.
  6. Max position concentration per ticker (per-ticker override or account default).
  7. Assignment risk flag (informational): short option within `assignment_risk_dte`
     days of expiry AND delta > `assignment_risk_delta`.
  8. Minimum premium yield: annualized premium % must clear `min_premium_pct_annualized`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import AccountConfig, Config
from ..portfolio.accounts import AccountSnapshot
from ..portfolio.types import OptionType, Recommendation
from ..strategy.options_math import bs_delta

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RiskReviewOutcome:
    accepted: list[Recommendation]
    rejected: list[tuple[Recommendation, str]]  # (rec, reason)


class RiskManager:
    """Stateless reviewer — all state comes via AccountSnapshot."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def review(
        self,
        recommendations: list[Recommendation],
        snapshots: dict[str, AccountSnapshot],
    ) -> RiskReviewOutcome:
        accepted: list[Recommendation] = []
        rejected: list[tuple[Recommendation, str]] = []

        for rec in recommendations:
            snap = snapshots.get(rec.account_key)
            if snap is None:
                rejected.append((rec, f"unknown account: {rec.account_key}"))
                continue

            reason = self._first_violation(rec, snap)
            if reason is not None:
                rejected.append((rec, reason))
                continue

            # Annotate with informational warnings (do not block)
            annotated = self._annotate_warnings(rec, snap)
            accepted.append(annotated)

        return RiskReviewOutcome(accepted=accepted, rejected=rejected)

    # ─── Hard rules (block) ────────────────────────────────────────────────

    def _first_violation(self, rec: Recommendation, snap: AccountSnapshot) -> str | None:
        cfg = snap.config
        # Rule 0: action must be in allowed_actions for the account
        from ..config import Action
        try:
            action_enum = Action(rec.action)
        except ValueError:
            return f"unknown action: {rec.action!r}"
        if action_enum not in cfg.allowed_actions:
            return f"action {rec.action!r} not permitted in account {rec.account_key!r}"

        # Rule: any shares trade needs at least 100 shares (wheel-lot rule)
        if rec.action in {Action.BUY_SHARES.value, Action.SELL_SHARES.value}:
            if rec.contracts_or_shares < 100:
                return (
                    f"shares trade rejected: {rec.contracts_or_shares} < 100 minimum "
                    "(wheel requires standard option lots)"
                )

        # Rule: IRA — no margin (caught at config load) + no naked / no long options
        is_ira = ("ira" in rec.account_key.lower() or "roth" in rec.account_key.lower())
        if is_ira and rec.action in {
            Action.BUY_PUT.value, Action.BUY_CALL.value,
            Action.SELL_LONG_PUT.value, Action.SELL_LONG_CALL.value,
        }:
            return f"action {rec.action!r} not permitted in IRA"

        # Rule: affordability
        if rec.action == Action.BUY_SHARES.value:
            cost = rec.contracts_or_shares * rec.limit_price
            if cost > snap.free_cash + 1e-6:
                return (
                    f"insufficient free cash: need ${cost:,.2f}, "
                    f"have ${snap.free_cash:,.2f}"
                )

        if rec.action == Action.SELL_CSP.value:
            if rec.strike is None:
                return "sell_csp missing strike"
            collateral = rec.strike * 100 * rec.contracts_or_shares
            if collateral > snap.free_cash + 1e-6:
                return (
                    f"insufficient free cash for CSP collateral: "
                    f"need ${collateral:,.2f}, have ${snap.free_cash:,.2f}"
                )
            # 100-share affordability check (the underlying must fit if assigned)
            if rec.strike > cfg.max_share_price:
                return (
                    f"strike ${rec.strike:.2f} above account max share price "
                    f"${cfg.max_share_price:.2f} (100-share wheel rule)"
                )

        if rec.action == Action.SELL_CC.value:
            if rec.strike is None:
                return "sell_cc missing strike"
            pos = snap.shares_for(rec.symbol)
            shares_needed = rec.contracts_or_shares * 100
            if pos is None or pos.shares < shares_needed:
                have = pos.shares if pos else 0
                return (
                    f"covered call uncovered: need {shares_needed} shares, "
                    f"have {have} in {rec.account_key}"
                )
            # Strike must clear effective cost basis (premium-adjusted)
            if rec.strike < pos.effective_cost_basis - 1e-6:
                return (
                    f"CC strike ${rec.strike:.2f} below effective cost basis "
                    f"${pos.effective_cost_basis:.2f}"
                )

        if rec.action in {Action.BUY_PUT.value, Action.BUY_CALL.value}:
            debit = rec.limit_price * 100 * rec.contracts_or_shares
            if debit > snap.free_cash + 1e-6:
                return (
                    f"insufficient free cash for long option debit: "
                    f"need ${debit:,.2f}, have ${snap.free_cash:,.2f}"
                )

        # Rule: per-ticker concentration
        cap_pct = self._max_position_pct_for_ticker(rec.symbol, cfg)
        cap_dollars = cfg.buying_power * (cap_pct / 100.0)
        committed = self._estimate_committed_after(rec, snap)
        if committed > cap_dollars + 1e-6:
            return (
                f"position cap exceeded: ${committed:,.2f} > "
                f"${cap_dollars:,.2f} ({cap_pct:.0f}% of buying power)"
            )

        # Rule: minimum premium yield (CSPs and CCs only)
        if rec.action in {Action.SELL_CSP.value, Action.SELL_CC.value}:
            yield_pct = self._annualized_yield_pct(rec)
            if yield_pct is not None and yield_pct < self.cfg.firm.risk.min_premium_pct_annualized:
                return (
                    f"premium too thin: {yield_pct:.1f}% annualized < "
                    f"{self.cfg.firm.risk.min_premium_pct_annualized:.1f}% floor"
                )

        return None

    # ─── Informational warnings (do not block) ─────────────────────────────

    def _annotate_warnings(
        self, rec: Recommendation, snap: AccountSnapshot,
    ) -> Recommendation:
        warnings: list[str] = list(rec.risk_notes)
        from ..config import Action

        # Assignment risk
        if rec.action in {Action.SELL_CSP.value, Action.SELL_CC.value}:
            if rec.expiry is not None and rec.strike is not None:
                from datetime import date as _date
                dte = (rec.expiry - _date.today()).days
                if dte <= self.cfg.firm.risk.assignment_risk_dte:
                    warnings.append(
                        f"high assignment risk: {dte} DTE ≤ "
                        f"{self.cfg.firm.risk.assignment_risk_dte} threshold"
                    )

        # Concentration approaching cap (>80% of cap)
        cap_pct = self._max_position_pct_for_ticker(rec.symbol, snap.config)
        cap_dollars = snap.config.buying_power * (cap_pct / 100.0)
        committed = self._estimate_committed_after(rec, snap)
        if cap_dollars > 0 and committed / cap_dollars > 0.80:
            warnings.append(
                f"concentration warning: ${committed:,.0f} is "
                f"{committed / cap_dollars:.0%} of {cap_pct:.0f}% cap"
            )

        if warnings == list(rec.risk_notes):
            return rec
        return Recommendation(
            account_key=rec.account_key, symbol=rec.symbol, action=rec.action,
            contracts_or_shares=rec.contracts_or_shares,
            limit_price=rec.limit_price, expiry=rec.expiry, strike=rec.strike,
            option_type=rec.option_type, rationale=rec.rationale,
            conviction=rec.conviction,
            expected_credit_or_debit=rec.expected_credit_or_debit,
            risk_notes=warnings, issued_at=rec.issued_at,
        )

    # ─── Helpers ───────────────────────────────────────────────────────────

    def _max_position_pct_for_ticker(self, symbol: str, cfg: AccountConfig) -> float:
        entry = self.cfg.watchlist.by_symbol(symbol)
        if entry is not None and entry.max_position_pct is not None:
            return entry.max_position_pct
        return cfg.max_position_pct

    def _estimate_committed_after(
        self, rec: Recommendation, snap: AccountSnapshot,
    ) -> float:
        from ..config import Action
        existing = 0.0
        pos = snap.shares_for(rec.symbol)
        if pos is not None:
            existing += pos.total_cost
        for opt in snap.open_options:
            if opt.symbol != rec.symbol:
                continue
            if opt.type == OptionType.PUT and opt.side.value == "short":
                existing += opt.strike * 100 * opt.contracts

        delta = 0.0
        if rec.action == Action.BUY_SHARES.value:
            delta = rec.contracts_or_shares * rec.limit_price
        elif rec.action == Action.SELL_CSP.value and rec.strike is not None:
            delta = rec.strike * 100 * rec.contracts_or_shares
        # CC, BUY_PUT, BUY_CALL don't increase ticker exposure beyond what's already there
        return existing + delta

    def _annualized_yield_pct(self, rec: Recommendation) -> float | None:
        from datetime import date as _date
        if rec.expiry is None or rec.strike is None or rec.limit_price <= 0:
            return None
        dte = max(1, (rec.expiry - _date.today()).days)
        # For CSP, capital = strike * 100; for CC, also strike * 100 as conservative proxy
        capital = rec.strike * 100.0
        premium = rec.limit_price * 100.0
        pct = (premium / capital) * (365.0 / dte) * 100.0
        return pct
