"""Configuration loading and validation.

All three YAML files (firm, accounts, watchlist) are loaded once at startup
into immutable Pydantic models. Any rule violation that would put the firm
into a dangerous state (e.g. margin=true on Roth) raises at load time.
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class Action(str, Enum):
    BUY_SHARES = "buy_shares"
    SELL_SHARES = "sell_shares"
    SELL_CSP = "sell_csp"
    SELL_CC = "sell_cc"
    CLOSE_CSP = "close_csp"
    CLOSE_CC = "close_cc"
    ROLL_CSP = "roll_csp"
    ROLL_CC = "roll_cc"
    BUY_PUT = "buy_put"
    BUY_CALL = "buy_call"
    SELL_LONG_PUT = "sell_long_put"
    SELL_LONG_CALL = "sell_long_call"


class AccountConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    display_name: str
    broker: str = "manual"
    capital: float = Field(gt=0)
    margin_enabled: bool = False
    margin_multiplier: float = Field(default=1.0, ge=1.0, le=4.0)
    options_level: int = Field(ge=0, le=4)
    allowed_actions: list[Action]
    max_position_pct: float = Field(gt=0, le=100)
    notes: str = ""

    @property
    def buying_power(self) -> float:
        if self.margin_enabled:
            return self.capital * self.margin_multiplier
        return self.capital

    @property
    def max_share_price(self) -> float:
        """Max share price to afford 100 shares (wheel-required lot size)."""
        return self.buying_power / 100.0


class AccountsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    accounts: dict[str, AccountConfig]

    @model_validator(mode="after")
    def _validate_ira_rules(self) -> "AccountsConfig":
        for key, acct in self.accounts.items():
            is_ira = "ira" in key.lower() or "roth" in key.lower()
            if is_ira and acct.margin_enabled:
                raise ValueError(
                    f"Account {key!r}: IRAs cannot use margin (IRS rule). "
                    "Set margin_enabled: false."
                )
            if is_ira:
                forbidden = {
                    Action.BUY_PUT, Action.BUY_CALL,
                    Action.SELL_LONG_PUT, Action.SELL_LONG_CALL,
                }
                # Long options are technically allowed in IRAs at level 2+,
                # but we keep IRAs to CSP/CC only for the wheel-purist approach.
                naked = forbidden & set(acct.allowed_actions)
                if naked:
                    raise ValueError(
                        f"Account {key!r}: actions {naked} not permitted in wheel-only IRA."
                    )
        return self


class WatchlistEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    sector: str
    notes: str = ""
    max_position_pct: float | None = None
    target_csp_strike: float | None = None
    target_cc_strike: float | None = None


class ScoutFilters(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    min_avg_volume: int = 500_000
    min_options_open_interest: int = 100
    max_share_price_roth: float = 200.0
    max_share_price_cash: float = 50.0
    min_iv_rank: float = 30.0


class WatchlistConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    watchlist: list[WatchlistEntry]
    scout_sectors: list[str]
    scout_filters: ScoutFilters

    def by_symbol(self, symbol: str) -> WatchlistEntry | None:
        sym_u = symbol.upper()
        for entry in self.watchlist:
            if entry.symbol.upper() == sym_u:
                return entry
        return None


class LLMConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    analyst_model: str
    portfolio_model: str
    max_tokens: int = Field(gt=0, le=64_000)
    temperature: float = Field(ge=0.0, le=1.0)
    enable_prompt_caching: bool = True


class ScanConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule_timezone: str
    schedule: list[str] = Field(min_length=1)
    scout_schedule: list[str] = Field(default_factory=list)
    daily_report_at: str | None = None
    catch_up_on_start: bool = True

    @model_validator(mode="after")
    def _validate_times(self) -> "ScanConfig":
        from datetime import time as _time
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(self.schedule_timezone)
        except ZoneInfoNotFoundError as e:
            raise ValueError(f"unknown timezone {self.schedule_timezone!r}") from e
        for t in (*self.schedule, *self.scout_schedule, *([self.daily_report_at] if self.daily_report_at else [])):
            try:
                _time.fromisoformat(t)
            except ValueError as e:
                raise ValueError(f"bad time {t!r}: {e}") from e
        return self


class DashboardConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    port: int = Field(gt=0, lt=65_536)
    open_browser_on_start: bool = True


class DataConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cache_db: str
    ledger_db: str
    yfinance_quote_ttl_seconds: int
    yfinance_history_ttl_seconds: int
    yfinance_options_ttl_seconds: int
    yfinance_fundamentals_ttl_seconds: int
    barchart_ttl_seconds: int


class RiskConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    default_max_position_pct: float = Field(gt=0, le=100)
    assignment_risk_dte: int = Field(ge=0)
    assignment_risk_delta: float = Field(gt=0, lt=1)
    target_csp_delta: float = Field(gt=0, lt=1)
    target_cc_delta: float = Field(gt=0, lt=1)
    min_iv_rank_for_csp: float = Field(ge=0, le=100)
    min_premium_pct_annualized: float = Field(ge=0)


class LoggingConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    file: str


class RegimeConfig(BaseModel):
    """Quant agent — Markov regime classifier parameters."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    window: int = Field(default=20, ge=2, le=252,
                        description="Rolling-return lookback in trading days")
    threshold: float = Field(default=0.05, gt=0, lt=1.0,
                             description="±return threshold for Bull/Bear labels (0.05 = ±5%)")
    history_period: str = Field(default="3y",
                                description="yfinance period string for the close-price fetch")
    min_train_days: int = Field(default=252, ge=30,
                                description="Min labeled days before the walk-forward backtest runs")


class FirmMeta(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    timezone: str


class FirmConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    firm: FirmMeta
    llm: LLMConfig
    scan: ScanConfig
    dashboard: DashboardConfig
    data: DataConfig
    risk: RiskConfig
    logging: LoggingConfig
    regime: RegimeConfig = Field(default_factory=RegimeConfig)


class Config(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    firm: FirmConfig
    accounts: AccountsConfig
    watchlist: WatchlistConfig
    root: Path


def _load_yaml(path: Path) -> dict:
    with path.open() as f:
        return yaml.safe_load(f)


class FirstRunRequired(RuntimeError):
    """Raised when accounts.yaml or watchlist.yaml is missing.

    Callers (CLI, MCP server) catch this and direct the user to run
    `firm init`, which writes both files from .example templates + prompts.
    """


def load_config(config_dir: Path = CONFIG_DIR) -> Config:
    """Load and validate all configuration. Fails fast on any rule violation.

    Raises FirstRunRequired if the user has not yet personalised the firm
    via `firm init` — i.e. accounts.yaml or watchlist.yaml is missing.
    """
    config_dir = Path(config_dir)
    missing = [
        name for name in ("accounts.yaml", "watchlist.yaml")
        if not (config_dir / name).exists()
    ]
    if missing:
        raise FirstRunRequired(
            f"Missing config: {', '.join(missing)}. "
            f"Run `uv run firm init` to create them from the .example templates."
        )
    firm_yaml = _load_yaml(config_dir / "firm.yaml")
    accounts_yaml = _load_yaml(config_dir / "accounts.yaml")
    watchlist_yaml = _load_yaml(config_dir / "watchlist.yaml")

    return Config(
        firm=FirmConfig(**firm_yaml),
        accounts=AccountsConfig(**accounts_yaml),
        watchlist=WatchlistConfig(**watchlist_yaml),
        root=config_dir.parent,
    )
