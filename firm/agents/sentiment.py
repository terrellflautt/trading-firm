"""Sentiment Analyst — reads news headlines and emits a directional signal."""
from __future__ import annotations

import logging
from pathlib import Path

from ..data.cache import Cache
from ..data.news import NewsAggregator, NewsItem, format_headlines_for_prompt
from ..portfolio.types import Signal, TickerContext
from .base import BaseAgent, load_skill

log = logging.getLogger(__name__)


SKILL_PATH = Path(__file__).resolve().parent / "skills" / "sentiment.md"


class SentimentAnalyst(BaseAgent):
    name = "sentiment_analyst"

    def __init__(self, *args, news: NewsAggregator | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._news = news
        self._role = load_skill(SKILL_PATH)

    def _get_news(self) -> NewsAggregator:
        if self._news is None:
            cache = Cache(self.cfg.root / self.cfg.firm.data.cache_db)
            self._news = NewsAggregator(cache)
        return self._news

    def role_prompt(self) -> str:
        return self._role

    def build_user_prompt(self, ctx: TickerContext) -> str:
        news = self._get_news()
        items: list[NewsItem] = news.fetch(ctx.symbol, limit=10)
        ctx.indicators.setdefault("news", []).extend([it.to_dict() for it in items])
        sector_line = f"Sector: {ctx.sector}"
        ticker_notes = f"Notes: {ctx.notes}" if ctx.notes else ""
        return (
            f"Ticker: {ctx.symbol}\n"
            f"{sector_line}\n"
            f"{ticker_notes}\n"
            f"Recent headlines (newest first):\n"
            f"{format_headlines_for_prompt(items, max_items=10)}\n"
        )

    async def analyze(self, ctx: TickerContext) -> Signal | None:
        prompt = self.build_user_prompt(ctx)
        raw = await self._call_llm(self.role_prompt(), prompt)
        sig = self._parse_signal(raw, ctx.symbol)
        if sig is not None:
            log.debug("%s %s -> %s (%.2f)", self.name, ctx.symbol, sig.direction.value, sig.conviction)
        return sig
