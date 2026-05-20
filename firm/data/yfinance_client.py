"""yfinance adapter — quotes, history, options chains, fundamentals.

All calls flow through the SQLite cache. yfinance is rate-limited and
flaky, so every method has a stale-data fallback path.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
import yfinance as yf

from ..config import DataConfig
from ..portfolio.types import (
    Fundamentals,
    OHLCBar,
    OptionContract,
    OptionsChain,
    OptionType,
    Quote,
)
from ..utils.time import utc_now
from .cache import Cache

log = logging.getLogger(__name__)


class YFClient:
    def __init__(self, cache: Cache, cfg: DataConfig):
        self.cache = cache
        self.cfg = cfg

    # ── Quote ──────────────────────────────────────────────────────────────

    def quote(self, symbol: str) -> Quote | None:
        sym = symbol.upper()
        key = f"yf:quote:{sym}"
        cached = self.cache.get(key)
        if cached is not None:
            data, _ = cached
            return self._quote_from_dict(data)
        try:
            t = yf.Ticker(sym)
            info = t.fast_info
            hist = t.history(period="2d", auto_adjust=False)
            if hist.empty:
                return self._maybe_stale_quote(key)
            last_row = hist.iloc[-1]
            prev_row = hist.iloc[-2] if len(hist) >= 2 else last_row
            avg_vol = None
            try:
                avg_vol = int(info.get("three_month_average_volume") or 0) or None
            except Exception:
                avg_vol = None
            data = {
                "symbol": sym,
                "price": float(info.get("last_price") or last_row["Close"]),
                "prev_close": float(prev_row["Close"]),
                "day_open": float(last_row["Open"]),
                "day_high": float(last_row["High"]),
                "day_low": float(last_row["Low"]),
                "volume": int(last_row["Volume"]),
                "avg_volume_30d": avg_vol,
                "timestamp": utc_now().isoformat(),
            }
            self.cache.set(key, data, self.cfg.yfinance_quote_ttl_seconds)
            return self._quote_from_dict(data)
        except Exception as e:
            log.warning("quote fetch failed for %s: %s", sym, e)
            return self._maybe_stale_quote(key)

    def _maybe_stale_quote(self, key: str) -> Quote | None:
        stale = self.cache.get(key, allow_stale=True)
        if stale is None:
            return None
        data, _ = stale
        return self._quote_from_dict(data)

    @staticmethod
    def _quote_from_dict(d: dict[str, Any]) -> Quote:
        return Quote(
            symbol=d["symbol"], price=float(d["price"]),
            prev_close=float(d["prev_close"]), day_open=float(d["day_open"]),
            day_high=float(d["day_high"]), day_low=float(d["day_low"]),
            volume=int(d["volume"]),
            avg_volume_30d=int(d["avg_volume_30d"]) if d.get("avg_volume_30d") else None,
            timestamp=datetime.fromisoformat(d["timestamp"]),
        )

    # ── History ────────────────────────────────────────────────────────────

    def history_daily(self, symbol: str, period: str = "3mo") -> list[OHLCBar]:
        sym = symbol.upper()
        key = f"yf:hist_d:{sym}:{period}"
        cached = self.cache.get(key)
        if cached is not None:
            data, _ = cached
            return [self._bar_from_dict(b) for b in data]
        try:
            df = yf.Ticker(sym).history(period=period, interval="1d", auto_adjust=False)
            if df.empty:
                return self._stale_history(key)
            bars = self._df_to_bars(df)
            self.cache.set(
                key, [self._bar_to_dict(b) for b in bars],
                self.cfg.yfinance_history_ttl_seconds,
            )
            return bars
        except Exception as e:
            log.warning("history fetch failed for %s: %s", sym, e)
            return self._stale_history(key)

    def history_intraday(self, symbol: str, days: int = 5) -> list[OHLCBar]:
        sym = symbol.upper()
        key = f"yf:hist_i:{sym}:{days}d"
        cached = self.cache.get(key)
        if cached is not None:
            data, _ = cached
            return [self._bar_from_dict(b) for b in data]
        try:
            df = yf.Ticker(sym).history(period=f"{days}d", interval="5m", auto_adjust=False)
            if df.empty:
                return self._stale_history(key)
            bars = self._df_to_bars(df)
            self.cache.set(
                key, [self._bar_to_dict(b) for b in bars],
                self.cfg.yfinance_history_ttl_seconds,
            )
            return bars
        except Exception as e:
            log.warning("intraday fetch failed for %s: %s", sym, e)
            return self._stale_history(key)

    def _stale_history(self, key: str) -> list[OHLCBar]:
        stale = self.cache.get(key, allow_stale=True)
        if stale is None:
            return []
        data, _ = stale
        return [self._bar_from_dict(b) for b in data]

    @staticmethod
    def _df_to_bars(df: pd.DataFrame) -> list[OHLCBar]:
        out: list[OHLCBar] = []
        for ts, row in df.iterrows():
            try:
                dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else datetime.fromisoformat(str(ts))
                if dt.tzinfo is not None:
                    dt = dt.replace(tzinfo=None)
            except Exception:
                continue
            out.append(OHLCBar(
                timestamp=dt,
                open=float(row["Open"]), high=float(row["High"]),
                low=float(row["Low"]), close=float(row["Close"]),
                volume=int(row["Volume"]),
            ))
        return out

    @staticmethod
    def _bar_to_dict(b: OHLCBar) -> dict[str, Any]:
        return {
            "timestamp": b.timestamp.isoformat(),
            "open": b.open, "high": b.high, "low": b.low, "close": b.close,
            "volume": b.volume,
        }

    @staticmethod
    def _bar_from_dict(d: dict[str, Any]) -> OHLCBar:
        return OHLCBar(
            timestamp=datetime.fromisoformat(d["timestamp"]),
            open=d["open"], high=d["high"], low=d["low"], close=d["close"],
            volume=int(d["volume"]),
        )

    # ── Options chain ──────────────────────────────────────────────────────

    def options_chain(self, symbol: str, max_expiries: int = 6) -> OptionsChain | None:
        sym = symbol.upper()
        key = f"yf:opts:{sym}:{max_expiries}"
        cached = self.cache.get(key)
        if cached is not None:
            data, _ = cached
            return self._chain_from_dict(data)
        try:
            t = yf.Ticker(sym)
            expiries_raw = t.options
            if not expiries_raw:
                return None
            spot = float(t.fast_info.get("last_price") or 0.0)
            if spot == 0.0:
                hist = t.history(period="1d")
                if not hist.empty:
                    spot = float(hist.iloc[-1]["Close"])

            picked = expiries_raw[:max_expiries]
            contracts: list[dict] = []
            for exp_str in picked:
                try:
                    chain = t.option_chain(exp_str)
                except Exception as e:
                    log.debug("option_chain(%s) failed: %s", exp_str, e)
                    continue
                for kind, df in (("call", chain.calls), ("put", chain.puts)):
                    for _, row in df.iterrows():
                        contracts.append(self._contract_to_dict(row, sym, exp_str, kind))

            data = {
                "symbol": sym,
                "fetched_at": utc_now().isoformat(),
                "spot_price": spot,
                "expiries": picked,
                "contracts": contracts,
            }
            self.cache.set(key, data, self.cfg.yfinance_options_ttl_seconds)
            return self._chain_from_dict(data)
        except Exception as e:
            log.warning("options chain fetch failed for %s: %s", sym, e)
            stale = self.cache.get(key, allow_stale=True)
            if stale is None:
                return None
            return self._chain_from_dict(stale[0])

    @staticmethod
    def _contract_to_dict(row: pd.Series, sym: str, exp: str, kind: str) -> dict:
        def f(x, default=0.0):
            try:
                v = float(x)
                if pd.isna(v):
                    return default
                return v
            except Exception:
                return default
        def i(x, default=0):
            try:
                v = int(x)
                if pd.isna(v):
                    return default
                return v
            except Exception:
                return default
        return {
            "symbol": sym, "expiry": exp,
            "strike": f(row.get("strike")), "type": kind,
            "bid": f(row.get("bid")), "ask": f(row.get("ask")),
            "last": f(row.get("lastPrice")),
            "volume": i(row.get("volume")),
            "open_interest": i(row.get("openInterest")),
            "implied_volatility": f(row.get("impliedVolatility")),
            "delta": None, "gamma": None, "theta": None, "vega": None,
        }

    @staticmethod
    def _chain_from_dict(d: dict) -> OptionsChain:
        contracts = [
            OptionContract(
                symbol=c["symbol"],
                expiry=date.fromisoformat(c["expiry"]),
                strike=c["strike"], type=OptionType(c["type"]),
                bid=c["bid"], ask=c["ask"], last=c["last"],
                volume=c["volume"], open_interest=c["open_interest"],
                implied_volatility=c["implied_volatility"],
                delta=c.get("delta"), gamma=c.get("gamma"),
                theta=c.get("theta"), vega=c.get("vega"),
            )
            for c in d["contracts"]
        ]
        return OptionsChain(
            symbol=d["symbol"],
            fetched_at=datetime.fromisoformat(d["fetched_at"]),
            spot_price=d["spot_price"],
            expiries=[date.fromisoformat(e) for e in d["expiries"]],
            contracts=contracts,
        )

    # ── Fundamentals ───────────────────────────────────────────────────────

    def fundamentals(self, symbol: str) -> Fundamentals | None:
        sym = symbol.upper()
        key = f"yf:fund:{sym}"
        cached = self.cache.get(key)
        if cached is not None:
            data, _ = cached
            return self._fund_from_dict(data)
        try:
            t = yf.Ticker(sym)
            info = t.info or {}
            def g(k):
                v = info.get(k)
                if v is None or (isinstance(v, float) and pd.isna(v)):
                    return None
                return float(v)
            data = {
                "symbol": sym,
                "market_cap": g("marketCap"),
                "shares_outstanding": g("sharesOutstanding"),
                "cash": g("totalCash"),
                "total_debt": g("totalDebt"),
                "revenue_ttm": g("totalRevenue"),
                "net_income_ttm": g("netIncomeToCommon"),
                "free_cash_flow_ttm": g("freeCashflow"),
                "operating_cash_flow_ttm": g("operatingCashflow"),
                "pe_ratio": g("trailingPE"),
                "dividend_yield": g("dividendYield"),
                "beta": g("beta"),
                "fetched_at": utc_now().isoformat(),
            }
            self.cache.set(key, data, self.cfg.yfinance_fundamentals_ttl_seconds)
            return self._fund_from_dict(data)
        except Exception as e:
            log.warning("fundamentals fetch failed for %s: %s", sym, e)
            stale = self.cache.get(key, allow_stale=True)
            if stale is None:
                return None
            return self._fund_from_dict(stale[0])

    @staticmethod
    def _fund_from_dict(d: dict) -> Fundamentals:
        return Fundamentals(
            symbol=d["symbol"],
            market_cap=d.get("market_cap"),
            shares_outstanding=d.get("shares_outstanding"),
            cash=d.get("cash"), total_debt=d.get("total_debt"),
            revenue_ttm=d.get("revenue_ttm"),
            net_income_ttm=d.get("net_income_ttm"),
            free_cash_flow_ttm=d.get("free_cash_flow_ttm"),
            operating_cash_flow_ttm=d.get("operating_cash_flow_ttm"),
            pe_ratio=d.get("pe_ratio"), dividend_yield=d.get("dividend_yield"),
            beta=d.get("beta"),
            fetched_at=datetime.fromisoformat(d["fetched_at"]),
        )
