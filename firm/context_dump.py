"""Raw-data context dumper for Claude Code / MCP integration.

Produces structured snapshots of everything an LLM would need to analyze a
ticker (or a whole watchlist) WITHOUT making any LLM API calls of its own.

Outputs available in two formats:
  - Markdown: human-readable, paste-friendly into a Claude Code session
  - JSON: machine-readable, used by the MCP server

The data is the same in both; format differs.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import date as _date, datetime
from pathlib import Path
from typing import Any

from .config import Config
from .data.cache import Cache
from .data.news import NewsAggregator, format_headlines_for_prompt
from .data.user_reports import discover_user_reports, index_by_symbol
from .data.yfinance_client import YFClient
from .portfolio.accounts import snapshot_account
from .portfolio.ledger import Ledger
from .strategy.indicators import compute_indicators
from .strategy.options_math import (
    approx_iv_rank,
    atm_iv_for_chain,
    expected_move,
    realized_volatility_30d,
)
from .strategy.regime import compute_regime, regime_markdown

log = logging.getLogger(__name__)


def gather_ticker_context(cfg: Config, symbol: str) -> dict[str, Any]:
    """Build the full data context for one ticker. Returns a plain dict.

    No LLM calls. Caches everything via the standard data layer.
    """
    sym = symbol.upper()
    cache = Cache(cfg.root / cfg.firm.data.cache_db)
    ledger = Ledger(cfg.root / cfg.firm.data.ledger_db)
    yf = YFClient(cache, cfg.firm.data)
    news_agg = NewsAggregator(cache)

    quote = yf.quote(sym)
    daily = yf.history_daily(sym, period="6mo")
    intraday = yf.history_intraday(sym, days=5)
    chain = yf.options_chain(sym, max_expiries=6)
    funds = yf.fundamentals(sym)
    headlines = news_agg.fetch(sym, limit=12)

    indicators = compute_indicators(sym, daily, intraday) if daily else None
    iv_rank = approx_iv_rank(chain, daily) if chain and daily else None
    atm_iv = atm_iv_for_chain(chain) if chain else None
    rv30 = realized_volatility_30d(daily) if daily else None
    em = None
    if chain and chain.expiries and atm_iv:
        em = expected_move(chain.spot_price, atm_iv, (chain.expiries[0] - _date.today()).days)

    # Quant agent — Markov regime classifier (deterministic, no LLM).
    # Pulls a longer history series so the transition matrix and walk-forward
    # backtest are meaningful. Falls back to None on thin tickers.
    rg = cfg.firm.regime
    daily_long = yf.history_daily(sym, period=rg.history_period)
    regime = compute_regime(
        sym, daily_long,
        window=rg.window, threshold=rg.threshold,
        min_train=rg.min_train_days,
    ) if daily_long else None

    # Per-account state
    accounts_state = {}
    for key, acct_cfg in cfg.accounts.accounts.items():
        snap = snapshot_account(key, acct_cfg, ledger)
        pos = ledger.get_shares(key, sym)
        opts = ledger.open_options_for(key, sym)
        accounts_state[key] = {
            "config": {
                "display_name": acct_cfg.display_name,
                "capital": acct_cfg.capital,
                "buying_power": acct_cfg.buying_power,
                "max_share_price": acct_cfg.max_share_price,
                "free_cash": snap.free_cash,
                "max_position_pct": acct_cfg.max_position_pct,
                "max_position_dollars": acct_cfg.buying_power * acct_cfg.max_position_pct / 100,
                "allowed_actions": [a.value for a in acct_cfg.allowed_actions],
            },
            "wheel_state": ledger.get_wheel_state(key, sym).value,
            "shares": _shares_to_dict(pos) if pos else None,
            "options": [_option_to_dict(o) for o in opts],
            "premium_collected_lifetime": ledger.premiums_collected(key, sym),
        }

    # Watchlist entry + user notes
    entry = cfg.watchlist.by_symbol(sym)
    sector = entry.sector if entry else "unknown"
    notes = entry.notes if entry else ""
    target_csp = entry.target_csp_strike if entry else None

    # User-supplied report sections for this ticker
    user_reports = index_by_symbol(
        discover_user_reports(cfg.root, known_symbols={sym})
    ).get(sym, [])

    return {
        "symbol": sym,
        "sector": sector,
        "notes": notes,
        "target_csp_strike": target_csp,
        "as_of": datetime.now().isoformat(),
        "quote": _quote_to_dict(quote) if quote else None,
        "indicators": _indicators_to_dict(indicators) if indicators else None,
        "regime": _regime_to_dict(regime) if regime else None,
        "volatility": {
            "iv_rank_approx": iv_rank,
            "atm_iv_annualized": atm_iv,
            "realized_vol_30d_annualized": rv30,
            "expected_move_front_expiry": em,
        },
        "fundamentals": _fundamentals_to_dict(funds) if funds else None,
        "options_chain": _chain_to_dict(chain) if chain else None,
        "headlines": [{"title": h.title, "url": h.url, "source": h.source,
                       "published": h.published_at.isoformat()} for h in headlines],
        "accounts": accounts_state,
        "user_reports": [
            {"date": r.report_date.isoformat(), "source": str(r.source_path),
             "text": r.text[:2000]}
            for r in user_reports[:3]
        ],
    }


def gather_firm_context(cfg: Config) -> dict[str, Any]:
    """Daily-planning context: accounts + every watchlist ticker's data (no LLM).

    Heavy — fetches every ticker's data. Used at the start of a planning session.
    """
    accounts_summary = {}
    cache = Cache(cfg.root / cfg.firm.data.cache_db)
    ledger = Ledger(cfg.root / cfg.firm.data.ledger_db)
    for key, acct_cfg in cfg.accounts.accounts.items():
        snap = snapshot_account(key, acct_cfg, ledger)
        accounts_summary[key] = {
            "display_name": acct_cfg.display_name,
            "capital": acct_cfg.capital,
            "buying_power": acct_cfg.buying_power,
            "free_cash": snap.free_cash,
            "committed_capital": snap.committed_capital,
            "premium_lifetime": snap.total_premium_collected,
            "positions": [_shares_to_dict(p) for p in snap.shares_positions],
            "open_options": [_option_to_dict(o) for o in snap.open_options],
        }
    tickers = [gather_ticker_context(cfg, e.symbol) for e in cfg.watchlist.watchlist]
    return {
        "as_of": datetime.now().isoformat(),
        "accounts": accounts_summary,
        "watchlist": tickers,
    }


# ─── Formatting ────────────────────────────────────────────────────────────


def ticker_context_markdown(ctx: dict[str, Any]) -> str:
    sym = ctx["symbol"]
    lines: list[str] = [f"# {sym} — Firm Context Snapshot"]
    lines.append(f"_As of {ctx['as_of']}_  ·  Sector: **{ctx['sector']}**")
    if ctx.get("notes"):
        lines.append(f"\n> {ctx['notes']}")

    # Quote
    q = ctx.get("quote")
    if q:
        lines.append("\n## Price\n")
        lines.append(f"- **Last**: ${q['price']:.2f}  ·  Day Δ: {q['day_change_pct']:+.2f}%  ·  Vol: {q['volume']:,}")
        lines.append(f"- Open ${q['day_open']:.2f}  ·  High ${q['day_high']:.2f}  ·  Low ${q['day_low']:.2f}  ·  Prev close ${q['prev_close']:.2f}")
    # Indicators
    ind = ctx.get("indicators")
    if ind:
        lines.append("\n## Technical Indicators\n")
        lines.append(f"- {ind['summary']}")
        lines.append(f"- SMA20 ${ind['sma_20']:.2f}" + (f", SMA50 ${ind['sma_50']:.2f}" if ind.get("sma_50") else ""))
        lines.append(f"- 20-day support ${ind['support_20']:.2f}, resistance ${ind['resistance_20']:.2f}")
        if ind.get("vwap"):
            lines.append(f"- VWAP (session): ${ind['vwap']:.2f}")
    # Volatility
    vol = ctx.get("volatility", {})
    if vol.get("iv_rank_approx") is not None or vol.get("atm_iv_annualized") is not None:
        lines.append("\n## Volatility\n")
        if vol.get("iv_rank_approx") is not None:
            lines.append(f"- **IV rank (approx)**: {vol['iv_rank_approx']:.0f}")
        if vol.get("atm_iv_annualized"):
            lines.append(f"- ATM IV (annualized): {vol['atm_iv_annualized']*100:.0f}%")
        if vol.get("realized_vol_30d_annualized"):
            lines.append(f"- 30d realized vol: {vol['realized_vol_30d_annualized']*100:.0f}%")
        if vol.get("expected_move_front_expiry"):
            lines.append(f"- 1-σ expected move (front exp): ${vol['expected_move_front_expiry']:.2f}")
    # Regime (Quant agent)
    rg = ctx.get("regime")
    if rg:
        lines.append("\n## Regime (Quant agent — Markov)\n")
        p = rg["next_step_distribution"]
        pi = rg["stationary_distribution"]
        persist = rg["persistence_diagonal"]
        lines.append(
            f"- **Current state**: `{rg['current_state']}` "
            f"({rg['window']}-day rolling return {rg['current_rolling_return']*100:+.2f}%, "
            f"threshold ±{rg['threshold']*100:.0f}%)"
        )
        lines.append(
            f"- **Next-day probability** from {rg['current_state']}: "
            f"Bull {p[2]*100:.1f}% / Sideways {p[1]*100:.1f}% / Bear {p[0]*100:.1f}%"
        )
        lines.append(
            f"- **Long-run regime mix**: "
            f"Bull {pi[2]*100:.1f}% / Sideways {pi[1]*100:.1f}% / Bear {pi[0]*100:.1f}%"
        )
        lines.append(
            f"- **State stickiness** (P→same): "
            f"Bear {persist[0]*100:.0f}%, Sideways {persist[1]*100:.0f}%, Bull {persist[2]*100:.0f}%"
        )
        if rg.get("walk_forward_sharpe") is not None:
            lines.append(
                f"- Walk-forward backtest ({rg['walk_forward_n_trades']} trades): "
                f"Sharpe {rg['walk_forward_sharpe']:.2f}, "
                f"max drawdown {rg['walk_forward_max_drawdown']*100:.1f}%"
            )
    # Fundamentals
    f = ctx.get("fundamentals")
    if f:
        lines.append("\n## Fundamentals\n")
        def m(v):
            if v is None: return "—"
            if abs(v) >= 1e9: return f"${v/1e9:.2f}B"
            if abs(v) >= 1e6: return f"${v/1e6:.2f}M"
            if abs(v) >= 1e3: return f"${v/1e3:.2f}K"
            return f"${v:.2f}"
        lines.append(f"- Market cap {m(f.get('market_cap'))}  ·  Shares out {m(f.get('shares_outstanding'))}")
        lines.append(f"- Cash {m(f.get('cash'))}  ·  Debt {m(f.get('total_debt'))}  ·  Net cash {m((f.get('cash') or 0) - (f.get('total_debt') or 0))}")
        lines.append(f"- Revenue TTM {m(f.get('revenue_ttm'))}  ·  Net income TTM {m(f.get('net_income_ttm'))}")
        lines.append(f"- Operating CF {m(f.get('operating_cash_flow_ttm'))}  ·  FCF {m(f.get('free_cash_flow_ttm'))}")
        runway = f.get("cash_runway_quarters")
        if runway is not None:
            lines.append(f"- Cash runway: **{runway:.1f} quarters**")
        elif (f.get("operating_cash_flow_ttm") or 0) >= 0:
            lines.append("- Cash-flow positive (runway not a concern)")
    # Wheel state per account
    lines.append("\n## Wheel State per Account\n")
    for acct_key, acct in ctx["accounts"].items():
        c = acct["config"]
        lines.append(f"### {c['display_name']} ({acct_key})")
        lines.append(
            f"- Buying power ${c['buying_power']:,.0f}  ·  Free cash ${c['free_cash']:,.0f}  ·  "
            f"Max share price ${c['max_share_price']:,.0f}"
        )
        lines.append(
            f"- Per-ticker cap: {c['max_position_pct']:.0f}% = ${c['max_position_dollars']:,.0f}"
        )
        lines.append(f"- **Wheel state**: `{acct['wheel_state']}`")
        if acct.get("shares"):
            s = acct["shares"]
            lines.append(
                f"- Shares: {s['shares']} @ avg ${s['average_cost']:.2f}, "
                f"effective basis ${s['effective_cost_basis']:.2f} "
                f"(premium collected ${s['premiums_collected']:.2f})"
            )
        for o in acct.get("options", []):
            lines.append(
                f"- {o['side']} {o['contracts']}x {o['type']} ${o['strike']:.2f} "
                f"exp {o['expiry']} @ ${o['entry_price']:.2f}/share entry"
            )
        lines.append(f"- Premium collected (lifetime): ${acct['premium_collected_lifetime']:.2f}")
    # Options chain summary
    chain = ctx.get("options_chain")
    if chain and chain.get("expiries"):
        lines.append("\n## Options Chain (front 3 expiries, near-ATM ±)\n")
        spot = chain.get("spot_price", 0)
        for exp in chain["expiries"][:3]:
            puts = [c for c in chain["contracts"] if c["type"] == "put" and c["expiry"] == exp]
            calls = [c for c in chain["contracts"] if c["type"] == "call" and c["expiry"] == exp]
            puts.sort(key=lambda c: c["strike"])
            calls.sort(key=lambda c: c["strike"])
            dte = (_date.fromisoformat(exp) - _date.today()).days
            lines.append(f"\n**{exp} ({dte} DTE)**\n")
            lines.append("| Strike | Put bid/ask | Put IV | Put OI | Call bid/ask | Call IV | Call OI |")
            lines.append("|---|---|---|---|---|---|---|")
            # Pick strikes around spot
            all_strikes = sorted({c["strike"] for c in puts + calls})
            near = sorted(all_strikes, key=lambda s: abs(s - spot))[:10]
            for k in sorted(near):
                p = next((c for c in puts if c["strike"] == k), None)
                c_ = next((c for c in calls if c["strike"] == k), None)
                lines.append(
                    f"| ${k:.2f} | "
                    f"{'$'+format(p['bid'],'.2f')+'/$'+format(p['ask'],'.2f') if p else '—'} | "
                    f"{format(p['implied_volatility']*100,'.0f')+'%' if p else '—'} | "
                    f"{p['open_interest'] if p else '—'} | "
                    f"{'$'+format(c_['bid'],'.2f')+'/$'+format(c_['ask'],'.2f') if c_ else '—'} | "
                    f"{format(c_['implied_volatility']*100,'.0f')+'%' if c_ else '—'} | "
                    f"{c_['open_interest'] if c_ else '—'} |"
                )
    # Headlines
    headlines = ctx.get("headlines", [])
    if headlines:
        lines.append("\n## Recent Headlines\n")
        for h in headlines[:10]:
            lines.append(f"- [{h['published'][:10]}|{h['source']}] {h['title']}")
    # User reports
    user_reports = ctx.get("user_reports", [])
    if user_reports:
        lines.append("\n## Your Notes (from report files)\n")
        for r in user_reports:
            lines.append(f"### {r['date']}\n\n> {r['text']}\n")
    return "\n".join(lines)


# ─── Dict converters (no Pydantic deps) ────────────────────────────────────


def _quote_to_dict(q) -> dict:
    return {
        "symbol": q.symbol, "price": q.price, "prev_close": q.prev_close,
        "day_open": q.day_open, "day_high": q.day_high, "day_low": q.day_low,
        "volume": q.volume, "avg_volume_30d": q.avg_volume_30d,
        "day_change_pct": q.day_change_pct,
        "timestamp": q.timestamp.isoformat(),
    }


def _regime_to_dict(r) -> dict:
    """Convert a RegimeSnapshot to a JSON-friendly dict (numpy arrays → lists)."""
    return {
        "symbol": r.symbol,
        "window": r.window,
        "threshold": r.threshold,
        "history_days": r.history_days,
        "current_state": r.current_state,
        "current_rolling_return": r.current_rolling_return,
        "transition_matrix": r.transition_matrix.tolist(),
        "stationary_distribution": r.stationary_distribution.tolist(),
        "next_step_distribution": r.next_step_distribution.tolist(),
        "bull_probability_next": r.bull_probability_next,
        "bear_probability_next": r.bear_probability_next,
        "sideways_probability_next": r.sideways_probability_next,
        "persistence_diagonal": list(r.persistence_diagonal),
        "walk_forward_sharpe": r.walk_forward_sharpe,
        "walk_forward_max_drawdown": r.walk_forward_max_drawdown,
        "walk_forward_n_trades": r.walk_forward_n_trades,
    }


def _indicators_to_dict(i) -> dict:
    return {
        "last_close": i.last_close,
        "sma_20": i.sma_20, "sma_50": i.sma_50, "ema_9": i.ema_9,
        "rsi_14": i.rsi_14, "macd": i.macd, "macd_signal": i.macd_signal,
        "macd_hist": i.macd_hist, "atr_14": i.atr_14,
        "bb_upper": i.bb_upper, "bb_lower": i.bb_lower, "bb_pct": i.bb_pct,
        "vwap": i.vwap, "adx_14": i.adx_14,
        "di_plus": i.di_plus, "di_minus": i.di_minus,
        "support_20": i.support_20, "resistance_20": i.resistance_20,
        "trend_direction": i.trend.direction, "trend_strength": i.trend.strength,
        "summary": i.to_summary(),
    }


def _fundamentals_to_dict(f) -> dict:
    return {
        "market_cap": f.market_cap, "shares_outstanding": f.shares_outstanding,
        "cash": f.cash, "total_debt": f.total_debt,
        "revenue_ttm": f.revenue_ttm, "net_income_ttm": f.net_income_ttm,
        "free_cash_flow_ttm": f.free_cash_flow_ttm,
        "operating_cash_flow_ttm": f.operating_cash_flow_ttm,
        "pe_ratio": f.pe_ratio, "dividend_yield": f.dividend_yield,
        "beta": f.beta, "cash_runway_quarters": f.cash_runway_quarters,
    }


def _chain_to_dict(c) -> dict:
    return {
        "symbol": c.symbol, "spot_price": c.spot_price,
        "fetched_at": c.fetched_at.isoformat(),
        "expiries": [e.isoformat() for e in c.expiries],
        "contracts": [{
            "expiry": ct.expiry.isoformat(), "strike": ct.strike,
            "type": ct.type.value, "bid": ct.bid, "ask": ct.ask, "last": ct.last,
            "volume": ct.volume, "open_interest": ct.open_interest,
            "implied_volatility": ct.implied_volatility, "dte": ct.dte,
        } for ct in c.contracts],
    }


def _shares_to_dict(p) -> dict:
    return {
        "symbol": p.symbol, "shares": p.shares,
        "total_cost": p.total_cost, "average_cost": p.average_cost,
        "premiums_collected": p.premiums_collected,
        "effective_cost_basis": p.effective_cost_basis,
    }


def _option_to_dict(o) -> dict:
    return {
        "symbol": o.symbol, "side": o.side.value, "type": o.type.value,
        "strike": o.strike, "expiry": o.expiry.isoformat(),
        "contracts": o.contracts, "entry_price": o.entry_price,
        "credit_received": o.credit_received,
        "opened_at": o.opened_at.isoformat(),
    }
