"""Parse pasted options chain text into an OptionsChain.

Supports several common broker / website formats — the parser autodetects which
one by inspecting the header row. Today the parser handles:

- Generic CSV (e.g. Yahoo Finance "Show columns" export):
    Strike,Bid,Ask,Last,Volume,OpenInterest,ImpliedVolatility,Delta?,...

- Schwab/StreetSmart format (puts and calls side-by-side):
    "Last Trade,Net Chg,Bid,Ask,Volume,Open Int,IV,Strike,IV,Open Int,Volume,Ask,Bid,Net Chg,Last Trade"
    (Calls on the left of Strike, Puts on the right)

- Fidelity Active Trader Pro:
    "Strike,Calls Bid,Calls Ask,Calls Last,Calls Vol,Calls OI,Calls IV,Puts Bid,Puts Ask,..."

- Plain text grid: lines with at least 4 numeric columns are interpreted as
  strike/bid/ask/last by best guess. Falls back to "unknown format" error.

The function returns an `OptionsChain` augmented from yfinance's structure.
The Symbol+Expiry must be passed in (the user knows which chain they pasted).
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from io import StringIO

from ..portfolio.types import OptionContract, OptionsChain, OptionType
from ..utils.time import utc_now

log = logging.getLogger(__name__)


class ChainParseError(ValueError):
    """Raised when the parser cannot make sense of the input."""


def parse_chain(
    text: str, symbol: str, expiry: date, spot_price: float,
) -> OptionsChain:
    """Parse pasted text into an OptionsChain.

    Args:
      text: the raw pasted chain (with or without header rows).
      symbol: ticker symbol (the parser doesn't infer this).
      expiry: expiration date of this chain.
      spot_price: current underlying price (for context/sorting).

    Raises ChainParseError if no parseable rows are found.
    """
    # Normalize: convert tabs to commas, drop currency symbols
    cleaned = text.replace("\t", ",").replace("$", "")
    rows = [r.strip() for r in cleaned.splitlines() if r.strip()]
    if not rows:
        raise ChainParseError("Empty input")

    header_idx = _find_header(rows)
    if header_idx is None:
        # Try generic columnar parse — assume strike,bid,ask,last,vol,oi,iv
        contracts = _parse_generic_numeric(rows, symbol, expiry, OptionType.PUT)
        if not contracts:
            raise ChainParseError("No parseable rows; pass headerized data")
        return _build_chain(symbol, expiry, spot_price, contracts)

    header = [c.strip().lower() for c in rows[header_idx].split(",")]
    data_rows = rows[header_idx + 1:]

    layout = _detect_layout(header)
    log.debug("chain parser detected layout: %s", layout)

    if layout == "side_by_side":
        contracts = _parse_side_by_side(data_rows, header, symbol, expiry)
    elif layout == "stacked":
        contracts = _parse_stacked(data_rows, header, symbol, expiry)
    else:
        raise ChainParseError(f"Unrecognized header: {header}")

    if not contracts:
        raise ChainParseError("Header parsed but no data rows produced contracts")
    return _build_chain(symbol, expiry, spot_price, contracts)


# ─── Header detection ───────────────────────────────────────────────────────


def _find_header(rows: list[str]) -> int | None:
    """Return the index of the first header row, or None if not found."""
    for i, r in enumerate(rows):
        lower = r.lower()
        if "strike" in lower and ("bid" in lower or "iv" in lower):
            return i
    return None


def _detect_layout(header: list[str]) -> str:
    """Side-by-side has "strike" in the middle with similar columns on each side."""
    if "strike" not in header:
        return "unknown"
    pos = header.index("strike")
    # Side-by-side: there's content both before AND after strike, with
    # mirrored column names (e.g. ["bid","ask","strike","ask","bid"]).
    if pos > 1 and pos < len(header) - 1:
        return "side_by_side"
    return "stacked"


# ─── Parsers ────────────────────────────────────────────────────────────────


def _parse_side_by_side(
    rows: list[str], header: list[str], symbol: str, expiry: date,
) -> list[OptionContract]:
    """Schwab/Fidelity-style layout: calls on left of Strike, puts on right."""
    strike_idx = header.index("strike")
    left_header = header[:strike_idx]
    right_header = header[strike_idx + 1:]
    out: list[OptionContract] = []
    for line in rows:
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < len(header):
            continue
        try:
            strike = _to_float(cells[strike_idx])
        except ValueError:
            continue
        if strike is None:
            continue
        left_cells = cells[:strike_idx]
        right_cells = cells[strike_idx + 1:]
        call = _make_contract(left_header, left_cells, OptionType.CALL, symbol, strike, expiry)
        put = _make_contract(right_header, right_cells, OptionType.PUT, symbol, strike, expiry)
        if call:
            out.append(call)
        if put:
            out.append(put)
    return out


def _parse_stacked(
    rows: list[str], header: list[str], symbol: str, expiry: date,
) -> list[OptionContract]:
    """Header like: Strike, Bid, Ask, Last, Volume, OI, IV — one option type per chain."""
    opt_type = OptionType.CALL if "call" in " ".join(header) else (
        OptionType.PUT if "put" in " ".join(header) else OptionType.PUT
    )
    strike_idx = header.index("strike")
    out: list[OptionContract] = []
    for line in rows:
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < len(header):
            continue
        try:
            strike = _to_float(cells[strike_idx])
        except (ValueError, IndexError):
            continue
        if strike is None:
            continue
        c = _make_contract(header, cells, opt_type, symbol, strike, expiry)
        if c:
            out.append(c)
    return out


def _parse_generic_numeric(
    rows: list[str], symbol: str, expiry: date, opt_type: OptionType,
) -> list[OptionContract]:
    """Last-resort: rows with ≥ 4 numeric tokens are interpreted as
    strike, bid, ask, last [, volume, oi, iv]."""
    out: list[OptionContract] = []
    for line in rows:
        cells = re.split(r"[,\s]+", line.strip())
        nums: list[float] = []
        for c in cells:
            try:
                nums.append(float(c.replace("%", "")))
            except ValueError:
                continue
        if len(nums) < 4:
            continue
        strike, bid, ask, last = nums[:4]
        volume = int(nums[4]) if len(nums) > 4 else 0
        oi = int(nums[5]) if len(nums) > 5 else 0
        iv = nums[6] / 100.0 if len(nums) > 6 else 0.0
        out.append(OptionContract(
            symbol=symbol.upper(), expiry=expiry, strike=strike,
            type=opt_type, bid=bid, ask=ask, last=last,
            volume=volume, open_interest=oi,
            implied_volatility=iv,
            delta=None, gamma=None, theta=None, vega=None,
        ))
    return out


def _make_contract(
    header: list[str], cells: list[str],
    opt_type: OptionType, symbol: str, strike: float, expiry: date,
) -> OptionContract | None:
    def field(*names: str, default: float = 0.0) -> float:
        for n in names:
            for i, h in enumerate(header):
                if n in h:
                    try:
                        v = _to_float(cells[i])
                        if v is None:
                            continue
                        # IV often comes as a percent string
                        if "iv" in n and v > 5.0:
                            return v / 100.0
                        return v
                    except (ValueError, IndexError):
                        continue
        return default

    bid = field("bid")
    ask = field("ask")
    last = field("last")
    volume = int(field("vol", default=0))
    oi = int(field("open", default=0)) or int(field("oi", default=0))
    iv = field("iv")
    if bid == 0 and ask == 0 and last == 0:
        return None
    return OptionContract(
        symbol=symbol.upper(), expiry=expiry, strike=strike,
        type=opt_type, bid=bid, ask=ask, last=last,
        volume=volume, open_interest=oi,
        implied_volatility=iv,
        delta=None, gamma=None, theta=None, vega=None,
    )


def _to_float(s: str) -> float | None:
    s = s.strip().replace(",", "").replace("%", "").replace("$", "")
    if not s or s in {"--", "-", "N/A", "n/a"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _build_chain(
    symbol: str, expiry: date, spot_price: float, contracts: list[OptionContract],
) -> OptionsChain:
    return OptionsChain(
        symbol=symbol.upper(),
        fetched_at=utc_now(),
        spot_price=spot_price,
        expiries=[expiry],
        contracts=sorted(contracts, key=lambda c: (c.type.value, c.strike)),
    )
