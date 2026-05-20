"""SQLite-backed persistence for positions, options, premiums, wheel states, and trade log.

A single SQLite file holds the firm's entire portfolio state. The schema is
versioned via PRAGMA user_version so we can migrate cleanly in later phases.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

from ..utils.time import utc_now
from .types import (
    OptionPosition,
    OptionSide,
    OptionType,
    SharesPosition,
    WheelState,
)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS shares_positions (
    account_key TEXT NOT NULL,
    symbol TEXT NOT NULL,
    shares INTEGER NOT NULL,
    total_cost REAL NOT NULL,
    premiums_collected REAL NOT NULL DEFAULT 0,
    opened_at TEXT NOT NULL,
    last_updated TEXT NOT NULL,
    PRIMARY KEY (account_key, symbol)
);

CREATE TABLE IF NOT EXISTS option_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_key TEXT NOT NULL,
    symbol TEXT NOT NULL,
    expiry TEXT NOT NULL,
    strike REAL NOT NULL,
    type TEXT NOT NULL,           -- put | call
    side TEXT NOT NULL,           -- short | long
    contracts INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    close_price REAL,
    realized_pnl REAL
);

CREATE INDEX IF NOT EXISTS idx_option_positions_open
    ON option_positions(account_key, symbol) WHERE closed_at IS NULL;

CREATE TABLE IF NOT EXISTS wheel_states (
    account_key TEXT NOT NULL,
    symbol TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_key, symbol)
);

CREATE TABLE IF NOT EXISTS premium_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_key TEXT NOT NULL,
    symbol TEXT NOT NULL,
    amount REAL NOT NULL,
    source TEXT NOT NULL,       -- 'csp_open' | 'cc_open' | 'roll_credit' | etc.
    option_position_id INTEGER,
    occurred_at TEXT NOT NULL,
    FOREIGN KEY (option_position_id) REFERENCES option_positions(id)
);

CREATE TABLE IF NOT EXISTS trade_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_key TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    contracts_or_shares INTEGER NOT NULL,
    limit_price REAL NOT NULL,
    expiry TEXT,
    strike REAL,
    option_type TEXT,
    rationale TEXT NOT NULL,
    conviction REAL NOT NULL,
    expected_credit_or_debit REAL NOT NULL,
    risk_notes_json TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    executed INTEGER NOT NULL DEFAULT 0,  -- 0 = pending/skipped, 1 = user-marked filled
    executed_at TEXT,
    fill_price REAL
);

CREATE TABLE IF NOT EXISTS scout_finds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    sector TEXT NOT NULL,
    rationale TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    surfaced_at TEXT NOT NULL,
    outcome TEXT                   -- 'added_to_watchlist' | 'dismissed' | NULL
);
"""


class Ledger:
    """Thin SQLite wrapper. All times stored as ISO-format UTC strings."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            if current == 0:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif current != SCHEMA_VERSION:
                raise RuntimeError(
                    f"Ledger schema version mismatch: db has {current}, code expects {SCHEMA_VERSION}. "
                    "Migration needed."
                )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
        finally:
            conn.close()

    # ─── Shares ────────────────────────────────────────────────────────────

    def upsert_shares(self, pos: SharesPosition) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO shares_positions
                   (account_key, symbol, shares, total_cost, premiums_collected,
                    opened_at, last_updated)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(account_key, symbol) DO UPDATE SET
                       shares = excluded.shares,
                       total_cost = excluded.total_cost,
                       premiums_collected = excluded.premiums_collected,
                       last_updated = excluded.last_updated""",
                (
                    pos.account_key, pos.symbol, pos.shares, pos.total_cost,
                    pos.premiums_collected,
                    pos.opened_at.isoformat(), pos.last_updated.isoformat(),
                ),
            )

    def get_shares(self, account_key: str, symbol: str) -> SharesPosition | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM shares_positions WHERE account_key = ? AND symbol = ?",
                (account_key, symbol.upper()),
            ).fetchone()
        if row is None:
            return None
        return SharesPosition(
            account_key=row["account_key"], symbol=row["symbol"],
            shares=row["shares"], total_cost=row["total_cost"],
            premiums_collected=row["premiums_collected"],
            opened_at=datetime.fromisoformat(row["opened_at"]),
            last_updated=datetime.fromisoformat(row["last_updated"]),
        )

    def all_shares_for_account(self, account_key: str) -> list[SharesPosition]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM shares_positions WHERE account_key = ? AND shares > 0",
                (account_key,),
            ).fetchall()
        return [
            SharesPosition(
                account_key=r["account_key"], symbol=r["symbol"],
                shares=r["shares"], total_cost=r["total_cost"],
                premiums_collected=r["premiums_collected"],
                opened_at=datetime.fromisoformat(r["opened_at"]),
                last_updated=datetime.fromisoformat(r["last_updated"]),
            )
            for r in rows
        ]

    def delete_shares(self, account_key: str, symbol: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM shares_positions WHERE account_key = ? AND symbol = ?",
                (account_key, symbol.upper()),
            )

    # ─── Options ───────────────────────────────────────────────────────────

    def insert_option(self, opt: OptionPosition) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO option_positions
                   (account_key, symbol, expiry, strike, type, side, contracts,
                    entry_price, opened_at, closed_at, close_price, realized_pnl)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    opt.account_key, opt.symbol, opt.expiry.isoformat(),
                    opt.strike, opt.type.value, opt.side.value, opt.contracts,
                    opt.entry_price, opt.opened_at.isoformat(),
                    opt.closed_at.isoformat() if opt.closed_at else None,
                    opt.close_price, opt.realized_pnl,
                ),
            )
            return cur.lastrowid

    def close_option(self, option_id: int, close_price: float, realized_pnl: float) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE option_positions
                   SET closed_at = ?, close_price = ?, realized_pnl = ?
                   WHERE id = ?""",
                (utc_now().isoformat(), close_price, realized_pnl, option_id),
            )

    def open_options_for(self, account_key: str, symbol: str) -> list[OptionPosition]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM option_positions
                   WHERE account_key = ? AND symbol = ? AND closed_at IS NULL""",
                (account_key, symbol.upper()),
            ).fetchall()
        return [self._row_to_option(r) for r in rows]

    def all_open_options_for_account(self, account_key: str) -> list[OptionPosition]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM option_positions WHERE account_key = ? AND closed_at IS NULL",
                (account_key,),
            ).fetchall()
        return [self._row_to_option(r) for r in rows]

    @staticmethod
    def _row_to_option(r: sqlite3.Row) -> OptionPosition:
        return OptionPosition(
            account_key=r["account_key"], symbol=r["symbol"],
            expiry=date.fromisoformat(r["expiry"]), strike=r["strike"],
            type=OptionType(r["type"]), side=OptionSide(r["side"]),
            contracts=r["contracts"], entry_price=r["entry_price"],
            opened_at=datetime.fromisoformat(r["opened_at"]),
            closed_at=datetime.fromisoformat(r["closed_at"]) if r["closed_at"] else None,
            close_price=r["close_price"], realized_pnl=r["realized_pnl"],
        )

    # ─── Wheel state ───────────────────────────────────────────────────────

    def set_wheel_state(self, account_key: str, symbol: str, state: WheelState) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO wheel_states (account_key, symbol, state, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(account_key, symbol) DO UPDATE SET
                       state = excluded.state,
                       updated_at = excluded.updated_at""",
                (account_key, symbol.upper(), state.value, utc_now().isoformat()),
            )

    def get_wheel_state(self, account_key: str, symbol: str) -> WheelState:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT state FROM wheel_states WHERE account_key = ? AND symbol = ?",
                (account_key, symbol.upper()),
            ).fetchone()
        if row is None:
            return WheelState.CASH
        return WheelState(row["state"])

    def all_wheel_states(self, account_key: str) -> dict[str, WheelState]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT symbol, state FROM wheel_states WHERE account_key = ?",
                (account_key,),
            ).fetchall()
        return {r["symbol"]: WheelState(r["state"]) for r in rows}

    # ─── Premium ledger ────────────────────────────────────────────────────

    def add_premium(
        self,
        account_key: str,
        symbol: str,
        amount: float,
        source: str,
        option_position_id: int | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO premium_ledger
                   (account_key, symbol, amount, source, option_position_id, occurred_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    account_key, symbol.upper(), amount, source, option_position_id,
                    utc_now().isoformat(),
                ),
            )

    def premiums_collected(self, account_key: str, symbol: str) -> float:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT COALESCE(SUM(amount), 0) AS total FROM premium_ledger
                   WHERE account_key = ? AND symbol = ?""",
                (account_key, symbol.upper()),
            ).fetchone()
        return float(row["total"])

    # ─── Trade log ─────────────────────────────────────────────────────────

    def log_recommendation(self, rec: dict) -> int:
        """Persist a Recommendation. Accepts dict to avoid the agents layer importing here."""
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO trade_log
                   (account_key, symbol, action, contracts_or_shares, limit_price,
                    expiry, strike, option_type, rationale, conviction,
                    expected_credit_or_debit, risk_notes_json, issued_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    rec["account_key"], rec["symbol"], rec["action"],
                    rec["contracts_or_shares"], rec["limit_price"],
                    rec["expiry"], rec["strike"], rec["option_type"],
                    rec["rationale"], rec["conviction"],
                    rec["expected_credit_or_debit"],
                    json.dumps(rec.get("risk_notes", [])),
                    rec.get("issued_at", utc_now().isoformat()),
                ),
            )
            return cur.lastrowid

    def mark_executed(self, trade_id: int, fill_price: float) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE trade_log SET executed = 1, executed_at = ?, fill_price = ?
                   WHERE id = ?""",
                (datetime.utcnow().isoformat(), fill_price, trade_id),
            )
