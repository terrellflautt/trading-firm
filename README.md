# Trading Firm — New User Edition

Locally-hosted, wheel-strategy trading firm that adapts to **your** accounts
and watchlist. **No API costs** — all LLM reasoning runs through Claude Code
(your Pro/Max subscription) talking to a local MCP server. The firm itself is
a free local data + ledger + analytics tool.

> Status: 109 tests passing. Fully manual scan model, no automated API
> daemons. Run `firm init` once, then open Claude Code and start trading.

---

## Quick start

```bash
# Open Claude Code in this folder. That's it.
cd trading-firm-fresh-start-for-new-user
claude
```

On first launch Claude Code will auto-discover the firm's MCP server, then
walk you through 4 setup questions in chat (capital, margin, IRA, tickers)
and write your config files. You're trading in under a minute.

Once setup is done, talk to it like a desk analyst:

```
"what time is it / what market phase"
"plan today"
"deep dive on AAPL"
"what's the regime on NVDA"           ← Quant agent / Markov regime
"scout for new opportunities"
"validate sell 1 AAPL 180 put 2026-06-19 for $1.80 on cash"
"record that I bought 100 INTC shares at $25.50 in cash"
```

### Prerequisites

- **Claude Code** installed (any plan: Pro / Max / Team)
- **`uv`** — Astral's Python package manager: `pip install uv` or
  `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Python 3.12+ (uv can install this for you)

The first `claude` launch will trigger `uv` to sync dependencies
automatically — no separate `uv sync` step needed.

### Prefer the shell?

You can still run the old interactive CLI setup if you want — equivalent
to the in-chat flow:

```bash
uv run firm init
```

Or hand-edit `config/accounts.yaml` and `config/watchlist.yaml` after the
fact — they're plain YAML.

### Importing your existing positions

If you already have shares or open options, paste them in after `firm init`:

```bash
uv run firm position import-paste -a cash
# then paste lines, one per position:
#   SHARES: AAPL 100 175.50
#   OPTION: AAPL put 175 2026-06-19 1 1.85 side=short
# then Ctrl-D (Unix) or Ctrl-Z+Enter (Windows)
```

Or just tell Claude Code in chat — "I own 100 AMD at $145 and a short 140 put
expiring June 19 for $2.10" — and it will call `add_shares_position` and
`record_option_position` for you.

Optional browser dashboard for read-only viewing:
```bash
uv run firm start               # http://localhost:8088
uv run firm stop                # clean shutdown
```

---

## The MCP server — what tools are exposed

When you open this folder with Claude Code, the firm's MCP server is
auto-discovered (via `.mcp.json`) and presents these 13 tools — including
the deterministic **Quant agent** (`get_regime_state`). They're all pure
local data — **zero API calls**.

### Time and market phase

#### `get_current_time()`
Returns current time in three zones (your local Central, NY exchange time, UTC)
plus the current market phase: `pre-market` / `regular hours (market open)` /
`after-hours` / `overnight` / `weekend (market closed)`. Also returns minutes
until next open or close if applicable.

Always call this at the start of a planning session.

### Per-ticker data

#### `get_ticker_context(symbol, format="markdown")`
Complete data snapshot for one ticker: quote, technical indicators (VWAP, ADX,
RSI, MACD, Bollinger, S/R), IV rank + ATM IV + realized vol + expected move,
fundamentals (cash runway, debt, FCF), options chain summary across 3 expiries,
recent headlines from Yahoo/SeekingAlpha RSS, wheel state per account, and any
user-supplied report sections.

This is THE primary tool for analyzing one ticker. Call it first whenever the
user names a symbol. `format="json"` for structured output, `"markdown"` for
human-readable.

#### `get_firm_context(format="markdown")`
Heavy version — full snapshot of **accounts + every watchlist ticker**. Use at
the start of a "plan today" session. Returns ~5-10KB markdown with everything
needed to construct trade recommendations across the watchlist.

#### `get_wheel_state(symbol)`
Quick check of just the wheel state (CASH / SHORT_PUT / LONG_SHARES / COVERED /
CALLED_AWAY_PENDING) for one ticker across both accounts, plus open shares /
options / premium collected lifetime. JSON.

### Position management

#### `list_positions(account=None)`
All shares + open options across accounts. Optionally filter to one account.

#### `add_shares_position(account, symbol, shares, price)`
Records buying shares into the ledger. Updates wheel state to `LONG_SHARES`.
Use whenever the user reports they actually executed a share buy.

#### `record_option_position(account, symbol, option_type, side, strike, expiry, contracts, premium)`
Records an option position. For short positions (CSP/CC), updates wheel state
and credits the premium ledger. Use whenever the user reports they executed an
option trade.

### Opportunity discovery

#### `scout_now(top_n=5)`
Runs the Scout: finds new tickers **outside** the watchlist using Finviz sector
screens + earnings movers + unusual options activity. Returns ranked candidates
with score, IV rank, sector, account-fit, indicator summary. Pure local — no LLM.

### Risk + validation

#### `validate_ticket(account, symbol, action, qty, limit, strike=None, expiry=None)`
Runs a proposed trade through the firm's Risk Manager. Returns ACCEPTED or
REJECTED with reasons. Rules enforced:
- 100-share minimum on share trades
- IRA constraints (no margin, no naked options)
- Per-ticker concentration cap (Roth 25%, Cash 30% by default)
- Strike must clear effective cost basis for covered calls
- Minimum annualized premium yield (15% default)
- Affordability under account free cash

**Always call this before recommending a ticket to the user.**

### User content

#### `parse_options_chain(symbol, expiry, spot, text)`
Parses pasted options chain text (Yahoo/Schwab/Fidelity/TastyTrade CSV) into
structured data. Use when the user wants fresher chain data than yfinance has.

### Reports

#### `list_reports()`
Lists daily and quarterly markdown report files already on disk.

#### `read_report(filename)`
Reads a previously generated report. Filename only — no paths (security).

---

## Strategy: the Wheel

1. Sell cash-secured puts on tickers you'd be happy to own, at strikes you'd
   be happy paying. Collect premium.
2. If assigned → you now own 100 shares. Sell covered calls above your cost
   basis. Collect more premium.
3. Premium income lowers your effective cost basis. Buy more shares on dips
   to lower further.
4. Eventually called away at a profit. Back to step 1.

Variants supported:
- Long-dated calls (LEAPS) on high-conviction setups — rare.
- Long-put hedges on assigned positions in the cash account — rare.

---

## Constraints encoded in the firm

- **100-share minimum** on any trade (wheel requires standard option lots)
- **Roth IRA**: no margin (IRS rule), only CSP + covered calls
- **Cash account**: optional 2x margin, long options allowed
- **Concentration cap**: per-ticker max % of account
- **Premium-yield floor**: skip thin premiums
- Risk Manager enforces all of these — both the CLI and MCP `validate_ticket`

---

## CLI reference (also no-API)

```bash
uv run firm context IONQ              # Full data snapshot for one ticker (markdown)
uv run firm context IONQ --format json
uv run firm context --all             # Every watchlist ticker
uv run firm scout --top 5             # Find new opportunities
uv run firm validate -a roth_ira -s IONQ --action sell_csp --qty 1 --limit 1.80 --strike 49 --expiry 2026-05-29
uv run firm position list             # See current positions
uv run firm position add-shares -a cash -s INTC --shares 100 --price 25.00
uv run firm position add-option -a cash -s INTC --type call --strike 28 --expiry 2026-06-18 --premium 0.50
uv run firm config validate           # Sanity-check YAMLs

uv run firm start                     # Launch dashboard (no auto-scans)
uv run firm stop                      # Clean shutdown
uv run firm status                    # Check running state

uv run firm mcp                       # Start MCP server (auto-launched by Claude Code)
```

`firm scan` exists but is gated behind `--use-api` so you can't accidentally
trigger paid API calls.

---

## Edit your config

- `config/accounts.yaml` — Roth + cash account: capital, margin toggle, rules
- `config/watchlist.yaml` — Tickers grouped by sector + scout filters
- `config/firm.yaml` — Dashboard port, risk thresholds, schedule (unused now)

---

## File layout

```
firm/
  mcp_server.py            # 12 MCP tools exposed to Claude Code
  context_dump.py          # Raw data snapshot functions (used by MCP)
  cli.py                   # All CLI commands
  orchestrator.py          # Internal scan coordinator (used if --use-api)
  agents/                  # Analyst + specialist agents (--use-api only)
  strategy/                # Wheel state machine, indicators, options math
  data/                    # yfinance, Barchart, Finviz, news, chain parser
  portfolio/               # Accounts, positions, ledger, wheel state, types
  dashboard/               # FastAPI + HTMX + SSE (read-only mode)
  reports/                 # Daily / quarterly markdown generators
  utils/                   # Helpers (time)
  scheduler.py             # Time-of-day scheduler (kept dormant)

config/                    # YAML configs
data_store/                # SQLite ledger + cache (gitignored)
reports/                   # Generated reports (gitignored)
tests/                     # 92 tests
.mcp.json                  # Tells Claude Code about the firm MCP server
.env                       # API key INTENTIONALLY EMPTY
CLAUDE.md                  # Project context for future Claude Code sessions
```

---

## Daily workflow (example)

```
06:00am CT  Wake up
06:05       cd Trading-Firm && claude
06:06       "What time is it and what's the market phase?"
06:07       "Plan today. Look at my watchlist + run the scout."
06:10       I call get_firm_context, scout_now, validate proposed tickets,
            present ranked list. Maybe 2-4 actionable tickets.
06:15       You decide which trades to place.
08:30       Market opens — you place orders in your broker.
09:00       "Anything change?" — I pull fresh context for the tickers we acted on.
12:30       "Power hour check incoming. Anything to roll or open?"
14:30       Last look. Maybe close-of-day adjustments.
15:00       "Write today's daily report and tell me how my premium income
            stacks up YTD."
```

You can also paste:
- Your broker portfolio (`"here's my portfolio: SHARES: IONQ 200 45.50..."`) — I'll call `add_shares_position` and `record_option_position` to sync the ledger
- A fresh options chain when yfinance is too stale (`"here's the IONQ chain..."`) — I'll call `parse_options_chain` and analyze it

---

## Why no auto-daemon?

We tried a 5-times-per-day scheduled API daemon. The math:
- 5 scans/day × ~$1.70/scan on Sonnet = $190/month
- Same on Opus: ~$925/month
- For a $25k account, that eats your edge

Killed it. Now every scan is on-demand. **You only pay your existing
Pro/Max subscription, nothing more.** The trade-off is you have to open
Claude Code when you want a scan — but in practice that's seconds, and you
gain total control over timing and depth.

---

## Phase history

- **Phase 1**: Foundation — config, ledger, indicators (VWAP/ADX/RSI/MACD),
  wheel state machine, risk manager, CLI scan. ~4k LOC, 42 tests.
- **Phase 2**: Full agent roster (Technical/Volatility/Fundamental/Sentiment
  analysts + Stock/Call/Put specialists), Scout, daily + quarterly reports,
  RTF report ingestion, options-chain paste parser. ~7.4k LOC, 85 tests.
- **Phase 3**: FastAPI + HTMX dashboard with SSE live updates, 7 pages,
  position forms, paste tools, lifecycle (start/stop/status). ~9.4k LOC,
  92 tests.
- **Phase 4** (today): MCP-first refactor. Removed all auto-API code paths,
  exposed firm as 12 MCP tools for Claude Code integration. Zero API cost.

See `CLAUDE.md` for what's next.
