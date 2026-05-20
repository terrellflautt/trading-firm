"""Time-of-day scheduler for the firm.

Replaces cadence-based ticks with cron-style wall-clock scans. The daemon
sleeps until the next scheduled time, runs the work, then sleeps to the
following slot. DST is handled automatically by `zoneinfo`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Schedule:
    timezone: ZoneInfo
    times: tuple[time, ...]            # local wall-clock times

    @classmethod
    def from_strings(cls, tz_name: str, hhmm_list: Iterable[str]) -> "Schedule":
        return cls(
            timezone=ZoneInfo(tz_name),
            times=tuple(sorted(time.fromisoformat(s) for s in hhmm_list)),
        )

    def next_fire_after(self, now: datetime) -> datetime:
        """First scheduled wall-clock time strictly after `now`."""
        local_now = _to_tz(now, self.timezone)
        # Skip weekends — markets are closed Sat/Sun.
        # For today's remaining times (if it's a weekday), find the next one.
        if local_now.weekday() < 5:
            for t in self.times:
                candidate = local_now.replace(hour=t.hour, minute=t.minute,
                                              second=0, microsecond=0)
                if candidate > local_now:
                    return candidate
        # Otherwise, advance to next weekday's first slot.
        day = local_now + timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        return day.replace(hour=self.times[0].hour, minute=self.times[0].minute,
                           second=0, microsecond=0)

    def most_recent_fire_before(self, now: datetime) -> datetime | None:
        """The most recent scheduled time that has already passed today (or this week).

        Used for catch-up: if the daemon starts after some of today's times,
        run the last one we missed once on startup.
        """
        local_now = _to_tz(now, self.timezone)
        # Look back through today and previous business days for the most
        # recent slot.
        for delta in range(0, 8):
            day = local_now - timedelta(days=delta)
            if day.weekday() >= 5:
                continue
            slots_today = [
                day.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
                for t in self.times
            ]
            # Filter to those at or before local_now
            slots_today = [s for s in slots_today if s <= local_now]
            if slots_today:
                return max(slots_today)
        return None


def _to_tz(dt: datetime, tz: ZoneInfo) -> datetime:
    if dt.tzinfo is None:
        # Treat naive datetimes as local time in `tz` — simpler than assuming UTC
        # since the daemon constructs them with `datetime.now(tz)`.
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)
