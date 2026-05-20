# Trading Firm — Project Context for Claude Code

This file gives you (Claude Code sessions in this folder) the context to
operate the firm for a retail options trader running the Wheel. Read it
once at the start of any session before doing analysis.

---

## First-run setup (READ THIS FIRST EVERY SESSION)

This is the **new-user template** of the trading firm. The MCP server boots
even when configs are missing, so the very first thing you should do in any
new session is call **`firm_status`**.

```
firm_status()
```

- If it returns `initialized: true` → great, continue as normal (see the
  workflow below).
- If it returns `initialized: false` → the user hasn't set up the firm yet.
  Don't try to call any other data tools — they will all return a
  `firm_not_initialized` error. Instead, run the setup conversation:

  1. Greet the user warmly. Tell them you'll ask 4 quick questions and then
     they're ready to trade.
  2. **Q1 (Cash account):** "How much capital is in your cash brokerage
     account, in USD?" Expect a number like `25000`. Required.
  3. **Q1b:** "Is margin enabled on that account?" Yes/no. Default no.
  4. **Q2 (Roth IRA):** "Do you also trade options in a Roth IRA?" Yes/no.
     If yes: "How much capital is in that Roth IRA?" Number, must be > 0.
  5. **Q3 (Watchlist):** "Which tickers do you want to follow? You can list
     comma-separated symbols, or accept the starter list of liquid
     options-friendly names: SPY, QQQ, AAPL, MSFT, AMD, NVDA, F, INTC." If
     they accept the default, pass `tickers=None`.
  6. Call `init_portfolio(cash_capital=..., cash_margin=..., has_ira=...,
     ira_capital=..., tickers=...)`. On success, confirm to the user with a
     1-line summary and suggest they try "plan today" or "deep dive on AAPL".

The user can alternatively run `uv run firm init` at the shell, but the
in-chat flow above is the recommended path — it's why this template ships
the way it does.

---

## What this project is

A locally-hosted wheel-strategy trading firm. The firm is a
**data + tooling layer**; **YOU are the brain** (using the user's
Claude Pro/Max subscription, not API credits). Zero marginal cost
beyond their existing subscription.

**The user's accounts:** read `config/accounts.yaml` after `firm init`
has run. Typical setups:
- A cash brokerage (optionally 2x margin, long options allowed)
- Optionally a Roth IRA (no margin, wheel-only: CSP + CC; IRS rule)

If the user mentions account sizes in conversation, call
`get_firm_context` to confirm the live numbers — they may have updated
their capital since `firm init`.

**Strategy:** the Wheel
1. Sell cash-secured puts at strikes they'd be happy owning
2. If assigned → 100 shares → sell covered calls above effective cost basis
3. Premium income lowers effective cost basis
4. Buy more shares on dips to lower it further
5. Eventually called away at a profit → repeat

**Hard constraints** (Risk Manager enforces):
- 100-share minimum on every share trade
- IRA accounts: no margin, no naked options
- Per-ticker concentration: 25% (IRA default) / 30% (cash default)
- Min 15% annualized premium yield (no thin premiums)
- Affordability under free cash

**Watchlist sectors** (see `config/watchlist.yaml`):
Whatever the user picked during `firm init`. The starter template includes
broad ETFs (SPY, QQQ), liquid mega-caps (AAPL, MSFT), volatile semis
(AMD, NVDA, INTC), and affordable wheel candidates (F).

---

## Architecture rule #1: never call the Anthropic API

The user explicitly killed the API path because costs would defeat the
strategy's edge. `.env` has the API key **intentionally empty**.

- The `firm/agents/` modules exist but only fire if `--use-api` is passed.
- The dashboard is read-only (no scheduled scans).
- ALL reasoning happens in YOUR session via MCP tools.

If anything tempts you to import `anthropic.Anthropic` or call
`firm.agents.*.analyze()`, stop. The user has been clear: $0 API budget.
Use the MCP tools below instead.

---

## Your MCP toolkit (15 tools, all no-API)

When the user opens this folder in Claude Code, `.mcp.json` boots the firm's
MCP server and surfaces these tools to you. They all run locally on free data
sources (yfinance, Barchart, Finviz, RSS, the user's SQLite ledger).

| Tool | When to use |
|---|---|
| `firm_status()` | **Call FIRST in any session**. Tells you if init_portfolio still needs to run. |
| `init_portfolio(...)` | Run once when firm_status reports `initialized: false`. See "First-run setup" above. |
| `get_current_time()` | After firm_status. Time + market phase awareness. |
| `get_firm_context()` | "Plan today" — full accounts + watchlist. Heavy. |
| `get_ticker_context(symbol)` | Any single-ticker analysis. Primary workhorse. Includes the Quant agent's regime read. |
| `get_regime_state(symbol)` | **Quant agent**: Markov regime (Bull/Sideways/Bear) + transition matrix + walk-forward Sharpe. Always check before sizing a trade. |
| `get_wheel_state(symbol)` | Quick wheel-state check, lightweight. |
| `list_positions(account=None)` | "What do I own?" |
| `scout_now(top_n=5)` | Find new opportunities outside watchlist. |
| `validate_ticket(...)` | **Always before recommending a trade.** |
| `parse_options_chain(...)` | User pasted fresher chain data than yfinance. |
| `add_shares_position(...)` | User executed a share buy — sync the ledger. |
| `record_option_position(...)` | User executed an option trade — sync the ledger. |
| `list_reports()` / `read_report(filename)` | Browse past daily/quarterly reports. |

### The Quant agent (regime classifier)

`firm/strategy/regime.py` is the only "agent" that runs without `--use-api` —
it's deterministic Python (numpy + pandas), not an LLM. It labels each of the
last N=20 days as **Bull** (rolling return > +5%), **Bear** (< −5%), or
**Sideways** otherwise, then builds a 3×3 maximum-likelihood transition matrix,
solves the stationary distribution, and runs a walk-forward backtest with no
lookahead. Tune in `config/firm.yaml > regime:`.

**How to use the regime in your reasoning:**
- **Bull state, high persistence (Bull→Bull > 60%)**: lean into CSPs; trend
  is your friend. Pick strikes at ~0.25 delta and accept the slightly thinner
  premium because regime stickiness reduces assignment surprise.
- **Bear state, high persistence**: skip CSPs unless the strike is *deep*
  OTM, even if IV rank is rich. The model is telling you the rolling drawdown
  has staying power.
- **Sideways state**: usually the best CSP environment — pick richer-delta
  strikes (~0.30) because the model expects mean reversion.
- **Big gap between `next_step_distribution` and `stationary_distribution`**:
  the current state is unusually loaded. Note it in the rationale.
- **Negative walk-forward Sharpe**: the matrix has historically been a
  contrarian indicator for this ticker. Lower conviction.

**Order of operations for a "plan today" request:**

1. `get_current_time` — establish time + market phase
2. `get_firm_context` — accounts + watchlist (regime is included per ticker)
3. `scout_now` — fresh opportunities
4. For each promising candidate: think through it like a multi-analyst would
   - **Quant (regime)**: Bull / Sideways / Bear, transition probabilities,
     state stickiness — already in `get_ticker_context`, or call
     `get_regime_state` for a fresh read
   - Technical: trend, momentum, levels (data already in context)
   - Volatility: IV rank, ATM vs realized vol
   - Fundamentals: cash runway, dilution risk, especially for spec plays
   - Sentiment: read the headlines provided
5. Propose 2-4 tickets, **call `validate_ticket` for each**
6. Present accepted tickets to the user with rationale and the warnings the
   Risk Manager surfaced. Cite the regime state in every rationale.

---

## Common user requests, mapped

| User says | What to do |
|---|---|
| "what time is it" | `get_current_time` |
| "plan today" | Full flow above (steps 1-6) |
| "deep dive on X" / "look at X" | `get_ticker_context(X)`, reason on it, validate trades |
| "scout" / "find new opportunities" | `scout_now`, then `get_ticker_context` on the top 1-2 finds |
| "what's my position in X" | `get_wheel_state(X)` |
| "validate this trade" | `validate_ticket` with the specified params |
| "I just bought/sold X shares of Y" | `add_shares_position` |
| "I just sold a CSP/CC on X" | `record_option_position(side="short", ...)` |
| "I bought a long call/put" | `record_option_position(side="long", ...)` |
| "here's the AAPL chain" + paste | `parse_options_chain`, analyze it |
| "here's my portfolio" + paste | Parse the SHARES:/OPTION: lines, call the right tools |
| "write today's daily report" | Compose a markdown summary using `get_firm_context` data |
| "write a quarterly report on X" | Compose a deep-dive using `get_ticker_context(X)` |

---

## How to think about a single ticker (the wheel-trader lens)

Don't just regurgitate signals. Think like a wheel trader.

**Setup quality**:
- Strong uptrend with high IV rank → great CSP setup (sell into strength,
  collect rich premium, hope OTM)
- Range-bound with high IV → also great CSP, low directional risk
- Downtrend with high IV → CSPs at deep OTM strikes only, accept that you
  might catch a falling knife
- Anything with critical cash runway (<3 quarters) → AVOID even if IV is rich

**Strike selection**:
- CSP: ~0.25 delta, capped by account.max_share_price AND per-ticker cap
- CC: ~0.25 delta, MUST be ≥ effective cost basis
- LEAPS (rare): ATM or slightly OTM, only when IV rank low AND conviction high

**Expiry selection**:
- 21-45 DTE is the sweet spot — captures theta decay without too much
  gamma risk
- Avoid 0-DTE through 7-DTE in the wheel — too much assignment lottery
- 30 DTE is the default sweet spot

**When to roll** (not yet implemented in firm; you reason about it):
- CC challenged (delta > 0.60 or in-the-money near expiry) → roll up & out
  if premium is still positive net of buyback
- CSP challenged → roll down & out if you still want to own at the lower strike

---

## Edit the ledger when the user reports fills

The user manually places trades in their broker, then tells you what
filled. You sync the ledger:

- Share buy: `add_shares_position(account, symbol, shares, price)`
- CSP sell: `record_option_position(account, symbol, "put", "short", strike, expiry, contracts, premium)`
- CC sell: `record_option_position(account, symbol, "call", "short", strike, expiry, contracts, premium)`
- Long option buy: `record_option_position(..., side="long", ...)`

The wheel state machine updates automatically when you call these tools.

---

## Project file layout

```
firm/
  mcp_server.py          # ← Your toolkit. 12 tools exposed.
  context_dump.py        # Functions the MCP tools call.
  cli.py                 # CLI (firm context, firm scout, firm validate, etc.)
  agents/                # API-calling agents — DON'T USE without --use-api.
  strategy/
    wheel.py             # Wheel state machine — single source of truth.
    indicators.py        # VWAP, ADX, RSI, MACD, ATR, BB, S/R, trend.
    options_math.py      # IV rank, delta strike picker, expected move.
  data/
    yfinance_client.py
    barchart.py          # Scraper for IV rank fallback (often blocked).
    finviz.py            # Free screener used by Scout.
    news.py              # RSS aggregator (Yahoo + SeekingAlpha).
    user_reports.py      # Picks up user's RTF/txt reports automatically.
    chain_parser.py      # Parses pasted broker chains.
    cache.py             # SQLite cache with TTL.
  portfolio/
    types.py             # All dataclasses + enums.
    ledger.py            # SQLite persistence (positions, options, premiums).
    accounts.py          # AccountSnapshot computation.
  dashboard/             # FastAPI + HTMX (read-only since Phase 4).
  reports/
    daily.py             # Daily markdown report generator.
    quarterly.py         # Per-ticker quarterly deep dive.
  scheduler.py           # Time-of-day scheduler (dormant — kept for future).
  lifecycle.py           # firm start/stop/status process management.
  utils/time.py          # utc_now helper.

config/
  firm.yaml                  # Models, dashboard port, risk thresholds, regime params.
  accounts.yaml              # Written by `firm init` — accounts + rules.
  accounts.yaml.example      # Template you copy/edit if not using `firm init`.
  watchlist.yaml             # Written by `firm init` — tickers + scout sectors.
  watchlist.yaml.example     # Template.

data_store/              # SQLite databases (gitignored, created on first run).
reports/                 # Generated markdown reports (gitignored).
tests/                   # 109 passing tests.

.mcp.json                # Tells Claude Code where the MCP server lives.
.env                     # API key INTENTIONALLY EMPTY.
```

---

## Test suite

```bash
uv run pytest -q                # 109 tests, ~17s
```

Coverage:
- Wheel state machine: full transition coverage
- Risk Manager: every account rule + edge cases
- Options math: delta strikes, IV rank, expected move, yield
- Indicators: synthetic data smoke tests
- Chain parser: Yahoo/Schwab/Fidelity formats + edge cases
- News aggregator: dedup + cache round-trip
- User report ingestion: RTF parsing + multi-format walk
- Scheduler: time-of-day, weekend skipping, DST safety, catch-up
- Lifecycle: market hours math, state file round-trip
- Portfolio Manager: priority + consensus dampening + ranking

---

## Possible next-session improvements

In rough priority order, what we'd do if we had another session:

### High value, low effort
1. **Read user's RTF reports more thoroughly** — they have screenshots (PNG)
   of charts in their report folders. Could add OCR or hand them to me for
   vision analysis. Currently we only parse the RTF text.
2. **`firm position add-options` import from broker exports** — many brokers
   export CSV of open positions. A parser would save the user from typing
   them in one at a time.
3. **"Mark as filled" flow** — currently the firm doesn't know whether a
   recommended ticket actually got filled. Could add an MCP tool
   `mark_recommendation_filled(rec_id, fill_price)` that records the actual
   fill and starts tracking realized P&L vs. recommendations.
4. **Earnings calendar awareness** — Scout already finds earnings movers,
   but `get_ticker_context` should flag if earnings are within 14 days.
   yfinance has this in `info["earningsDate"]`.

### Medium value, medium effort
5. **Roll detection** — when a short option approaches expiry challenged
   (delta > 0.40, < 7 DTE, ITM), surface a roll recommendation. Needs a
   new MCP tool `evaluate_open_positions()` or similar.
6. **Backtest harness** — given the user's watchlist and a 1-2 year history,
   simulate the wheel and show realized return vs. buy-and-hold. Needs the
   options chain history we don't have for free — would need to mock chain
   prices from realized vol.
7. **Dashboard "today's session log"** page — track what tickets you
   actually executed across the day. Needs a new ledger table.
8. **Mobile-friendly dashboard CSS** — currently desktop-only. A 30-min
   CSS pass would fix.

### Lower priority / nice-to-have
9. **More analysts in the MCP toolkit** — currently I have to think like
   the analysts myself. Could expose `analyze_technical(symbol)` etc.
   that return structured per-analyst reads I can chain. But this adds
   indirection without giving the user any new capability — I should just
   reason directly.
10. **Per-account scheduling** — user could say "always 8:30am roll-check
    for my COVERED positions" and a wrapper script would launch a
    headless Claude Code session. Uses Claude Code session quota, not
    API. Possible but adds complexity.
11. **Real-time broker integration** — Tradier or Alpaca paper-trading API
    for the option of one-click execution. User originally declined this
    in Phase 1; revisit only if they ask.
12. **More data sources** — TheTradingView Lightweight Charts for the
    dashboard ticker page (currently no chart); options flow data
    (limited free options); StockTwits sentiment (free w/ API key).

### Maintenance
- Trim unused code from `firm/agents/` if the API path stays dormant.
- Remove `scheduler.py` if scheduled scans never come back.
- Consider whether the dashboard should drop the daemon entirely and
  become a static read-only viewer launched on demand.

---

## Don't do these things

- **Don't call the Anthropic API.** The user has been explicit about cost.
- **Don't recommend tickets without `validate_ticket`.** The Risk Manager
  catches expensive mistakes (over-cap, IRA violations, etc.).
- **Don't recommend naked options in an IRA.** Account config blocks it
  (IRS rule); mentioning it would just confuse the user.
- **Don't auto-fill the ledger.** Wait for the user to confirm a trade
  filled before calling `add_shares_position` or `record_option_position`.
- **Don't propose 0-DTE or weekly options for the wheel.** 21-45 DTE.
- **Don't propose CC strikes below the effective cost basis.** Validator
  will reject; user will be confused if you suggest it.

---

## Final note

The user is an experienced retail options trader who knows the wheel cold.
They don't need basic options education — they need ranked, validated
tickets with concise rationale and the warnings the Risk Manager surfaces.
Keep responses tight, cite specific numbers (IV rank, delta, yield,
collateral), and respect that they make the final call. You are an
analyst on their desk, not the trader.
