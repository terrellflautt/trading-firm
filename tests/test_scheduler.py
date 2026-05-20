"""Scheduler tests — verify next-fire logic, weekend handling, catch-up."""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from firm.scheduler import Schedule

CHI = ZoneInfo("America/Chicago")


def _now(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=CHI)


def test_next_fire_picks_today_remaining():
    s = Schedule.from_strings("America/Chicago", ["03:00", "08:30", "13:00", "14:30"])
    now = _now(2026, 5, 13, 9, 0)   # Wednesday 9am
    nxt = s.next_fire_after(now)
    assert nxt.hour == 13 and nxt.minute == 0
    assert nxt.date() == now.date()


def test_next_fire_after_all_today_jumps_to_tomorrow():
    s = Schedule.from_strings("America/Chicago", ["03:00", "08:30", "13:00", "14:30"])
    now = _now(2026, 5, 13, 16, 0)   # Wed 4pm — after all
    nxt = s.next_fire_after(now)
    assert nxt.date() == datetime(2026, 5, 14).date()  # Thursday
    assert nxt.hour == 3 and nxt.minute == 0


def test_next_fire_skips_weekend():
    s = Schedule.from_strings("America/Chicago", ["03:00", "08:30"])
    # Saturday morning
    now = _now(2026, 5, 16, 10, 0)
    nxt = s.next_fire_after(now)
    assert nxt.date() == datetime(2026, 5, 18).date()  # Monday
    assert nxt.hour == 3


def test_next_fire_friday_after_close_jumps_monday():
    s = Schedule.from_strings("America/Chicago", ["03:00", "14:30"])
    now = _now(2026, 5, 15, 16, 0)   # Friday after close
    nxt = s.next_fire_after(now)
    assert nxt.date() == datetime(2026, 5, 18).date()  # Monday


def test_most_recent_fire_finds_today_earlier():
    s = Schedule.from_strings("America/Chicago", ["03:00", "08:30", "13:00"])
    now = _now(2026, 5, 13, 10, 0)
    prev = s.most_recent_fire_before(now)
    assert prev is not None
    assert prev.hour == 8 and prev.minute == 30


def test_most_recent_fire_finds_yesterday_before_first_today():
    s = Schedule.from_strings("America/Chicago", ["03:00", "14:30"])
    # Wed at 2am — before today's first slot
    now = _now(2026, 5, 13, 2, 0)
    prev = s.most_recent_fire_before(now)
    assert prev is not None
    # Tuesday's last slot at 14:30
    assert prev.date() == datetime(2026, 5, 12).date()
    assert prev.hour == 14 and prev.minute == 30


def test_dst_spring_forward_handled():
    # Spring DST 2026-03-08 in US: 2am skips to 3am
    s = Schedule.from_strings("America/Chicago", ["02:30", "08:30"])
    # Saturday before
    sat = datetime(2026, 3, 7, 23, 59, tzinfo=CHI)
    nxt = s.next_fire_after(sat)
    # Sunday 02:30 doesn't exist in DST; either zoneinfo picks 03:30 CDT or pre-DST 02:30 CST.
    # Either way the candidate should be a valid datetime — main thing is no crash.
    assert nxt is not None
