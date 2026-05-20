"""User-supplied report ingestion.

The user drops reports into ticker folders (e.g. `pure-quantum-computing-plays/
report-5-13-2026/5-13-2026.rtf` plus screenshots). We:
  1. Walk the Trading-Firm root looking for `report-*` subdirs and `.txt`/`.rtf`
     files inside ticker folders.
  2. Strip RTF control codes into plain text.
  3. Split per-ticker sections using ticker symbols as section headers
     (the user's reports follow the pattern: SYM-DATE.png followed by prose).
  4. Index each section under its ticker symbol so agents see it as `notes`.

This is a one-way ingest — the firm doesn't write back to user reports.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class UserReportSection:
    symbol: str
    report_date: date
    source_path: Path
    text: str
    images: tuple[Path, ...] = field(default_factory=tuple)

    @property
    def preview(self) -> str:
        return self.text[:300].strip()


def strip_rtf(raw: bytes) -> str:
    """Quick-and-dirty RTF → plain text. Doesn't render fonts/colors but keeps text."""
    text = raw.decode("latin-1", errors="ignore")
    # Replace \word123 control words with space
    text = re.sub(r"\\[a-zA-Z]+-?\d*\s?", " ", text)
    # Strip escaped backslashes and braces
    text = re.sub(r"\\[\\{}]", "", text)
    text = re.sub(r"[{}]", "", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_report_file(path: Path) -> str:
    """Read a report file and return its plain text."""
    if path.suffix.lower() == ".rtf":
        return strip_rtf(path.read_bytes())
    return path.read_text(encoding="utf-8", errors="ignore")


# Patterns we use to detect a per-ticker section:
#   1. "SYM-MM-DD-YYYY.png" — the screenshot anchor (user's pattern)
#   2. "SYM:" or "Ticker SYM" as section header
#   3. Standalone uppercase 1-5-char SYM at the start of a chunk
TICKER_ANCHOR_RE = re.compile(r"\b([A-Z]{1,5})-\d{1,2}-\d{1,2}-\d{4}\.png", re.IGNORECASE)
TICKER_INLINE_RE = re.compile(r"\b([A-Z]{2,5})[\s:,]")


def split_into_ticker_sections(
    text: str, known_symbols: set[str] | None = None,
) -> list[tuple[str, str]]:
    """Split a report's text into (symbol, section_text) tuples.

    Uses ticker-anchored image references as primary delimiters. If anchors
    aren't found, falls back to scanning for known ticker symbols.
    """
    anchors = list(TICKER_ANCHOR_RE.finditer(text))
    if not anchors:
        return _fallback_split(text, known_symbols or set())

    sections: list[tuple[str, str]] = []
    for i, m in enumerate(anchors):
        sym = m.group(1).upper()
        start = m.end()
        end = anchors[i + 1].start() if i + 1 < len(anchors) else len(text)
        chunk = text[start:end].strip()
        if chunk:
            sections.append((sym, chunk))
    return sections


def _fallback_split(text: str, known: set[str]) -> list[tuple[str, str]]:
    """If no image anchors found, search for known symbols and slice around them."""
    if not known:
        return []
    # Find positions of known symbols (word-boundary, uppercase only)
    hits: list[tuple[int, str]] = []
    for sym in known:
        for m in re.finditer(rf"\b{re.escape(sym)}\b", text):
            hits.append((m.start(), sym))
    hits.sort()
    if not hits:
        return []
    sections: list[tuple[str, str]] = []
    for i, (pos, sym) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        chunk = text[pos:end].strip()
        if chunk:
            sections.append((sym, chunk))
    return sections


# ─── Walking the directory tree ─────────────────────────────────────────────


REPORT_DIR_RE = re.compile(r"^report[-_](\d{1,2})[-_/](\d{1,2})[-_/](\d{4})$", re.IGNORECASE)


SKIP_DIR_NAMES = {".venv", "venv", ".git", "__pycache__", "node_modules", "data_store",
                  ".pytest_cache", ".ruff_cache", ".mypy_cache", "tests", "firm",
                  "reports"}


def _walk_skip(root: Path):
    """Walk `root` skipping noise dirs (.venv, .git, firm-generated reports/)."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            for entry in current.iterdir():
                if entry.is_dir():
                    if entry.name in SKIP_DIR_NAMES:
                        continue
                    stack.append(entry)
                elif entry.is_file():
                    yield entry
        except (PermissionError, FileNotFoundError):
            continue


def discover_user_reports(
    root: Path, known_symbols: set[str] | None = None,
) -> list[UserReportSection]:
    """Walk `root` finding all user reports and return per-ticker sections."""
    sections: list[UserReportSection] = []
    for path in _walk_skip(root):
        if path.suffix.lower() not in {".rtf", ".txt", ".md"}:
            continue
        report_date = _date_from_path(path)
        try:
            text = parse_report_file(path)
        except Exception as e:
            log.debug("could not parse report %s: %s", path, e)
            continue
        if not text or len(text) < 50:
            continue
        # Collect sibling images (PNGs in the same dir)
        images = tuple(sorted(path.parent.glob("*.png"))) if path.parent.is_dir() else ()
        # Split by ticker
        per_ticker = split_into_ticker_sections(text, known_symbols)
        if not per_ticker:
            # Whole-file fallback — file is named like a single-ticker note
            sym = _ticker_from_filename(path) or "?"
            if sym != "?":
                per_ticker = [(sym, text)]
        for sym, chunk in per_ticker:
            sym_images = tuple(p for p in images if sym.lower() in p.stem.lower())
            sections.append(UserReportSection(
                symbol=sym.upper(), report_date=report_date,
                source_path=path, text=chunk.strip(), images=sym_images,
            ))
    return sections


def _date_from_path(path: Path) -> date:
    """Best-effort: pull a date from the parent directory name `report-M-D-YYYY`."""
    for part in (path.parent.name, path.parent.parent.name if path.parent.parent else ""):
        m = REPORT_DIR_RE.match(part)
        if m:
            mo, day, yr = m.groups()
            try:
                return date(int(yr), int(mo), int(day))
            except ValueError:
                pass
    return datetime.fromtimestamp(path.stat().st_mtime).date()


def _ticker_from_filename(path: Path) -> str | None:
    """Pull a 1-5 char uppercase ticker from a filename like `INTC.txt` or `ACHR.txt`."""
    m = re.match(r"^([A-Z]{1,5})", path.stem.upper())
    if m:
        return m.group(1)
    return None


def index_by_symbol(sections: list[UserReportSection]) -> dict[str, list[UserReportSection]]:
    """Group sections by ticker, newest first."""
    out: dict[str, list[UserReportSection]] = {}
    for s in sections:
        out.setdefault(s.symbol, []).append(s)
    for sym in out:
        out[sym].sort(key=lambda x: x.report_date, reverse=True)
    return out
