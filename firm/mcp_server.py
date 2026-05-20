"""MCP server exposing firm data tools to Claude Code.

When you `cd Trading-Firm && claude`, Claude Code auto-discovers this server
via `.mcp.json` in the project root and presents the firm's tools to me.
I (Claude Code) use them with your Pro subscription — **zero API cost**.

Tools exposed:
  - get_ticker_context: full data snapshot for one ticker (markdown)
  - get_firm_context: accounts + entire watchlist (markdown)
  - get_quote: just price + day move
  - get_indicators: technical indicators
  - get_options_chain: parsed chain summary
  - get_fundamentals: balance sheet + cash runway
  - get_news: recent headlines
  - get_regime_state: Markov regime (Bull/Sideways/Bear) + transition matrix
  - get_wheel_state: per-account wheel state for a ticker
  - list_positions: all positions across accounts
  - scout_now: trigger Scout discovery
  - validate_ticket: run a proposed trade through Risk Manager
  - parse_options_chain: parse user-pasted chain text

All tools are no-LLM. They return structured data; I do the reasoning.
"""
from __future__ import annotations

import json
import logging
from datetime import date as _date
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR, Config, FirstRunRequired, load_config
from .context_dump import (
    _option_to_dict,
    _shares_to_dict,
    gather_firm_context,
    gather_ticker_context,
    ticker_context_markdown,
)
from .portfolio.ledger import Ledger

log = logging.getLogger(__name__)

_UNINITIALIZED_ERROR = json.dumps({
    "error": "firm_not_initialized",
    "message": (
        "The firm has no accounts/watchlist yet. Call init_portfolio(...) "
        "after asking the user for: cash brokerage capital, whether margin "
        "is enabled, whether they have a Roth IRA (and its capital), and "
        "which tickers to follow."
    ),
    "next_step_tool": "init_portfolio",
}, indent=2)


def _minutes_until(now, target_time):
    """Minutes from now until target_time today (same date)."""
    from datetime import datetime, time as _time
    target_dt = now.replace(hour=target_time.hour, minute=target_time.minute,
                            second=0, microsecond=0)
    return max(0, int((target_dt - now).total_seconds() / 60))


def build_server(cfg: Config | None = None):
    """Build the MCP server. Lazy import of FastMCP so non-MCP code paths don't pay.

    If accounts.yaml / watchlist.yaml are missing the server still boots —
    but data tools return a friendly "call init_portfolio first" error until
    the user (via Claude Code chat) provides their setup answers.
    """
    from mcp.server.fastmcp import FastMCP

    if cfg is None:
        try:
            cfg = load_config()
        except FirstRunRequired:
            cfg = None
    server = FastMCP("trading-firm")

    # ─── First-run setup ────────────────────────────────────────────────

    @server.tool()
    def firm_status() -> str:
        """Report whether the firm is initialized.

        ALWAYS call this first when starting a new session. If it returns
        initialized=false, ask the user the 4 setup questions and then call
        init_portfolio(...) — the rest of the toolkit is unusable until then.
        """
        if cfg is None:
            return json.dumps({
                "initialized": False,
                "message": (
                    "No accounts.yaml / watchlist.yaml on disk yet. "
                    "Ask the user: (1) cash brokerage capital in USD, "
                    "(2) is margin enabled on the cash account, "
                    "(3) do they trade options in a Roth IRA — and if so, capital, "
                    "(4) which tickers to follow (or accept the default "
                    "SPY/QQQ/AAPL/MSFT/AMD/NVDA/F/INTC starter list). "
                    "Then call init_portfolio(...) with the answers."
                ),
                "next_step_tool": "init_portfolio",
            }, indent=2)
        return json.dumps({
            "initialized": True,
            "accounts": list(cfg.accounts.accounts.keys()),
            "watchlist_size": len(cfg.watchlist.watchlist),
            "watchlist_symbols": [e.symbol for e in cfg.watchlist.watchlist],
        }, indent=2)

    @server.tool()
    def init_portfolio(
        cash_capital: float,
        cash_margin: bool = False,
        has_ira: bool = False,
        ira_capital: float = 0.0,
        tickers: list[str] | None = None,
        force: bool = False,
    ) -> str:
        """First-run setup. Writes accounts.yaml and watchlist.yaml, then arms the firm.

        Call once when firm_status reports initialized=false. After this returns
        successfully, all other tools become available.

        Args:
            cash_capital: USD in the user's cash brokerage account (required, > 0).
            cash_margin: True if 2x margin is enabled on the cash account.
            has_ira: True if the user also trades options in a Roth IRA.
            ira_capital: USD in the Roth IRA. Required and must be > 0 if has_ira.
            tickers: List of ticker symbols to follow. Omit or pass null to accept
                the starter list: SPY, QQQ, AAPL, MSFT, AMD, NVDA, F, INTC.
            force: Overwrite existing configs without asking. Default False —
                if configs already exist, the call is rejected.
        """
        nonlocal cfg
        from .setup import write_configs

        accounts_path = CONFIG_DIR / "accounts.yaml"
        watchlist_path = CONFIG_DIR / "watchlist.yaml"
        existing = [p.name for p in (accounts_path, watchlist_path) if p.exists()]
        if existing and not force:
            return json.dumps({
                "ok": False,
                "error": "configs_exist",
                "existing": existing,
                "message": (
                    f"{', '.join(existing)} already exist. Pass force=true to overwrite, "
                    "or ask the user to confirm before re-running."
                ),
            }, indent=2)

        try:
            summary = write_configs(
                CONFIG_DIR,
                cash_capital=cash_capital,
                cash_margin=cash_margin,
                has_ira=has_ira,
                ira_capital=ira_capital,
                tickers=tickers,
                source="init_portfolio (Claude Code)",
            )
        except ValueError as e:
            return json.dumps({"ok": False, "error": "invalid_input", "message": str(e)})

        try:
            cfg = load_config()
        except Exception as e:  # validation failure on the freshly-written YAML
            return json.dumps({
                "ok": False, "error": "config_load_failed", "message": str(e),
            })

        return json.dumps({
            "ok": True,
            "message": (
                "Firm initialized. Configs written and validated. "
                "All tools are now available. Suggest the user try "
                "'plan today' or 'deep dive on AAPL'."
            ),
            "summary": summary,
        }, indent=2)

    # ─── Time / clock ───────────────────────────────────────────────────

    @server.tool()
    def get_current_time() -> str:
        """Return current time in multiple useful zones.

        Always call this at the start of a planning session so you know exactly
        what time it is in market terms — pre-market vs. live trading vs. close.
        Uses zoneinfo (IANA tzdb), so DST is correct.
        """
        from datetime import datetime
        from zoneinfo import ZoneInfo
        chi = ZoneInfo("America/Chicago")    # user's local time
        ny = ZoneInfo("America/New_York")    # exchange time
        utc = ZoneInfo("UTC")
        now_chi = datetime.now(chi)
        now_ny = datetime.now(ny)
        is_weekday = now_ny.weekday() < 5
        from datetime import time as _time
        market_open = _time(9, 30)
        market_close = _time(16, 0)
        if not is_weekday:
            phase = "weekend (market closed)"
        elif now_ny.time() < _time(4, 0):
            phase = "overnight"
        elif now_ny.time() < market_open:
            phase = "pre-market"
        elif now_ny.time() <= market_close:
            phase = "regular hours (market open)"
        elif now_ny.time() <= _time(20, 0):
            phase = "after-hours"
        else:
            phase = "overnight"
        return json.dumps({
            "user_local_ct": now_chi.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "exchange_et": now_ny.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "utc": datetime.now(utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "weekday": now_ny.strftime("%A"),
            "market_phase": phase,
            "is_weekday": is_weekday,
            "minutes_until_open": _minutes_until(now_ny, market_open) if is_weekday and now_ny.time() < market_open else None,
            "minutes_until_close": _minutes_until(now_ny, market_close) if is_weekday and market_open <= now_ny.time() <= market_close else None,
        }, indent=2)

    # ─── Data tools ─────────────────────────────────────────────────────

    @server.tool()
    def get_ticker_context(symbol: str, format: str = "markdown") -> str:
        """Return a complete data snapshot for one ticker.

        Includes: quote, technical indicators, IV rank, fundamentals, cash runway,
        options chain summary, recent headlines, wheel state per account, and any
        user-supplied report sections. This is the primary tool for analyzing
        one ticker — call it first when the user names a ticker.

        Args:
            symbol: Ticker symbol (e.g. "IONQ"). Case-insensitive.
            format: "markdown" (default, paste-ready) or "json" (structured).
        """
        if cfg is None: return _UNINITIALIZED_ERROR
        ctx = gather_ticker_context(cfg, symbol)
        if format == "json":
            return json.dumps(ctx, indent=2, default=str)
        return ticker_context_markdown(ctx)

    @server.tool()
    def get_firm_context(format: str = "markdown") -> str:
        """Return a full snapshot of accounts + every watchlist ticker.

        Use this at the START of a planning session ("plan today"). Heavy —
        fetches data for every ticker. Returns ~5-10KB of markdown.

        Args:
            format: "markdown" (default) or "json".
        """
        if cfg is None: return _UNINITIALIZED_ERROR
        data = gather_firm_context(cfg)
        if format == "json":
            return json.dumps(data, indent=2, default=str)
        # Light markdown summary then per-ticker contexts
        lines = [f"# Firm Context — {data['as_of']}", "", "## Accounts", ""]
        for key, acct in data["accounts"].items():
            lines.append(f"### {acct['display_name']} ({key})")
            lines.append(
                f"- BP ${acct['buying_power']:,.0f}, free ${acct['free_cash']:,.0f}, "
                f"committed ${acct['committed_capital']:,.0f}, premium LTD ${acct['premium_lifetime']:,.2f}"
            )
            for p in acct.get("positions", []):
                lines.append(
                    f"  - {p['shares']} {p['symbol']} @ avg ${p['average_cost']:.2f} "
                    f"(eff basis ${p['effective_cost_basis']:.2f})"
                )
            for o in acct.get("open_options", []):
                lines.append(
                    f"  - {o['side']} {o['contracts']}x {o['type']} {o['symbol']} "
                    f"${o['strike']} exp {o['expiry']}"
                )
        lines.append("")
        for ctx in data["watchlist"]:
            lines.append("---")
            lines.append(ticker_context_markdown(ctx))
        return "\n".join(lines)

    @server.tool()
    def list_positions(account: str | None = None) -> str:
        """List all shares and open options across accounts (or one account).

        Args:
            account: Optional account key ("roth_ira" or "cash"). If omitted, lists both.
        """
        if cfg is None: return _UNINITIALIZED_ERROR
        ledger = Ledger(cfg.root / cfg.firm.data.ledger_db)
        out: dict[str, Any] = {}
        keys = [account] if account else list(cfg.accounts.accounts.keys())
        for k in keys:
            if k not in cfg.accounts.accounts:
                return json.dumps({"error": f"unknown account: {k}"})
            shares = [_shares_to_dict(p) for p in ledger.all_shares_for_account(k)]
            opts = [_option_to_dict(o) for o in ledger.all_open_options_for_account(k)]
            out[k] = {
                "shares": shares,
                "open_options": opts,
                "wheel_states": {s: ledger.get_wheel_state(k, s).value for s in {p["symbol"] for p in shares}},
            }
        return json.dumps(out, indent=2, default=str)

    @server.tool()
    def get_regime_state(
        symbol: str, window: int | None = None, threshold: float | None = None,
        history_period: str | None = None,
    ) -> str:
        """Quant agent — Markov regime read for one ticker.

        Labels the last N-day window as Bull / Sideways / Bear from the rolling
        return, then returns the full transition matrix, next-day probability
        distribution from the current state, long-run stationary mix, state
        stickiness on the diagonal, and a walk-forward Sharpe + max drawdown.

        Use to: ground a ticker analysis in regime context before sizing trades,
        compare the current state's transition row vs the stationary mix (a big
        gap is unusual market state), or sanity-check whether a Bull-state CSP
        sale matches the model's next-step distribution.

        Args:
            symbol: Ticker (case-insensitive).
            window: Override the rolling-return window (default from firm.yaml — usually 20).
            threshold: Override the ±return cutoff (default 0.05 = ±5%).
            history_period: yfinance period for the fetch (default '3y').
        """
        if cfg is None: return _UNINITIALIZED_ERROR
        from .data.cache import Cache
        from .data.yfinance_client import YFClient
        from .strategy.regime import compute_regime
        rg = cfg.firm.regime
        cache = Cache(cfg.root / cfg.firm.data.cache_db)
        yf = YFClient(cache, cfg.firm.data)
        period = history_period or rg.history_period
        bars = yf.history_daily(symbol.upper(), period=period)
        if not bars:
            return json.dumps({"error": f"no daily history for {symbol}"})
        snap = compute_regime(
            symbol, bars,
            window=window or rg.window,
            threshold=threshold if threshold is not None else rg.threshold,
            min_train=rg.min_train_days,
        )
        if snap is None:
            return json.dumps({
                "error": f"insufficient history for regime fit ({len(bars)} bars; need > {(window or rg.window) + 1})"
            })
        from .context_dump import _regime_to_dict
        return json.dumps(_regime_to_dict(snap), indent=2)

    @server.tool()
    def get_wheel_state(symbol: str) -> str:
        """Wheel state for one ticker across all accounts.

        Returns CASH | SHORT_PUT | LONG_SHARES | COVERED | CALLED_AWAY_PENDING per account.
        """
        if cfg is None: return _UNINITIALIZED_ERROR
        ledger = Ledger(cfg.root / cfg.firm.data.ledger_db)
        sym = symbol.upper()
        out = {}
        for k in cfg.accounts.accounts:
            pos = ledger.get_shares(k, sym)
            opts = ledger.open_options_for(k, sym)
            out[k] = {
                "state": ledger.get_wheel_state(k, sym).value,
                "shares": _shares_to_dict(pos) if pos else None,
                "open_options": [_option_to_dict(o) for o in opts],
                "premium_collected_lifetime": ledger.premiums_collected(k, sym),
            }
        return json.dumps(out, indent=2, default=str)

    # ─── Scout ──────────────────────────────────────────────────────────

    @server.tool()
    def scout_now(top_n: int = 5) -> str:
        """Run the Scout: find new tickers outside the watchlist worth investigating.

        Uses Finviz sector screens + earnings movers + unusual options activity.
        Returns ranked candidates with score, IV rank, sector, account fit.

        Args:
            top_n: How many candidates to return (default 5).
        """
        if cfg is None: return _UNINITIALIZED_ERROR
        import asyncio
        from .data.cache import Cache
        from .data.finviz import FinvizScreener
        from .data.yfinance_client import YFClient
        from .agents.scout import Scout
        cache = Cache(cfg.root / cfg.firm.data.cache_db)
        ledger = Ledger(cfg.root / cfg.firm.data.ledger_db)
        yf = YFClient(cache, cfg.firm.data)
        finviz = FinvizScreener(cache)
        scout = Scout(cfg, yf, finviz, ledger)
        finds = asyncio.run(scout.discover(top_n=top_n))
        return json.dumps([
            {
                "symbol": f.symbol, "score": f.score, "source": f.source,
                "price": f.price, "iv_rank": f.iv_rank,
                "fits_account": f.fits_account, "sector": f.sector_guess,
                "options_oi_total_front": f.options_oi_total_front,
                "indicators": f.indicators_summary, "rationale": f.rationale,
            } for f in finds
        ], indent=2, default=str)

    # ─── Validation ─────────────────────────────────────────────────────

    @server.tool()
    def validate_ticket(
        account: str, symbol: str, action: str,
        qty: int, limit: float,
        strike: float | None = None, expiry: str | None = None,
    ) -> str:
        """Run a proposed trade through the firm's Risk Manager.

        Returns whether the ticket would be ACCEPTED or REJECTED and the reason.
        Always call this BEFORE recommending a trade to the user.

        Args:
            account: "roth_ira" or "cash"
            symbol: Ticker
            action: "buy_shares" | "sell_shares" | "sell_csp" | "sell_cc" | "buy_put" | "buy_call"
            qty: Shares (multiples of 100) or option contracts
            limit: Limit price per share/contract
            strike: Required for option actions
            expiry: YYYY-MM-DD, required for option actions
        """
        if cfg is None: return _UNINITIALIZED_ERROR
        from .agents.risk_manager import RiskManager
        from .portfolio.accounts import snapshot_account
        from .portfolio.types import OptionType, Recommendation
        if account not in cfg.accounts.accounts:
            return json.dumps({"accepted": False, "error": f"unknown account: {account}"})
        rm = RiskManager(cfg)
        ledger = Ledger(cfg.root / cfg.firm.data.ledger_db)
        snapshots = {
            k: snapshot_account(k, c, ledger) for k, c in cfg.accounts.accounts.items()
        }
        opt_type = None
        if action in ("sell_csp", "buy_put"): opt_type = OptionType.PUT
        elif action in ("sell_cc", "buy_call"): opt_type = OptionType.CALL
        try:
            rec = Recommendation(
                account_key=account, symbol=symbol.upper(), action=action,
                contracts_or_shares=qty, limit_price=limit,
                expiry=_date.fromisoformat(expiry) if expiry else None,
                strike=strike, option_type=opt_type,
                rationale="(validation only)", conviction=1.0,
                expected_credit_or_debit=(
                    limit * 100 * qty if action in ("sell_csp", "sell_cc") else
                    -limit * 100 * qty if action in ("buy_put", "buy_call") else
                    -limit * qty if action == "buy_shares" else limit * qty
                ),
            )
        except Exception as e:
            return json.dumps({"accepted": False, "error": str(e)})
        out = rm.review([rec], snapshots)
        return json.dumps({
            "accepted": len(out.accepted) > 0,
            "ticket": rec.order_ticket(),
            "rejection_reason": out.rejected[0][1] if out.rejected else None,
            "warnings": out.accepted[0].risk_notes if out.accepted else [],
        }, indent=2)

    # ─── User content ingestion ─────────────────────────────────────────

    @server.tool()
    def parse_options_chain(
        symbol: str, expiry: str, spot: float, text: str,
    ) -> str:
        """Parse a pasted options chain (broker export, CSV) into structured data.

        Use when the user wants you to analyze options data fresher than yfinance.

        Args:
            symbol: Ticker symbol
            expiry: YYYY-MM-DD
            spot: Current underlying price
            text: The raw pasted chain (CSV-ish, with or without headers)
        """
        from .data.chain_parser import ChainParseError, parse_chain
        try:
            chain = parse_chain(text, symbol, _date.fromisoformat(expiry), spot_price=spot)
        except (ChainParseError, ValueError) as e:
            return json.dumps({"error": str(e)})
        from .context_dump import _chain_to_dict
        return json.dumps(_chain_to_dict(chain), indent=2, default=str)

    # ─── Position management ────────────────────────────────────────────

    @server.tool()
    def add_shares_position(
        account: str, symbol: str, shares: int, price: float,
    ) -> str:
        """Record buying shares into the ledger. Updates wheel state to LONG_SHARES.

        Args:
            account: Account key
            symbol: Ticker
            shares: Number of shares (any positive integer, but 100-multiples are standard)
            price: Average fill price per share
        """
        if cfg is None: return _UNINITIALIZED_ERROR
        from datetime import datetime
        from .strategy.wheel import WheelEvent, WheelEventPayload, apply
        if account not in cfg.accounts.accounts:
            return json.dumps({"error": f"unknown account: {account}"})
        ledger = Ledger(cfg.root / cfg.firm.data.ledger_db)
        state = apply(ledger, WheelEventPayload(
            account_key=account, symbol=symbol.upper(),
            event=WheelEvent.BUY_SHARES_DIRECT,
            occurred_at=datetime.now(),
            shares=shares, price_per_share=price,
        ))
        return json.dumps({"ok": True, "new_wheel_state": state.value})

    @server.tool()
    def record_option_position(
        account: str, symbol: str, option_type: str, side: str,
        strike: float, expiry: str, contracts: int, premium: float,
    ) -> str:
        """Record an option position into the ledger.

        For short positions (CSP/CC), updates wheel state appropriately and credits
        the premium ledger.

        Args:
            account: Account key
            symbol: Ticker
            option_type: "put" or "call"
            side: "short" (you sold) or "long" (you bought)
            strike: Strike price
            expiry: YYYY-MM-DD
            contracts: Number of contracts
            premium: Per-share premium (positive value)
        """
        if cfg is None: return _UNINITIALIZED_ERROR
        from datetime import datetime
        from .strategy.wheel import WheelEvent, WheelEventPayload, apply
        from .portfolio.types import OptionPosition, OptionSide, OptionType
        if account not in cfg.accounts.accounts:
            return json.dumps({"error": f"unknown account: {account}"})
        ledger = Ledger(cfg.root / cfg.firm.data.ledger_db)
        if side == "short":
            event = WheelEvent.SELL_CSP if option_type == "put" else WheelEvent.SELL_CC
            state = apply(ledger, WheelEventPayload(
                account_key=account, symbol=symbol.upper(),
                event=event, occurred_at=datetime.now(),
                strike=strike, expiry_iso=expiry, contracts=contracts,
                premium_per_share=premium,
            ))
            return json.dumps({"ok": True, "new_wheel_state": state.value})
        # Long: insert directly
        ledger.insert_option(OptionPosition(
            account_key=account, symbol=symbol.upper(),
            expiry=_date.fromisoformat(expiry), strike=strike,
            type=OptionType(option_type), side=OptionSide(side),
            contracts=contracts, entry_price=premium,
            opened_at=datetime.now(),
        ))
        return json.dumps({"ok": True})

    # ─── Reports ────────────────────────────────────────────────────────

    @server.tool()
    def list_reports() -> str:
        """List daily and quarterly reports under reports/."""
        if cfg is None: return _UNINITIALIZED_ERROR
        rdir = cfg.root / "reports"
        daily = sorted([p.name for p in rdir.glob("*.md")], reverse=True) if rdir.exists() else []
        qdir = rdir / "quarterly"
        quarterly = sorted([p.name for p in qdir.glob("*.md")], reverse=True) if qdir.exists() else []
        return json.dumps({"daily": daily, "quarterly": quarterly}, indent=2)

    @server.tool()
    def read_report(filename: str) -> str:
        """Read a previously generated daily or quarterly report. Filename only — no paths."""
        if cfg is None: return _UNINITIALIZED_ERROR
        rdir = cfg.root / "reports"
        candidates = [rdir / filename, rdir / "quarterly" / filename]
        for p in candidates:
            if p.exists() and p.is_file():
                return p.read_text(encoding="utf-8")
        return json.dumps({"error": f"report not found: {filename}"})

    return server


def main() -> None:
    """Entry point for `firm mcp`."""
    server = build_server()
    server.run()


if __name__ == "__main__":
    main()
