"""Time helpers — single point of truth for timestamps."""
from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Naive UTC datetime — keeps SQLite ISO strings consistent with existing data."""
    return datetime.now(UTC).replace(tzinfo=None)
