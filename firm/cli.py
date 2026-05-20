"""CLI entry point.

Commands:
    firm scan [--ticker SYM]     One-shot scan of watchlist or a single ticker
    firm config validate          Sanity-check YAML configs
    firm version                  Print version

Future (Phase 2/3):
    firm start / stop / report / position / wheel
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .config import CONFIG_DIR, FirstRunRequired, load_config

console = Console()


def _setup_logging(cfg) -> None:
    log_path = cfg.root / cfg.firm.logging.file
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=cfg.firm.logging.level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path), logging.StreamHandler(sys.stderr)],
    )
    # yfinance and httpx are noisy at INFO
    logging.getLogger("yfinance").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _load() -> tuple[object, "Path"]:
    """Load env and config. Returns (config, root).

    If the user hasn't run `firm init`, print a friendly hint and exit
    rather than dumping a stack trace.
    """
    load_dotenv()
    try:
        cfg = load_config(CONFIG_DIR)
    except FirstRunRequired as e:
        console.print(Panel(
            f"[yellow]{e}[/yellow]\n\n"
            "  [bold]uv run firm init[/bold]\n\n"
            "It will ask a few questions about your portfolio and write the "
            "config files for you. Takes about a minute.",
            title="First-run setup needed", border_style="yellow",
        ))
        sys.exit(1)
    return cfg, cfg.root


@click.group()
@click.version_option(__version__)
def main() -> None:
    """Wheel-strategy trading firm — recommendations only."""


@main.command("version")
def version_cmd() -> None:
    """Print the firm version."""
    console.print(f"trading-firm v{__version__}")


@main.command("init")
@click.option("--force", is_flag=True,
              help="Overwrite existing accounts.yaml / watchlist.yaml without prompting.")
def init_cmd(force: bool) -> None:
    """First-run setup: ask a few questions, write personalised config files.

    Writes config/accounts.yaml and config/watchlist.yaml from your inputs.
    Creates the empty data_store/ + reports/ directories.

    NOTE: most users can skip this — just open Claude Code in the project
    folder and ask it to set you up. The MCP `init_portfolio` tool runs the
    same logic as this command but conversationally.
    """
    from .setup import DEFAULT_TICKERS, write_configs

    config_dir = CONFIG_DIR
    accounts_path = config_dir / "accounts.yaml"
    watchlist_path = config_dir / "watchlist.yaml"

    existing = [p for p in (accounts_path, watchlist_path) if p.exists()]
    if existing and not force:
        names = ", ".join(p.name for p in existing)
        if not click.confirm(
            f"Found existing {names}. Overwrite?", default=False
        ):
            console.print("[yellow]Aborted. Use --force to skip this prompt next time.[/yellow]")
            return

    console.print(Panel(
        "[bold]Welcome to the Wheel-Strategy Trading Firm.[/bold]\n\n"
        "This setup will ask about the accounts you trade from and the "
        "tickers you want to follow.\n\n"
        "Nothing leaves your machine — all data lives in [cyan]data_store/[/cyan]\n"
        "and your YAML configs in [cyan]config/[/cyan]. No API keys required.\n\n"
        "You can rerun [bold]firm init[/bold] any time to redo this.",
        title="firm init", border_style="cyan",
    ))

    # ── Cash brokerage ──
    console.print("\n[bold cyan]1) Cash brokerage account[/bold cyan]")
    console.print("  (Most retail traders have one. Used for shares + the full wheel.)")
    cash_capital = click.prompt(
        "  How much capital is in your cash brokerage? (USD)",
        type=float, default=10000.0,
    )
    cash_margin = click.confirm(
        "  Is margin enabled on this account?", default=False,
    )

    # ── Roth IRA ──
    console.print("\n[bold cyan]2) Roth IRA[/bold cyan]")
    console.print(
        "  (Optional. IRS forbids margin in IRAs — wheel-only here means CSP + CC.)"
    )
    has_ira = click.confirm("  Do you trade options in a Roth IRA?", default=False)
    ira_capital = 0.0
    if has_ira:
        ira_capital = click.prompt(
            "  How much capital is in your Roth IRA? (USD)",
            type=float, default=20000.0,
        )

    # ── Watchlist ──
    console.print("\n[bold cyan]3) Watchlist[/bold cyan]")
    console.print(
        "  Tickers to follow. Comma-separated. Press Enter to accept the\n"
        "  starter list of liquid, options-friendly names."
    )
    starter = ", ".join(DEFAULT_TICKERS)
    raw_tickers = click.prompt(
        "  Tickers", default=starter, show_default=True,
    )
    tickers = [t.strip().upper() for t in raw_tickers.split(",") if t.strip()]

    summary_data = write_configs(
        config_dir,
        cash_capital=cash_capital,
        cash_margin=cash_margin,
        has_ira=has_ira,
        ira_capital=ira_capital,
        tickers=tickers,
        source="firm init",
    )

    # Confirm validation by attempting a full load.
    load_config(CONFIG_DIR)

    summary = (
        f"[green]✓ Configs written.[/green]\n\n"
        f"  [cyan]config/accounts.yaml[/cyan]   {summary_data['num_accounts']} account(s)\n"
        f"  [cyan]config/watchlist.yaml[/cyan]  {summary_data['num_tickers']} ticker(s)\n\n"
        f"  Cash account: ${cash_capital:,.0f}"
        f"{' (margin on, buying power $' + format(cash_capital*2,',.0f') + ')' if cash_margin else ''}\n"
    )
    if has_ira and ira_capital > 0:
        summary += f"  Roth IRA: ${ira_capital:,.0f}\n"
    summary += (
        f"\n[bold]Next steps:[/bold]\n"
        f"  1. (Optional) Paste current positions:\n"
        f"     [dim]uv run firm position import-paste -a cash[/dim]\n"
        f"  2. Open Claude Code in this folder to start trading:\n"
        f"     [dim]claude[/dim]\n"
        f"  3. Try: \"plan today\" or \"deep dive on AAPL\""
    )
    console.print(Panel(summary, title="Setup complete", border_style="green"))


@main.group("config")
def config_group() -> None:
    """Configuration commands."""


@config_group.command("validate")
def config_validate() -> None:
    """Load and validate all YAML configs. Fails fast on any rule violation."""
    try:
        cfg, _ = _load()
    except Exception as e:
        console.print(f"[red]Config validation failed:[/red] {e}")
        sys.exit(1)
    accts = cfg.accounts.accounts
    t = Table(title="Accounts", show_header=True)
    t.add_column("Key"); t.add_column("Name"); t.add_column("Capital")
    t.add_column("Margin"); t.add_column("Buying Power")
    t.add_column("Max share price"); t.add_column("Allowed actions")
    for key, a in accts.items():
        t.add_row(
            key, a.display_name, f"${a.capital:,.0f}",
            f"{a.margin_multiplier:.1f}x" if a.margin_enabled else "—",
            f"${a.buying_power:,.0f}", f"${a.max_share_price:,.2f}",
            ", ".join(x.value for x in a.allowed_actions),
        )
    console.print(t)

    console.print(Panel(
        f"Watchlist: {len(cfg.watchlist.watchlist)} tickers\n"
        f"Sectors: {', '.join(cfg.watchlist.scout_sectors)}\n"
        f"Analyst model: {cfg.firm.llm.analyst_model}\n"
        f"Portfolio model: {cfg.firm.llm.portfolio_model}",
        title="Firm config", border_style="green",
    ))


@main.command("start")
@click.option("--no-browser", is_flag=True, help="Do not open browser at startup")
@click.option("--headless", is_flag=True, help="Headless mode — kept for compatibility, currently a no-op since auto-scans are disabled.")
def start_cmd(no_browser: bool, headless: bool) -> None:
    """Start the firm dashboard at http://localhost:8088 (no API calls).

    The dashboard is now a read-only view + position editor + paste-chain tool.
    All LLM analysis happens via Claude Code talking to the firm MCP server.
    Ctrl-C stops the dashboard cleanly. SQLite state survives restart.
    """
    cfg, _ = _load()
    _setup_logging(cfg)
    from .lifecycle import is_running, write_pidfile, remove_pidfile
    pid = is_running(cfg)
    if pid is not None:
        console.print(f"[yellow]Firm already running (pid={pid}).[/yellow]")
        return

    if headless:
        # Old-style daemon: scan loop in foreground, no HTTP
        from .lifecycle import FirmDaemon
        console.print("[cyan]Starting firm (headless, scan loop only). Ctrl-C to stop.[/cyan]")
        daemon = FirmDaemon(cfg)
        try:
            asyncio.run(daemon.run())
        except KeyboardInterrupt:
            pass
        return

    # Default: dashboard + integrated scan loop
    import atexit
    import threading
    import webbrowser
    import uvicorn
    from .dashboard.app import create_app
    app = create_app(cfg)
    write_pidfile(cfg)
    # Belt-and-suspenders: clean up pidfile on any exit path (signal, exception, normal)
    atexit.register(lambda: remove_pidfile(cfg))
    url = f"http://{cfg.firm.dashboard.host}:{cfg.firm.dashboard.port}"
    console.print(f"[green]Firm starting at {url}[/green]")
    if not no_browser and cfg.firm.dashboard.open_browser_on_start:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(
            app, host=cfg.firm.dashboard.host, port=cfg.firm.dashboard.port,
            log_level="warning", access_log=False,
        )
    except KeyboardInterrupt:
        pass
    finally:
        remove_pidfile(cfg)
        console.print("[yellow]Firm stopped.[/yellow]")


@main.command("stop")
def stop_cmd() -> None:
    """Stop the running firm daemon."""
    cfg, _ = _load()
    from .lifecycle import is_running, stop_daemon
    pid = is_running(cfg)
    if pid is None:
        console.print("[yellow]No firm daemon running.[/yellow]")
        return
    ok = stop_daemon(cfg)
    if ok:
        console.print(f"[green]Stopped firm daemon (was pid {pid}).[/green]")
    else:
        console.print(f"[red]Failed to stop cleanly.[/red]")


@main.command("status")
def status_cmd() -> None:
    """Print daemon status and latest state snapshot."""
    cfg, _ = _load()
    from .lifecycle import is_running, read_state
    pid = is_running(cfg)
    state = read_state(cfg)
    if pid is None:
        console.print(f"[dim]Firm daemon: not running.[/dim]")
    else:
        console.print(f"[green]Firm daemon: running (pid {pid}).[/green]")
    if state:
        console.print(f"State: {state}")


@main.group("position")
def position_group() -> None:
    """Manage positions in the ledger (manual since no broker API)."""


@position_group.command("list")
@click.option("--account", "-a", help="Filter to one account key")
def position_list_cmd(account: str | None) -> None:
    """List shares + open option positions across accounts."""
    cfg, _ = _load()
    from .portfolio.ledger import Ledger
    ledger = Ledger(cfg.root / cfg.firm.data.ledger_db)
    accounts = [account] if account else list(cfg.accounts.accounts.keys())
    for key in accounts:
        if key not in cfg.accounts.accounts:
            console.print(f"[red]Unknown account: {key}[/red]")
            continue
        shares = ledger.all_shares_for_account(key)
        opts = ledger.all_open_options_for_account(key)
        if not shares and not opts:
            console.print(f"[dim]{key}: empty[/dim]")
            continue
        if shares:
            t = Table(title=f"{key} — shares", show_header=True)
            t.add_column("Symbol"); t.add_column("Shares"); t.add_column("Avg cost")
            t.add_column("Eff basis"); t.add_column("Premiums collected")
            for p in shares:
                t.add_row(p.symbol, str(p.shares),
                          f"${p.average_cost:.2f}", f"${p.effective_cost_basis:.2f}",
                          f"${p.premiums_collected:.2f}")
            console.print(t)
        if opts:
            t = Table(title=f"{key} — open options", show_header=True)
            t.add_column("Symbol"); t.add_column("Side"); t.add_column("Type")
            t.add_column("Strike"); t.add_column("Expiry")
            t.add_column("Contracts"); t.add_column("Entry premium")
            for o in opts:
                t.add_row(o.symbol, o.side.value, o.type.value,
                          f"${o.strike:.2f}", str(o.expiry),
                          str(o.contracts), f"${o.entry_price:.2f}")
            console.print(t)


@position_group.command("add-shares")
@click.option("--account", "-a", required=True, help="Account key")
@click.option("--symbol", "-s", required=True)
@click.option("--shares", type=int, required=True)
@click.option("--price", type=float, required=True, help="Avg fill price per share")
def position_add_shares_cmd(account: str, symbol: str, shares: int, price: float) -> None:
    """Record a buy of shares into the ledger and update wheel state."""
    cfg, _ = _load()
    from .portfolio.ledger import Ledger
    from .strategy.wheel import WheelEvent, WheelEventPayload, apply
    from datetime import datetime
    if account not in cfg.accounts.accounts:
        console.print(f"[red]Unknown account: {account}[/red]")
        return
    ledger = Ledger(cfg.root / cfg.firm.data.ledger_db)
    state = apply(ledger, WheelEventPayload(
        account_key=account, symbol=symbol.upper(),
        event=WheelEvent.BUY_SHARES_DIRECT,
        occurred_at=datetime.now(),
        shares=shares, price_per_share=price,
    ))
    console.print(f"[green]Added {shares} shares of {symbol.upper()} @ ${price:.2f} to {account}. Wheel state: {state.value}[/green]")


@position_group.command("add-option")
@click.option("--account", "-a", required=True)
@click.option("--symbol", "-s", required=True)
@click.option("--type", "type_", type=click.Choice(["put", "call"]), required=True)
@click.option("--strike", type=float, required=True)
@click.option("--expiry", required=True, help="YYYY-MM-DD")
@click.option("--contracts", type=int, default=1)
@click.option("--premium", type=float, required=True, help="Per-share premium received (CSP/CC) or paid (long)")
@click.option("--side", type=click.Choice(["short", "long"]), default="short")
def position_add_option_cmd(account, symbol, type_, strike, expiry, contracts, premium, side):
    """Record opening an option position (CSP, CC, long call, long put)."""
    from datetime import date as _date, datetime
    from .portfolio.ledger import Ledger
    from .strategy.wheel import WheelEvent, WheelEventPayload, apply
    cfg, _ = _load()
    if account not in cfg.accounts.accounts:
        console.print(f"[red]Unknown account: {account}[/red]")
        return
    ledger = Ledger(cfg.root / cfg.firm.data.ledger_db)
    event_map = {
        ("put", "short"): WheelEvent.SELL_CSP,
        ("call", "short"): WheelEvent.SELL_CC,
    }
    if (type_, side) not in event_map:
        console.print(f"[yellow]Long options not yet wired into wheel state machine; recording directly.[/yellow]")
        from .portfolio.types import OptionPosition, OptionSide, OptionType
        opt = OptionPosition(
            account_key=account, symbol=symbol.upper(),
            expiry=_date.fromisoformat(expiry), strike=strike,
            type=OptionType(type_), side=OptionSide(side),
            contracts=contracts, entry_price=premium,
            opened_at=datetime.now(),
        )
        ledger.insert_option(opt)
        console.print(f"[green]Recorded long {type_} on {symbol.upper()}[/green]")
        return
    event = event_map[(type_, side)]
    state = apply(ledger, WheelEventPayload(
        account_key=account, symbol=symbol.upper(),
        event=event, occurred_at=datetime.now(),
        strike=strike, expiry_iso=expiry, contracts=contracts,
        premium_per_share=premium,
    ))
    console.print(f"[green]Opened {side} {type_} {contracts}x {symbol.upper()} ${strike} exp {expiry} @ ${premium:.2f}. Wheel state: {state.value}[/green]")


@position_group.command("import-paste")
@click.option("--account", "-a", required=True)
def position_import_paste_cmd(account: str) -> None:
    """Import positions from a pasted broker portfolio.

    Reads stdin. Expected format: one position per line, two flavors:
        SHARES: SYMBOL SHARES AVG_COST
        OPTION: SYMBOL TYPE STRIKE YYYY-MM-DD CONTRACTS PREMIUM [side=short|long]
    """
    import sys
    from datetime import datetime
    from .portfolio.ledger import Ledger
    from .portfolio.types import OptionPosition, OptionSide, OptionType, SharesPosition
    cfg, _ = _load()
    if account not in cfg.accounts.accounts:
        console.print(f"[red]Unknown account: {account}[/red]")
        return
    console.print(f"[cyan]Paste portfolio lines, then Ctrl-D (Unix) / Ctrl-Z+Enter (Windows):[/cyan]")
    raw = sys.stdin.read()
    ledger = Ledger(cfg.root / cfg.firm.data.ledger_db)
    n_shares = n_opts = 0
    for line in raw.splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        if parts[0].upper() == "SHARES:":
            if len(parts) < 4:
                console.print(f"[yellow]skipping: {line}[/yellow]"); continue
            sym, shares, avg = parts[1].upper(), int(parts[2]), float(parts[3])
            ledger.upsert_shares(SharesPosition(
                account_key=account, symbol=sym,
                shares=shares, total_cost=shares * avg,
                premiums_collected=0.0,
                opened_at=datetime.now(), last_updated=datetime.now(),
            ))
            n_shares += 1
        elif parts[0].upper() == "OPTION:":
            if len(parts) < 7:
                console.print(f"[yellow]skipping: {line}[/yellow]"); continue
            from datetime import date as _date
            sym = parts[1].upper(); type_ = parts[2].lower()
            strike = float(parts[3]); exp = _date.fromisoformat(parts[4])
            contracts = int(parts[5]); premium = float(parts[6])
            side = "short"
            for p in parts[7:]:
                if p.startswith("side="):
                    side = p.split("=", 1)[1]
            ledger.insert_option(OptionPosition(
                account_key=account, symbol=sym, expiry=exp, strike=strike,
                type=OptionType(type_), side=OptionSide(side),
                contracts=contracts, entry_price=premium,
                opened_at=datetime.now(),
            ))
            n_opts += 1
    console.print(f"[green]Imported {n_shares} share positions and {n_opts} option positions.[/green]")


@main.command("paste-chain")
@click.option("--symbol", "-s", required=True)
@click.option("--expiry", required=True, help="YYYY-MM-DD")
@click.option("--spot", type=float, required=True, help="Current underlying price")
def paste_chain_cmd(symbol: str, expiry: str, spot: float) -> None:
    """Parse a pasted options chain (stdin) and dump it as structured rows."""
    import sys
    from datetime import date as _date
    from .data.chain_parser import parse_chain, ChainParseError
    console.print(f"[cyan]Paste chain text for {symbol.upper()} exp {expiry}, then Ctrl-D:[/cyan]")
    raw = sys.stdin.read()
    try:
        chain = parse_chain(raw, symbol, _date.fromisoformat(expiry), spot_price=spot)
    except ChainParseError as e:
        console.print(f"[red]Parse failed: {e}[/red]")
        return
    t = Table(title=f"{symbol.upper()} chain {expiry} (spot ${spot:.2f})")
    t.add_column("Type"); t.add_column("Strike"); t.add_column("Bid"); t.add_column("Ask")
    t.add_column("Last"); t.add_column("Vol"); t.add_column("OI"); t.add_column("IV")
    for c in chain.contracts:
        t.add_row(c.type.value, f"${c.strike:.2f}", f"${c.bid:.2f}", f"${c.ask:.2f}",
                  f"${c.last:.2f}", str(c.volume), str(c.open_interest),
                  f"{c.implied_volatility*100:.0f}%")
    console.print(t)


@main.group("report")
def report_group() -> None:
    """Generate daily or per-ticker reports."""


@report_group.command("daily")
@click.option("--no-llm", is_flag=True, help="Skip LLM agents")
def report_daily_cmd(no_llm: bool) -> None:
    """Run a watchlist scan + Scout, then write today's daily markdown report."""
    cfg, _ = _load()
    _setup_logging(cfg)
    if not no_llm and not os.environ.get("ANTHROPIC_API_KEY"):
        console.print("[yellow]ANTHROPIC_API_KEY not set — using --no-llm.[/yellow]")
        no_llm = True

    async def _go():
        # Run scan + scout in parallel
        from .orchestrator import Orchestrator
        from .reports.daily import write_daily_report
        orch = Orchestrator(cfg)
        if no_llm:
            _install_no_llm_stubs(orch, cfg)
        scan_task = asyncio.create_task(orch.scan_watchlist())
        scout_task = asyncio.create_task(orch.run_scout(top_n=5))
        scan, finds = await asyncio.gather(scan_task, scout_task)
        scan.scout_finds = finds
        path = write_daily_report(cfg, orch.ledger, scan, scout_finds=finds)
        console.print(f"[green]Wrote daily report: {path}[/green]")

    with console.status("[cyan]Scanning + scouting...[/cyan]"):
        asyncio.run(_go())


@report_group.command("ticker")
@click.argument("symbol")
@click.option("--no-llm", is_flag=True, help="Skip LLM outlook synthesis")
def report_ticker_cmd(symbol: str, no_llm: bool) -> None:
    """Write a quarterly-style deep-dive report on one ticker."""
    cfg, _ = _load()
    _setup_logging(cfg)
    if not no_llm and not os.environ.get("ANTHROPIC_API_KEY"):
        no_llm = True

    async def _go():
        from .reports.quarterly import write_quarterly_report
        path = await write_quarterly_report(cfg, symbol, include_llm_summary=not no_llm)
        console.print(f"[green]Wrote report for {symbol.upper()}: {path}[/green]")

    asyncio.run(_go())


def _install_no_llm_stubs(orch, cfg) -> None:
    """Replace LLM agents with deterministic stubs (used by daily report --no-llm)."""
    from .portfolio.types import Signal, SignalDirection
    class _TrendStub:
        name = "technical_stub"
        async def analyze(self, ctx):
            snap = ctx.indicators.get("technical")
            if snap is None:
                return None
            return Signal(agent=self.name, symbol=ctx.symbol,
                          direction=SignalDirection.NEUTRAL, conviction=0.3,
                          rationale=snap.to_summary())
    class _VolStub:
        name = "volatility_stub"
        async def analyze(self, ctx):
            vol = ctx.indicators.get("volatility") or {}
            ivr = vol.get("iv_rank") or 0
            d = SignalDirection.BULL if ivr >= 40 else SignalDirection.NEUTRAL
            return Signal(agent=self.name, symbol=ctx.symbol,
                          direction=d, conviction=0.5,
                          rationale=f"IV rank {ivr:.0f}")
    orch._analysts = {"technical": _TrendStub(), "volatility": _VolStub()}
    orch._specialists = {}  # No proposals without LLM


@main.command("mcp")
def mcp_cmd() -> None:
    """Run the firm MCP server (stdio transport) for Claude Code integration.

    Used internally by Claude Code via .mcp.json — you generally don't run this directly.
    """
    from .mcp_server import main as _main
    _main()


@main.command("validate")
@click.option("--account", "-a", required=True)
@click.option("--symbol", "-s", required=True)
@click.option("--action", required=True,
              type=click.Choice(["buy_shares", "sell_shares", "sell_csp", "sell_cc",
                                 "buy_put", "buy_call"]))
@click.option("--qty", type=int, required=True, help="shares (×100) or contracts")
@click.option("--limit", type=float, required=True, help="limit price")
@click.option("--strike", type=float, default=None)
@click.option("--expiry", default=None, help="YYYY-MM-DD")
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
def validate_cmd(account, symbol, action, qty, limit, strike, expiry, fmt):
    """Run a proposed trade through the Risk Manager. Returns OK or rejection reasons.

    Designed for Claude Code: I propose a ticket → run this → confirm before
    surfacing to the user.
    """
    from datetime import date as _date
    from .agents.risk_manager import RiskManager
    from .portfolio.accounts import snapshot_account
    from .portfolio.ledger import Ledger
    from .portfolio.types import OptionType, Recommendation
    cfg, _ = _load()
    if account not in cfg.accounts.accounts:
        click.echo(f"unknown account: {account}", err=True); sys.exit(1)
    rm = RiskManager(cfg)
    ledger = Ledger(cfg.root / cfg.firm.data.ledger_db)
    snapshots = {
        k: snapshot_account(k, c, ledger) for k, c in cfg.accounts.accounts.items()
    }
    rec = Recommendation(
        account_key=account, symbol=symbol.upper(), action=action,
        contracts_or_shares=qty, limit_price=limit,
        expiry=_date.fromisoformat(expiry) if expiry else None,
        strike=strike,
        option_type=(
            OptionType.PUT if action in ("sell_csp", "buy_put") else
            OptionType.CALL if action in ("sell_cc", "buy_call") else
            None
        ),
        rationale="(validation only)", conviction=1.0,
        expected_credit_or_debit=(
            limit * 100 * qty if action in ("sell_csp", "sell_cc") else
            -limit * 100 * qty if action in ("buy_put", "buy_call") else
            -limit * qty if action == "buy_shares" else
            limit * qty
        ),
    )
    out = rm.review([rec], snapshots)
    if fmt == "json":
        result = {
            "accepted": len(out.accepted) > 0,
            "ticket": rec.order_ticket(),
            "rejection_reason": out.rejected[0][1] if out.rejected else None,
            "warnings": out.accepted[0].risk_notes if out.accepted else [],
        }
        click.echo(json.dumps(result, indent=2))
    else:
        if out.accepted:
            click.echo(f"[green]✓ ACCEPTED[/green]: {rec.order_ticket()}")
            for w in out.accepted[0].risk_notes:
                click.echo(f"  ⚠ {w}")
        else:
            click.echo(f"[red]✗ REJECTED[/red]: {rec.order_ticket()}")
            click.echo(f"  reason: {out.rejected[0][1]}")
            sys.exit(2)


@main.command("context")
@click.argument("symbol", required=False)
@click.option("--all", "all_", is_flag=True, help="Dump all watchlist tickers (heavy)")
@click.option("--format", "fmt", type=click.Choice(["markdown", "json"]), default="markdown")
def context_cmd(symbol: str | None, all_: bool, fmt: str) -> None:
    """Dump raw firm data for a ticker (or the whole watchlist with --all).

    Pure local computation, no LLM calls. Designed for Claude Code sessions:
    you paste the output into a prompt or I run it via the firm MCP server.
    """
    import sys
    cfg, _ = _load()
    from .context_dump import (
        gather_firm_context, gather_ticker_context, ticker_context_markdown,
    )
    if all_:
        data = gather_firm_context(cfg)
        if fmt == "json":
            click.echo(json.dumps(data, indent=2, default=str))
            return
        # Markdown variant: account summary + per-ticker
        click.echo(f"# Firm Context — {data['as_of']}\n")
        click.echo("## Accounts\n")
        for key, acct in data["accounts"].items():
            click.echo(f"### {acct['display_name']} ({key})")
            click.echo(f"- BP ${acct['buying_power']:,.0f}, free ${acct['free_cash']:,.0f}, committed ${acct['committed_capital']:,.0f}, premium LTD ${acct['premium_lifetime']:,.2f}")
            for p in acct.get("positions", []):
                click.echo(f"  - {p['shares']} {p['symbol']} @ avg ${p['average_cost']:.2f} (basis ${p['effective_cost_basis']:.2f})")
            for o in acct.get("open_options", []):
                click.echo(f"  - {o['side']} {o['contracts']}x {o['type']} {o['symbol']} ${o['strike']} exp {o['expiry']}")
        click.echo("\n---\n")
        for ctx in data["watchlist"]:
            click.echo(ticker_context_markdown(ctx))
            click.echo("\n---\n")
        return
    if not symbol:
        click.echo("Usage: firm context <SYMBOL>  or  firm context --all", err=True)
        sys.exit(1)
    ctx = gather_ticker_context(cfg, symbol)
    if fmt == "json":
        click.echo(json.dumps(ctx, indent=2, default=str))
    else:
        click.echo(ticker_context_markdown(ctx))


@main.command("scout")
@click.option("--top", "-n", default=5, help="Top N candidates to surface")
def scout_cmd(top: int) -> None:
    """Discover new tickers outside the watchlist."""
    cfg, _ = _load()
    _setup_logging(cfg)
    from .orchestrator import Orchestrator
    orch = Orchestrator(cfg)

    async def _go():
        finds = await orch.run_scout(top_n=top)
        return finds

    with console.status("[cyan]Scouting for new opportunities...[/cyan]"):
        finds = asyncio.run(_go())

    if not finds:
        console.print("[yellow]No candidates found (markets may be closed or filters too tight).[/yellow]")
        return
    t = Table(title=f"Top {len(finds)} fresh candidates")
    t.add_column("Symbol"); t.add_column("Score"); t.add_column("Source")
    t.add_column("Price"); t.add_column("IVR"); t.add_column("Fits")
    t.add_column("Sector"); t.add_column("Why")
    for f in finds:
        ivr = f"{f.iv_rank:.0f}" if f.iv_rank is not None else "—"
        t.add_row(
            f.symbol, f"{f.score:.2f}", f.source,
            f"${f.price:.2f}", ivr,
            ", ".join(f.fits_account), f.sector_guess,
            Text(f.rationale[:200], overflow="fold"),
        )
    console.print(t)


@main.command("scan")
@click.option("--ticker", "-t", help="Scan a single ticker instead of the watchlist")
@click.option("--use-api", is_flag=True, help="DANGEROUS: actually call the Claude API (costs money). "
                                              "Default workflow is to use Claude Code instead — call `firm context` and ask me.")
def scan_cmd(ticker: str | None, use_api: bool) -> None:
    """One-shot scan: fetch data + apply deterministic heuristics.

    Default: NO API calls. Outputs data + simple heuristic signals.
    For real analysis, use Claude Code: `cd Trading-Firm && claude` and ask.
    """
    cfg, _ = _load()
    _setup_logging(cfg)
    no_llm = not use_api
    if use_api and not os.environ.get("ANTHROPIC_API_KEY"):
        console.print("[red]--use-api requested but ANTHROPIC_API_KEY is empty. Aborting.[/red]")
        sys.exit(1)
    if use_api:
        console.print("[yellow]⚠ WARNING: --use-api will call the Claude API and cost money. "
                      "Consider using Claude Code instead.[/yellow]")
    asyncio.run(_run_scan(cfg, ticker, no_llm))


async def _run_scan(cfg, ticker: str | None, no_llm: bool) -> None:
    from .orchestrator import Orchestrator
    from .agents.technical import TechnicalAnalyst
    from .agents.volatility import VolatilityAnalyst
    from .portfolio.types import Signal, SignalDirection

    orch = Orchestrator(cfg)

    if no_llm:
        # Heuristic stubs for offline / no-API testing. Skips specialist agents
        # entirely (they need the LLM to produce structured proposals).
        from .strategy.options_math import pick_csp_strike, pick_cc_strike
        from .agents.specialist import Proposal
        from .portfolio.types import OptionType

        class _TrendStub:
            name = "technical_stub"
            async def analyze(self, ctx):
                snap = ctx.indicators.get("technical")
                if snap is None:
                    return None
                strength = snap.trend.strength
                if snap.trend.direction == "up":
                    direction = SignalDirection.BULL if strength < 0.7 else SignalDirection.STRONG_BULL
                elif snap.trend.direction == "down":
                    direction = SignalDirection.BEAR if strength < 0.7 else SignalDirection.STRONG_BEAR
                else:
                    direction = SignalDirection.NEUTRAL
                return Signal(
                    agent=self.name, symbol=ctx.symbol,
                    direction=direction, conviction=max(0.3, strength),
                    rationale=f"trend={snap.trend.direction}({strength:.2f}); {snap.to_summary()}",
                )

        class _VolStub:
            name = "volatility_stub"
            async def analyze(self, ctx):
                vol = ctx.indicators.get("volatility") or {}
                ivr = vol.get("iv_rank")
                if ivr is None:
                    return Signal(
                        agent=self.name, symbol=ctx.symbol,
                        direction=SignalDirection.NEUTRAL, conviction=0.2,
                        rationale="no IV rank available — neutral",
                    )
                if ivr >= 60:
                    d, c = SignalDirection.STRONG_BULL, 0.8
                elif ivr >= 30:
                    d, c = SignalDirection.BULL, 0.6
                elif ivr >= 15:
                    d, c = SignalDirection.NEUTRAL, 0.5
                else:
                    d, c = SignalDirection.BEAR, 0.6
                return Signal(
                    agent=self.name, symbol=ctx.symbol,
                    direction=d, conviction=c,
                    rationale=f"IV rank {ivr:.0f} — {('rich' if ivr>=60 else 'decent' if ivr>=30 else 'thin' if ivr>=15 else 'too cheap')} premium",
                )

        class _CSPHeuristic:
            """Deterministic CSP/CC proposer used in --no-llm mode."""
            name = "put_heuristic"
            def __init__(self, cfg):
                self.cfg = cfg
            async def propose(self, ctx, snap):
                from firm.portfolio.types import WheelState
                state = ctx.wheel_states.get(snap.key, WheelState.CASH)
                if state != WheelState.CASH or ctx.options_chain is None:
                    return None
                vol = ctx.indicators.get("volatility") or {}
                ivr = vol.get("iv_rank") or 0
                if ivr < self.cfg.firm.risk.min_iv_rank_for_csp:
                    return None
                entry = self.cfg.watchlist.by_symbol(ctx.symbol)
                happy = entry.target_csp_strike if entry else None
                cap = min(happy, snap.config.max_share_price) if happy else snap.config.max_share_price
                pick = pick_csp_strike(ctx.options_chain, target_delta=self.cfg.firm.risk.target_csp_delta, happy_buy_price=cap)
                if pick is None:
                    return None
                return Proposal(
                    agent=self.name, account_key=snap.key, symbol=ctx.symbol,
                    action="sell_csp", contracts_or_shares=1,
                    limit_price=round(pick.mid, 2),
                    expiry=pick.expiry, strike=pick.strike,
                    option_type=OptionType.PUT, lane="cash_secured_put",
                    rationale=f"Heuristic CSP @ {pick.strike} ({pick.dte}d) — IV rank {ivr:.0f}",
                    conviction=min(0.9, 0.3 + (ivr / 100.0)),
                    expected_credit_or_debit=pick.mid * 100,
                )

        class _CCHeuristic:
            name = "call_heuristic"
            def __init__(self, cfg):
                self.cfg = cfg
            async def propose(self, ctx, snap):
                from firm.portfolio.types import WheelState
                state = ctx.wheel_states.get(snap.key, WheelState.CASH)
                if state != WheelState.LONG_SHARES or ctx.options_chain is None:
                    return None
                pos = snap.shares_for(ctx.symbol)
                if pos is None or pos.shares < 100:
                    return None
                pick = pick_cc_strike(ctx.options_chain, target_delta=self.cfg.firm.risk.target_cc_delta, min_strike=pos.effective_cost_basis)
                if pick is None:
                    return None
                return Proposal(
                    agent=self.name, account_key=snap.key, symbol=ctx.symbol,
                    action="sell_cc", contracts_or_shares=pos.shares // 100,
                    limit_price=round(pick.mid, 2),
                    expiry=pick.expiry, strike=pick.strike,
                    option_type=OptionType.CALL, lane="covered_call",
                    rationale=f"Heuristic CC @ {pick.strike} ({pick.dte}d) over basis {pos.effective_cost_basis:.2f}",
                    conviction=0.6,
                    expected_credit_or_debit=pick.mid * 100 * (pos.shares // 100),
                )

        class _StockNoop:
            name = "stock_heuristic"
            async def propose(self, ctx, snap):
                return None

        orch._analysts = {"technical": _TrendStub(), "volatility": _VolStub()}
        orch._specialists = {
            "stock": _StockNoop(),
            "call": _CCHeuristic(cfg),
            "put": _CSPHeuristic(cfg),
        }

    with console.status("[cyan]Fetching market data and running analysts...[/cyan]"):
        if ticker:
            result = await orch.scan_ticker(ticker)
        else:
            result = await orch.scan_watchlist()

    _print_result(result)


def _print_result(result) -> None:
    # Per-ticker signals
    sig_t = Table(title="Signals by ticker", show_lines=False)
    sig_t.add_column("Ticker"); sig_t.add_column("Sector")
    sig_t.add_column("Price"); sig_t.add_column("Day Δ")
    sig_t.add_column("Trend"); sig_t.add_column("IV rank")
    sig_t.add_column("Signals (direction × conv)")

    for sym, ctx in sorted(result.contexts.items()):
        tech = ctx.indicators.get("technical")
        vol = ctx.indicators.get("volatility") or {}
        sigs = result.signals_by_symbol.get(sym, [])
        sig_str = ", ".join(f"{s.agent.split('_')[0]}={s.direction.value}({s.conviction:.2f})" for s in sigs) or "—"
        trend = f"{tech.trend.direction}({tech.trend.strength:.2f})" if tech else "—"
        ivr = vol.get("iv_rank")
        sig_t.add_row(
            sym, ctx.sector,
            f"${ctx.quote.price:.2f}",
            _colored_pct(ctx.quote.day_change_pct),
            trend,
            f"{ivr:.0f}" if ivr is not None else "—",
            sig_str,
        )
    console.print(sig_t)

    # Recommendations
    if not result.recommendations:
        console.print(Panel("No actionable recommendations this scan.", border_style="yellow"))
    else:
        rec_t = Table(title="Ranked recommendations", show_lines=True)
        rec_t.add_column("#"); rec_t.add_column("Account"); rec_t.add_column("Ticket")
        rec_t.add_column("Conv"); rec_t.add_column("Credit/Debit"); rec_t.add_column("Rationale")
        for i, r in enumerate(result.recommendations[:20], start=1):
            cd = f"+${r.expected_credit_or_debit:,.2f}" if r.expected_credit_or_debit >= 0 else f"-${abs(r.expected_credit_or_debit):,.2f}"
            rec_t.add_row(
                str(i), r.account_key, r.order_ticket(),
                f"{r.conviction:.2f}", cd,
                Text(r.rationale, overflow="fold"),
            )
        console.print(rec_t)

    if result.rejected:
        rej_t = Table(title=f"Filtered out ({len(result.rejected)})", show_lines=False)
        rej_t.add_column("Ticket"); rej_t.add_column("Reason")
        for r, why in result.rejected[:15]:
            rej_t.add_row(r.order_ticket(), why)
        console.print(rej_t)


def _colored_pct(p: float) -> str:
    if p > 0:
        return f"[green]{p:+.2f}%[/green]"
    if p < 0:
        return f"[red]{p:+.2f}%[/red]"
    return "0.00%"


if __name__ == "__main__":
    main()
