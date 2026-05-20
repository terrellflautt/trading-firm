"""Scout tests — sector guessing and scoring logic only (no network)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
import yaml

from firm.agents.scout import Scout, ScoutCandidate
from firm.config import (
    AccountsConfig,
    Config,
    FirmConfig,
    WatchlistConfig,
)


@pytest.fixture
def cfg() -> Config:
    config_dir = Path(__file__).resolve().parent.parent / "config"
    return Config(
        firm=FirmConfig(**yaml.safe_load((config_dir / "firm.yaml").read_text())),
        accounts=AccountsConfig(**yaml.safe_load((config_dir / "accounts.yaml").read_text())),
        watchlist=WatchlistConfig(**yaml.safe_load((config_dir / "watchlist.yaml").read_text())),
        root=config_dir.parent,
    )


def test_guess_sector_quantum():
    assert Scout._guess_sector("Quantum Computing", "Technology") == "quantum"


def test_guess_sector_semis():
    assert Scout._guess_sector("Semiconductor Equipment", "Technology") == "semis_gpu"
    assert Scout._guess_sector("Computer Hardware", "Technology") == "semis_gpu"


def test_guess_sector_evtol():
    assert Scout._guess_sector("Aerospace & Defense", "Industrials") == "evtol"


def test_guess_sector_unknown_defaults_to_industry():
    s = Scout._guess_sector("Pharmaceutical Mfg", "Healthcare")
    assert s == "pharmaceutical mfg"


def test_unusual_options_trigger():
    c = ScoutCandidate(
        symbol="X", sector_guess="quantum", source="sector_screen",
        price=20.0, avg_volume_30d=1_000_000, today_volume=5_000_000,
        iv_rank=65.0, iv_30d_annualized=0.8,
        fits_account=["cash"], options_oi_total_front=12_000,
        indicators_summary="", score=0.5, rationale="",
    )
    assert Scout._is_unusual_options(c) is True


def test_unusual_options_no_trigger_when_low_iv():
    c = ScoutCandidate(
        symbol="Y", sector_guess="quantum", source="sector_screen",
        price=20.0, avg_volume_30d=1_000_000, today_volume=2_000_000,
        iv_rank=25.0, iv_30d_annualized=0.5,
        fits_account=["cash"], options_oi_total_front=15_000,
        indicators_summary="", score=0.5, rationale="",
    )
    assert Scout._is_unusual_options(c) is False
