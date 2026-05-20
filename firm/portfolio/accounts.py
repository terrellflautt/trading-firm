"""Runtime view of accounts: snapshots capital, positions, and free cash for an account."""
from __future__ import annotations

from dataclasses import dataclass

from ..config import AccountConfig
from .ledger import Ledger
from .types import OptionPosition, OptionSide, OptionType, SharesPosition


@dataclass
class AccountSnapshot:
    """Computed state of an account at one moment.

    `committed_capital` includes:
      - Cost basis of shares held
      - Cash-secured-put obligations (strike × 100 × contracts) for short puts
        — this is the cash that must be reserved against assignment.
    """
    config: AccountConfig
    key: str
    shares_positions: list[SharesPosition]
    open_options: list[OptionPosition]

    @property
    def shares_value_at_cost(self) -> float:
        return sum(p.total_cost for p in self.shares_positions)

    @property
    def csp_collateral_reserved(self) -> float:
        total = 0.0
        for opt in self.open_options:
            if opt.type == OptionType.PUT and opt.side == OptionSide.SHORT:
                total += opt.strike * 100 * opt.contracts
        return total

    @property
    def committed_capital(self) -> float:
        return self.shares_value_at_cost + self.csp_collateral_reserved

    @property
    def free_cash(self) -> float:
        return max(0.0, self.config.buying_power - self.committed_capital)

    @property
    def total_premium_collected(self) -> float:
        return sum(p.premiums_collected for p in self.shares_positions)

    def has_shares(self, symbol: str) -> bool:
        sym = symbol.upper()
        return any(p.shares >= 100 and p.symbol == sym for p in self.shares_positions)

    def shares_for(self, symbol: str) -> SharesPosition | None:
        sym = symbol.upper()
        for p in self.shares_positions:
            if p.symbol == sym:
                return p
        return None


def snapshot_account(key: str, config: AccountConfig, ledger: Ledger) -> AccountSnapshot:
    return AccountSnapshot(
        config=config,
        key=key,
        shares_positions=ledger.all_shares_for_account(key),
        open_options=ledger.all_open_options_for_account(key),
    )
