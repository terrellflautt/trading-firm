"""HTTP route registration for the dashboard."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date as _date, datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sse_starlette.sse import EventSourceResponse

from ..agents.scout import ScoutCandidate
from ..data.chain_parser import ChainParseError, parse_chain
from ..portfolio.accounts import snapshot_account
from ..portfolio.ledger import Ledger
from ..portfolio.types import (
    OptionPosition,
    OptionSide,
    OptionType,
    SharesPosition,
    WheelState,
)
from ..strategy.wheel import WheelEvent, WheelEventPayload, apply
from ..utils.time import utc_now
from .state import get_app_state

log = logging.getLogger(__name__)


def register(app: FastAPI) -> None:
    templates = app.state.templates

    # ─── Home ────────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        state = get_app_state()
        snapshots = {
            key: snapshot_account(key, c, state.orchestrator.ledger)
            for key, c in state.cfg.accounts.accounts.items()
        }
        return templates.TemplateResponse(
            request, "home.html",
            {
                "state": state,
                "snapshots": snapshots,
                "scan": state.latest_scan,
                "scout": state.latest_scout,
            },
        )

    @app.post("/refresh", response_class=HTMLResponse)
    async def refresh(request: Request):
        """Disabled in MCP-only mode. Scans now happen via Claude Code."""
        return HTMLResponse(
            "Scans are now run via Claude Code (zero API cost). "
            "Open a Claude Code session in this folder and ask for analysis.",
            status_code=410,  # Gone
        )

    # ─── Ticker detail ───────────────────────────────────────────────────

    @app.get("/ticker/{symbol}", response_class=HTMLResponse)
    async def ticker_detail(symbol: str, request: Request):
        state = get_app_state()
        sym = symbol.upper()
        ctx = (state.latest_scan.contexts.get(sym) if state.latest_scan else None)
        signals = (state.latest_scan.signals_by_symbol.get(sym, []) if state.latest_scan else [])
        recs = [r for r in (state.latest_scan.recommendations if state.latest_scan else []) if r.symbol == sym]
        rejected = [(r, why) for r, why in (state.latest_scan.rejected if state.latest_scan else []) if r.symbol == sym]
        ledger = state.orchestrator.ledger
        per_account: dict[str, dict] = {}
        for key, c in state.cfg.accounts.accounts.items():
            per_account[key] = {
                "config": c,
                "shares": ledger.get_shares(key, sym),
                "options": ledger.open_options_for(key, sym),
                "wheel_state": ledger.get_wheel_state(key, sym),
                "premiums": ledger.premiums_collected(key, sym),
            }
        return templates.TemplateResponse(
            request, "ticker.html",
            {
                "state": state,
                "symbol": sym,
                "ctx": ctx,
                "signals": signals,
                "recommendations": recs,
                "rejected": rejected,
                "per_account": per_account,
            },
        )

    # ─── Accounts ────────────────────────────────────────────────────────

    @app.get("/accounts", response_class=HTMLResponse)
    async def accounts(request: Request):
        state = get_app_state()
        snapshots = {
            key: snapshot_account(key, c, state.orchestrator.ledger)
            for key, c in state.cfg.accounts.accounts.items()
        }
        return templates.TemplateResponse(
            request, "accounts.html",
            {"state": state, "snapshots": snapshots},
        )

    @app.post("/positions/shares")
    async def add_shares(
        account: str = Form(...),
        symbol: str = Form(...),
        shares: int = Form(...),
        price: float = Form(...),
    ):
        state = get_app_state()
        if account not in state.cfg.accounts.accounts:
            raise HTTPException(400, f"unknown account {account!r}")
        if shares < 100 or shares % 100 != 0:
            raise HTTPException(400, "shares must be a positive multiple of 100")
        if price <= 0:
            raise HTTPException(400, "price must be positive")
        apply(state.orchestrator.ledger, WheelEventPayload(
            account_key=account, symbol=symbol.upper(),
            event=WheelEvent.BUY_SHARES_DIRECT,
            occurred_at=datetime.now(),
            shares=shares, price_per_share=price,
        ))
        return RedirectResponse("/accounts", status_code=303)

    @app.post("/positions/option")
    async def add_option(
        account: str = Form(...),
        symbol: str = Form(...),
        opt_type: str = Form(..., alias="type"),
        side: str = Form("short"),
        strike: float = Form(...),
        expiry: str = Form(...),
        contracts: int = Form(1),
        premium: float = Form(...),
    ):
        state = get_app_state()
        if account not in state.cfg.accounts.accounts:
            raise HTTPException(400, f"unknown account")
        if opt_type not in {"put", "call"} or side not in {"short", "long"}:
            raise HTTPException(400, "bad type/side")
        try:
            _date.fromisoformat(expiry)
        except ValueError:
            raise HTTPException(400, "expiry must be YYYY-MM-DD")
        if side == "short":
            event = WheelEvent.SELL_CSP if opt_type == "put" else WheelEvent.SELL_CC
            apply(state.orchestrator.ledger, WheelEventPayload(
                account_key=account, symbol=symbol.upper(),
                event=event, occurred_at=datetime.now(),
                strike=strike, expiry_iso=expiry, contracts=contracts,
                premium_per_share=premium,
            ))
        else:
            from datetime import date as _d
            ledger = state.orchestrator.ledger
            ledger.insert_option(OptionPosition(
                account_key=account, symbol=symbol.upper(),
                expiry=_d.fromisoformat(expiry), strike=strike,
                type=OptionType(opt_type), side=OptionSide(side),
                contracts=contracts, entry_price=premium,
                opened_at=datetime.now(),
            ))
        return RedirectResponse("/accounts", status_code=303)

    @app.post("/positions/shares/remove")
    async def remove_shares(account: str = Form(...), symbol: str = Form(...)):
        state = get_app_state()
        state.orchestrator.ledger.delete_shares(account, symbol.upper())
        state.orchestrator.ledger.set_wheel_state(account, symbol.upper(), WheelState.CASH)
        return RedirectResponse("/accounts", status_code=303)

    @app.post("/positions/option/close")
    async def close_option(account: str = Form(...), option_id: int = Form(...), close_price: float = Form(0.0)):
        state = get_app_state()
        ledger = state.orchestrator.ledger
        # Look up the option to compute realized P&L on the way out
        with ledger.connect() as conn:
            row = conn.execute("SELECT * FROM option_positions WHERE id = ?", (option_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "option not found")
        entry_credit = row["entry_price"] * 100 * row["contracts"]
        close_cost = close_price * 100 * row["contracts"]
        sign = 1 if row["side"] == "short" else -1
        pnl = sign * (entry_credit - close_cost)
        ledger.close_option(option_id, close_price, pnl)
        return RedirectResponse("/accounts", status_code=303)

    # ─── Scout ───────────────────────────────────────────────────────────

    @app.get("/scout", response_class=HTMLResponse)
    async def scout(request: Request):
        state = get_app_state()
        return templates.TemplateResponse(
            request, "scout.html",
            {"state": state, "finds": state.latest_scout},
        )

    @app.post("/scout/refresh")
    async def scout_refresh():
        """Scout is no-LLM, so this is safe to run directly from the dashboard."""
        state = get_app_state()
        async def _go():
            try:
                finds = await state.orchestrator.run_scout(top_n=5)
                await state.set_latest_scout(finds)
            except Exception as e:
                await state.set_error(f"scout: {e}")
        asyncio.create_task(_go())
        return RedirectResponse("/scout", status_code=303)

    # ─── Tools (paste-chain, paste-portfolio) ────────────────────────────

    @app.get("/tools", response_class=HTMLResponse)
    async def tools(request: Request):
        state = get_app_state()
        return templates.TemplateResponse(request, "tools.html", {"state": state})

    @app.post("/tools/paste-chain", response_class=HTMLResponse)
    async def paste_chain(
        request: Request,
        symbol: str = Form(...), expiry: str = Form(...),
        spot: float = Form(...), text: str = Form(...),
    ):
        state = get_app_state()
        try:
            chain = parse_chain(text, symbol, _date.fromisoformat(expiry), spot_price=spot)
        except (ChainParseError, ValueError) as e:
            return templates.TemplateResponse(
                request, "tools.html",
                {"state": state, "chain_error": str(e), "chain_text": text,
                 "chain_symbol": symbol, "chain_expiry": expiry, "chain_spot": spot},
            )
        return templates.TemplateResponse(
            request, "_chain_result.html",
            {"chain": chain, "symbol": symbol.upper(), "expiry": expiry, "spot": spot},
        )

    @app.post("/tools/paste-portfolio", response_class=HTMLResponse)
    async def paste_portfolio(
        request: Request,
        account: str = Form(...), text: str = Form(...),
    ):
        state = get_app_state()
        if account not in state.cfg.accounts.accounts:
            raise HTTPException(400, f"unknown account")
        from datetime import datetime as _dt
        ledger = state.orchestrator.ledger
        n_shares = n_opts = 0
        errors: list[str] = []
        for line in text.splitlines():
            parts = line.strip().split()
            if not parts:
                continue
            try:
                if parts[0].upper() == "SHARES:" and len(parts) >= 4:
                    sym, sh, avg = parts[1].upper(), int(parts[2]), float(parts[3])
                    ledger.upsert_shares(SharesPosition(
                        account_key=account, symbol=sym,
                        shares=sh, total_cost=sh * avg,
                        premiums_collected=0.0,
                        opened_at=_dt.now(), last_updated=_dt.now(),
                    ))
                    n_shares += 1
                elif parts[0].upper() == "OPTION:" and len(parts) >= 7:
                    sym = parts[1].upper(); tp = parts[2].lower()
                    strike = float(parts[3]); exp = _date.fromisoformat(parts[4])
                    contracts = int(parts[5]); premium = float(parts[6])
                    side = next((p.split("=")[1] for p in parts[7:] if p.startswith("side=")), "short")
                    ledger.insert_option(OptionPosition(
                        account_key=account, symbol=sym, expiry=exp, strike=strike,
                        type=OptionType(tp), side=OptionSide(side),
                        contracts=contracts, entry_price=premium,
                        opened_at=_dt.now(),
                    ))
                    n_opts += 1
                else:
                    errors.append(f"unparsed: {line}")
            except (ValueError, KeyError) as e:
                errors.append(f"{line!r}: {e}")
        return templates.TemplateResponse(
            request, "_portfolio_result.html",
            {"n_shares": n_shares, "n_opts": n_opts, "errors": errors, "account": account},
        )

    # ─── Reports ─────────────────────────────────────────────────────────

    @app.get("/reports", response_class=HTMLResponse)
    async def reports_index(request: Request):
        state = get_app_state()
        reports_dir = state.cfg.root / "reports"
        daily = []
        quarterly = []
        if reports_dir.exists():
            daily = sorted(reports_dir.glob("*.md"), reverse=True)
            qdir = reports_dir / "quarterly"
            if qdir.exists():
                quarterly = sorted(qdir.glob("*.md"), reverse=True)
        return templates.TemplateResponse(
            request, "reports.html",
            {"state": state, "daily": daily, "quarterly": quarterly},
        )

    @app.get("/reports/view", response_class=HTMLResponse)
    async def reports_view(request: Request, path: str):
        state = get_app_state()
        reports_dir = state.cfg.root / "reports"
        p = (reports_dir / path).resolve()
        # Safety: ensure resolved path is inside reports_dir
        if not str(p).startswith(str(reports_dir.resolve())):
            raise HTTPException(403, "forbidden")
        if not p.exists():
            raise HTTPException(404)
        text = p.read_text(encoding="utf-8")
        html = _md_to_html(text)
        return templates.TemplateResponse(
            request, "report_view.html",
            {"state": state, "title": p.name, "html": html, "path": str(p.relative_to(reports_dir))},
        )

    @app.post("/reports/daily/generate", response_class=HTMLResponse)
    async def reports_generate_daily(request: Request):
        """Disabled — daily reports now produced via Claude Code (free)."""
        return HTMLResponse(
            "Daily reports are written via Claude Code (your Pro/Max subscription). "
            "Open Claude Code in this folder and ask for a daily report.",
            status_code=410,
        )

    @app.post("/reports/ticker/generate", response_class=HTMLResponse)
    async def reports_generate_ticker(symbol: str = Form(...)):
        """Disabled — quarterly reports now produced via Claude Code (free)."""
        return HTMLResponse(
            "Quarterly reports are written via Claude Code (your Pro/Max subscription). "
            f"Open Claude Code and ask for a deep-dive on {symbol}.",
            status_code=410,
        )

    # ─── SSE event stream ────────────────────────────────────────────────

    @app.get("/events")
    async def events():
        state = get_app_state()
        queue = state.new_subscriber()

        async def stream():
            try:
                # Send an initial heartbeat
                yield {"event": "hello", "data": json.dumps({"at": utc_now().isoformat()})}
                while True:
                    try:
                        msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        # heartbeat
                        yield {"event": "ping", "data": "{}"}
                        continue
                    yield {"event": msg.get("event", "message"), "data": json.dumps(msg, default=str)}
            finally:
                state.drop_subscriber(queue)

        return EventSourceResponse(stream())


# ─── Helpers ───────────────────────────────────────────────────────────────


def _md_to_html(text: str) -> str:
    """Tiny markdown→HTML for report rendering. Doesn't pull a full markdown dep."""
    import re
    out: list[str] = []
    in_table = False
    in_code = False
    code_lang = ""
    for line in text.splitlines():
        if line.strip().startswith("```"):
            if not in_code:
                code_lang = line.strip()[3:]
                out.append("<pre><code>")
                in_code = True
            else:
                out.append("</code></pre>")
                in_code = False
            continue
        if in_code:
            out.append(_esc(line))
            continue
        if line.startswith("# "):
            out.append(f"<h1>{_esc(line[2:])}</h1>")
        elif line.startswith("## "):
            out.append(f"<h2>{_esc(line[3:])}</h2>")
        elif line.startswith("### "):
            out.append(f"<h3>{_esc(line[4:])}</h3>")
        elif line.startswith("|") and "|" in line[1:]:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(set(c) <= {"-", ":", " "} for c in cells):
                continue  # divider row
            if not in_table:
                out.append("<table>")
                in_table = True
                out.append("<tr>" + "".join(f"<th>{_inline(c)}</th>" for c in cells) + "</tr>")
            else:
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
        elif line.startswith("- "):
            out.append(f"<li>{_inline(line[2:])}</li>")
        elif line.strip() == "":
            if in_table:
                out.append("</table>")
                in_table = False
            out.append("")
        else:
            out.append(f"<p>{_inline(line)}</p>")
    if in_table:
        out.append("</table>")
    return "\n".join(out)


def _inline(s: str) -> str:
    import re
    s = _esc(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
    return s


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
