"""User-content ingestion tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from firm.data.user_reports import (
    UserReportSection,
    discover_user_reports,
    index_by_symbol,
    split_into_ticker_sections,
    strip_rtf,
)


def test_strip_rtf_basic():
    raw = b"{\\rtf1\\ansi Hello \\b world}"
    text = strip_rtf(raw)
    assert "Hello" in text
    assert "world" in text


def test_split_by_image_anchors():
    text = (
        "intro IONQ-5-13-2026.png IONQ commentary text. "
        "QBTS-5-13-2026.png QBTS commentary text."
    )
    sections = split_into_ticker_sections(text)
    syms = [s for s, _ in sections]
    assert syms == ["IONQ", "QBTS"]
    assert "commentary" in sections[0][1]


def test_split_by_known_symbols_fallback():
    text = "Some intro. NVDA is the leader. INTC is recovering."
    sections = split_into_ticker_sections(text, known_symbols={"NVDA", "INTC"})
    syms = [s for s, _ in sections]
    assert "NVDA" in syms
    assert "INTC" in syms


def test_index_by_symbol_groups_and_sorts(tmp_path: Path):
    from datetime import date
    secs = [
        UserReportSection("IONQ", date(2026, 1, 1), tmp_path / "a.txt", "old"),
        UserReportSection("IONQ", date(2026, 5, 1), tmp_path / "b.txt", "new"),
        UserReportSection("QBTS", date(2026, 4, 1), tmp_path / "c.txt", "qbts"),
    ]
    idx = index_by_symbol(secs)
    assert idx["IONQ"][0].text == "new"
    assert idx["IONQ"][1].text == "old"
    assert "QBTS" in idx


def test_discover_user_reports_picks_up_real_user_file():
    """Ingest any report file in the working tree (smoke test).

    Skips when the user hasn't dropped any RTF/text reports under the
    project root yet — fresh-start installs won't have these.
    """
    import pytest as _pytest
    root = Path(__file__).resolve().parent.parent
    sections = discover_user_reports(
        root,
        known_symbols={"IONQ", "QBTS", "RGTI", "INTC", "ACHR", "SIDU",
                       "QNC", "QUBT", "AMD", "NVDA", "AAPL", "MSFT",
                       "SPY", "QQQ", "F"},
    )
    if not sections:
        _pytest.skip("No user-supplied report files in this workspace yet.")
    # At least one recognised symbol came through
    assert any(s.symbol for s in sections)
