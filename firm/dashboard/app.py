"""FastAPI app + lifespan that runs the daemon scan loop in the background."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import date as _date, datetime, time as _time
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import Config
from ..orchestrator import Orchestrator
from ..scheduler import Schedule
from .state import AppState, set_app_state

log = logging.getLogger(__name__)


async def _run_scan_once(state: AppState, label: str) -> None:
    log.info("[%s] scan starting (watchlist=%d tickers)", label, len(state.cfg.watchlist.watchlist))
    await state.set_scanning(True)
    t0 = asyncio.get_running_loop().time()
    try:
        result = await state.orchestrator.scan_watchlist()
        await state.set_latest_scan(result)
        log.info(
            "[%s] scan complete in %.1fs: %d signals, %d proposals, %d recs (%d filtered)",
            label, asyncio.get_running_loop().time() - t0,
            sum(len(s) for s in result.signals_by_symbol.values()),
            len(result.proposals), len(result.recommendations), len(result.rejected),
        )
    except Exception as e:
        log.exception("[%s] scan failed: %s", label, e)
        await state.set_error(str(e))
    finally:
        await state.set_scanning(False)


async def _run_scout_once(state: AppState, label: str) -> None:
    log.info("[%s] scout starting", label)
    t0 = asyncio.get_running_loop().time()
    try:
        finds = await state.orchestrator.run_scout(top_n=5)
        await state.set_latest_scout(finds)
        log.info("[%s] scout complete in %.1fs: %d finds",
                 label, asyncio.get_running_loop().time() - t0, len(finds))
    except Exception as e:
        log.exception("[%s] scout failed: %s", label, e)


async def _run_eod_report(state: AppState) -> None:
    log.info("[eod] writing daily report")
    try:
        from ..reports.daily import write_daily_report
        scan = state.latest_scan
        if scan is None:
            scan = await state.orchestrator.scan_watchlist()
            await state.set_latest_scan(scan)
        finds = state.latest_scout or await state.orchestrator.run_scout(top_n=5)
        scan.scout_finds = finds
        path = write_daily_report(state.cfg, state.orchestrator.ledger, scan, scout_finds=finds)
        log.info("[eod] wrote %s", path)
    except Exception as e:
        log.exception("[eod] failed: %s", e)


async def _scheduled_loop(state: AppState) -> None:
    """Wait for the next scheduled fire-time, run the work, repeat.

    Three independent schedules: scan (5x daily), scout (1x daily pre-open),
    and an EOD report at close. Each runs as a separate sub-task so they
    don't interleave-block each other.
    """
    cfg = state.cfg.firm.scan
    tz = ZoneInfo(cfg.schedule_timezone)
    scan_sched = Schedule.from_strings(cfg.schedule_timezone, cfg.schedule)
    scout_sched = Schedule.from_strings(cfg.schedule_timezone, cfg.scout_schedule) if cfg.scout_schedule else None
    eod_time = _time.fromisoformat(cfg.daily_report_at) if cfg.daily_report_at else None

    # Catch-up on startup
    if cfg.catch_up_on_start:
        now = datetime.now(tz)
        prev = scan_sched.most_recent_fire_before(now)
        if prev is not None and (now - prev).total_seconds() < 8 * 3600:
            log.info("startup catch-up: most recent slot was %s — running now", prev)
            asyncio.create_task(_run_scan_once(state, f"catchup-{prev.strftime('%H:%M')}"))
        elif state.latest_scan is None:
            # No prior scan today and no recent slot to catch up — kick a one-shot
            # scan anyway so the dashboard isn't empty on first launch.
            log.info("startup: no prior scan — running initial scan")
            asyncio.create_task(_run_scan_once(state, "startup"))

    eod_done_today: _date | None = None
    while True:
        try:
            now = datetime.now(tz)
            next_scan = scan_sched.next_fire_after(now)
            next_scout = scout_sched.next_fire_after(now) if scout_sched else None
            next_eod = None
            if eod_time:
                today = now.replace(hour=eod_time.hour, minute=eod_time.minute,
                                    second=0, microsecond=0)
                next_eod = today if today > now and eod_done_today != today.date() else None
                if next_eod is None:
                    # Tomorrow's EOD (skipping weekends)
                    from datetime import timedelta
                    cand = today + timedelta(days=1)
                    while cand.weekday() >= 5:
                        cand += timedelta(days=1)
                    next_eod = cand

            # Pick the soonest event across all three streams
            candidates = [(next_scan, "scan")]
            if next_scout:
                candidates.append((next_scout, "scout"))
            if next_eod:
                candidates.append((next_eod, "eod"))
            candidates.sort(key=lambda x: x[0])
            target, kind = candidates[0]

            wait = (target - now).total_seconds()
            log.info("scheduler: next %s at %s (in %.0fs)", kind, target.strftime("%Y-%m-%d %H:%M %Z"), wait)
            try:
                await asyncio.sleep(max(1.0, min(wait, 3600.0)))
            except asyncio.CancelledError:
                raise
            # Loop back to re-check (in case the clock moved or DST shifted),
            # but if the target is within 30s, fire now.
            now = datetime.now(tz)
            if (target - now).total_seconds() <= 30:
                slot_label = target.strftime("%H:%M")
                if kind == "scan":
                    asyncio.create_task(_run_scan_once(state, slot_label))
                elif kind == "scout":
                    asyncio.create_task(_run_scout_once(state, f"scout-{slot_label}"))
                elif kind == "eod":
                    eod_done_today = target.date()
                    asyncio.create_task(_run_eod_report(state))
                # Brief sleep so we don't re-fire the same slot in this loop iteration
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            log.info("scheduler cancelled — exiting cleanly")
            break
        except Exception as e:
            log.exception("scheduler hiccup: %s", e)
            await asyncio.sleep(30)


def create_app(cfg: Config) -> FastAPI:
    orch = Orchestrator(cfg)
    state = AppState(cfg=cfg, orchestrator=orch)
    set_app_state(state)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # NO scheduled API-calling scan loop. The firm is now a data + UI tool;
        # all LLM reasoning happens via Claude Code → MCP server (free under Pro/Max).
        log.info("dashboard started — read-only mode (no API daemon)")
        yield
        log.info("dashboard shutting down")

    app = FastAPI(title="Trading Firm", lifespan=lifespan)

    # Templates
    here = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=str(here / "templates"))
    app.state.templates = templates

    # Static files
    static_dir = here / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Register routes
    from . import routes
    routes.register(app)

    @app.get("/healthz", response_class=HTMLResponse)
    async def healthz() -> str:
        return "ok"

    return app
