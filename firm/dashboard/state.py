"""Shared in-memory state between the daemon's scan loop and HTTP handlers.

The daemon's `_tick()` writes the latest `ScanResult` into `AppState`. HTTP
handlers read it under an asyncio lock. Both the daemon and the dashboard run
in the same process, so this is just a singleton + lock — no IPC needed.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..agents.scout import ScoutCandidate
from ..config import Config
from ..orchestrator import Orchestrator, ScanResult


@dataclass
class AppState:
    """Singleton-ish state container shared by daemon and HTTP handlers."""

    cfg: Config
    orchestrator: Orchestrator
    latest_scan: ScanResult | None = None
    latest_scout: list[ScoutCandidate] = field(default_factory=list)
    last_scan_at: datetime | None = None
    last_scout_at: datetime | None = None
    is_scanning: bool = False
    last_error: str | None = None

    # asyncio.Queue acts as a fanout for SSE clients
    _event_queues: list[asyncio.Queue[dict[str, Any]]] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def set_latest_scan(self, scan: ScanResult) -> None:
        async with self._lock:
            self.latest_scan = scan
            self.last_scan_at = datetime.now()
        await self._broadcast({"event": "scan_complete", "at": datetime.now().isoformat()})

    async def set_latest_scout(self, finds: list[ScoutCandidate]) -> None:
        async with self._lock:
            self.latest_scout = finds
            self.last_scout_at = datetime.now()
        await self._broadcast({"event": "scout_complete", "at": datetime.now().isoformat()})

    async def set_scanning(self, scanning: bool) -> None:
        async with self._lock:
            self.is_scanning = scanning
        await self._broadcast({"event": "scan_state", "scanning": scanning})

    async def set_error(self, err: str | None) -> None:
        async with self._lock:
            self.last_error = err
        if err:
            await self._broadcast({"event": "error", "message": err})

    # ─── SSE fanout ────────────────────────────────────────────────────────

    def new_subscriber(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=50)
        self._event_queues.append(q)
        return q

    def drop_subscriber(self, q: asyncio.Queue) -> None:
        try:
            self._event_queues.remove(q)
        except ValueError:
            pass

    async def _broadcast(self, event: dict[str, Any]) -> None:
        # Best-effort: never block on a slow client; just drop full queues.
        for q in list(self._event_queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


# Module-level singleton, set during app startup
_APP_STATE: AppState | None = None


def set_app_state(state: AppState) -> None:
    global _APP_STATE
    _APP_STATE = state


def get_app_state() -> AppState:
    if _APP_STATE is None:
        raise RuntimeError("AppState not initialized — call set_app_state() first")
    return _APP_STATE
