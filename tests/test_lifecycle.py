"""Lifecycle tests — market hours math, state file round-trip."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import yaml

from firm.config import AccountsConfig, Config, FirmConfig, WatchlistConfig
from firm.lifecycle import NY_TZ, is_market_hours, next_market_open, read_state, write_state


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    config_dir = Path(__file__).resolve().parent.parent / "config"
    c = Config(
        firm=FirmConfig(**yaml.safe_load((config_dir / "firm.yaml").read_text())),
        accounts=AccountsConfig(**yaml.safe_load((config_dir / "accounts.yaml").read_text())),
        watchlist=WatchlistConfig(**yaml.safe_load((config_dir / "watchlist.yaml").read_text())),
        root=tmp_path,
    )
    (tmp_path / "data_store").mkdir()
    return c


def test_is_market_hours_weekday_open():
    # Wednesday 10am ET
    dt = datetime(2026, 5, 13, 10, 0, tzinfo=NY_TZ)
    assert is_market_hours(dt) is True


def test_is_market_hours_weekend():
    dt = datetime(2026, 5, 16, 10, 0, tzinfo=NY_TZ)  # Saturday
    assert is_market_hours(dt) is False


def test_is_market_hours_before_open():
    dt = datetime(2026, 5, 13, 8, 0, tzinfo=NY_TZ)
    assert is_market_hours(dt) is False


def test_is_market_hours_after_close():
    dt = datetime(2026, 5, 13, 16, 30, tzinfo=NY_TZ)
    assert is_market_hours(dt) is False


def test_next_market_open_after_close():
    fri_eve = datetime(2026, 5, 15, 17, 0, tzinfo=NY_TZ)
    nxt = next_market_open(fri_eve)
    assert nxt.weekday() == 0  # Monday
    assert nxt.hour == 9 and nxt.minute == 30


def test_state_round_trip(cfg: Config):
    write_state(cfg, {"status": "running", "ticks": 5})
    state = read_state(cfg)
    assert state["status"] == "running"
    assert state["ticks"] == 5
    assert "updated_at" in state


def test_read_state_missing_returns_empty(cfg: Config):
    state = read_state(cfg)
    assert state == {}
