"""Start/stop lifecycle for the firm daemon.

`firm start` launches a long-running process that:
  - Performs a catch-up scan on startup if a scheduled slot was missed today.
  - Fires scans at each scheduled wall-clock time in `firm.yaml` `scan.schedule`.
  - Runs the Scout at each `scout_schedule` time.
  - Writes the daily report at `daily_report_at` (market close).
  - Handles SIGTERM/SIGINT gracefully — flushes the cache, persists state.

State that survives restart:
  - Everything in the SQLite ledger (positions, options, premiums, wheel states,
    trade log, scout finds) — these were already persistent.
  - PID file lets `firm stop` find the running daemon.
  - Last-scan timestamp file lets us know if we crashed mid-scan.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from contextlib import suppress
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config
from .orchestrator import Orchestrator
from .reports.daily import write_daily_report
from .utils.time import utc_now

log = logging.getLogger(__name__)


def _pid_path(cfg: Config) -> Path:
    return cfg.root / "data_store" / "firm.pid"


def _state_path(cfg: Config) -> Path:
    return cfg.root / "data_store" / "firm.state.json"


def is_running(cfg: Config) -> int | None:
    """Return the PID of a running daemon, or None."""
    pidfile = _pid_path(cfg)
    if not pidfile.exists():
        return None
    try:
        pid = int(pidfile.read_text().strip())
    except (ValueError, OSError):
        return None
    # On Unix, signal 0 checks if the process exists.
    try:
        os.kill(pid, 0)
        return pid
    except (ProcessLookupError, PermissionError):
        # Stale pidfile
        with suppress(OSError):
            pidfile.unlink()
        return None
    except OSError:
        return None


def write_pidfile(cfg: Config) -> None:
    pidfile = _pid_path(cfg)
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(os.getpid()))


def remove_pidfile(cfg: Config) -> None:
    with suppress(FileNotFoundError):
        _pid_path(cfg).unlink()


def write_state(cfg: Config, state: dict) -> None:
    state["updated_at"] = utc_now().isoformat()
    _state_path(cfg).write_text(json.dumps(state, indent=2, default=str))


def read_state(cfg: Config) -> dict:
    path = _state_path(cfg)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


# ─── Market hours awareness ─────────────────────────────────────────────────


NY_TZ = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)


def is_market_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now(NY_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=NY_TZ)
    elif now.tzinfo != NY_TZ:
        now = now.astimezone(NY_TZ)
    if now.weekday() >= 5:   # Weekend
        return False
    return MARKET_OPEN <= now.time() <= MARKET_CLOSE


def next_market_open(now: datetime | None = None) -> datetime:
    now = now or datetime.now(NY_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=NY_TZ)
    candidate = now.replace(hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


# ─── Main daemon loop ───────────────────────────────────────────────────────


class FirmDaemon:
    """Headless variant — same scheduled loop as the dashboard, no HTTP."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.orch = Orchestrator(cfg)
        self._stop = asyncio.Event()

    async def run(self) -> None:
        write_pidfile(self.cfg)
        log.info("firm daemon started (pid=%d, headless)", os.getpid())
        self._install_signal_handlers()
        write_state(self.cfg, {"status": "running", "started_at": utc_now().isoformat()})
        try:
            # Reuse the dashboard's scheduled loop with a minimal AppState shim
            from .dashboard.state import AppState
            from .dashboard.app import _scheduled_loop
            state = AppState(cfg=self.cfg, orchestrator=self.orch)
            scheduler_task = asyncio.create_task(_scheduled_loop(state))
            await self._stop.wait()
            scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await scheduler_task
        except Exception as e:
            log.exception("daemon crashed: %s", e)
        finally:
            await self._shutdown()

    async def _shutdown(self) -> None:
        log.info("daemon shutting down — writing final state")
        write_state(self.cfg, {"status": "stopped", "stopped_at": utc_now().isoformat()})
        remove_pidfile(self.cfg)
        log.info("daemon stopped cleanly")

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stop.set)


def stop_daemon(cfg: Config, timeout: float = 30.0) -> bool:
    """Send SIGTERM to the running daemon and wait for it to exit.

    Returns True on clean exit, False if forced or no daemon found.
    """
    pid = is_running(cfg)
    if pid is None:
        return False
    log.info("stopping firm daemon pid=%d", pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        log.error("could not signal pid %d: %s", pid, e)
        return False
    # Wait for it to clean up
    import time as _time
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            # Process gone — also ensure pidfile is gone (atexit may race)
            remove_pidfile(cfg)
            return True
        _time.sleep(0.5)
    # Hard kill
    log.warning("daemon didn't exit within %ds — sending SIGKILL", timeout)
    with suppress(OSError):
        os.kill(pid, signal.SIGKILL)
    remove_pidfile(cfg)
    return False
