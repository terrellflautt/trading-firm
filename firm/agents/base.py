"""Base class for LLM-backed analyst agents.

All agents share:
  - An async Anthropic client (the firm runs many ticker scans concurrently).
  - Prompt caching on the system prompt + the agent's skill description.
  - Strict JSON output parsed into Signal dataclasses.
  - Backoff/retry on transient errors.
  - A shared concurrency semaphore so we don't blow the per-minute token budget.

Each subclass overrides:
  - `name` — agent identity in log lines and Signal.agent
  - `role_prompt()` — the cacheable role description (the "skill")
  - `analyze(ctx)` — produce a Signal given a TickerContext
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import anthropic
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import Config
from ..portfolio.types import Signal, SignalDirection, TickerContext

log = logging.getLogger(__name__)


# Reasonable retryable errors from the Anthropic SDK
RETRYABLE = (
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
)


# Global concurrency limiter — protects the org-wide 30k input-tokens/minute
# rate limit on Sonnet by capping how many agent calls run at once.
_LLM_SEMAPHORE: asyncio.Semaphore | None = None
_DEFAULT_CONCURRENCY = int(os.environ.get("FIRM_LLM_CONCURRENCY", "2"))
_INTER_CALL_DELAY_SEC = float(os.environ.get("FIRM_LLM_DELAY_SEC", "0.5"))


def _get_semaphore() -> asyncio.Semaphore:
    global _LLM_SEMAPHORE
    if _LLM_SEMAPHORE is None:
        _LLM_SEMAPHORE = asyncio.Semaphore(_DEFAULT_CONCURRENCY)
    return _LLM_SEMAPHORE


class BaseAgent(ABC):
    """LLM-backed agent that emits a Signal for a ticker."""

    name: str = "base"
    use_portfolio_model: bool = False   # True for synthesis agents

    def __init__(self, cfg: Config, client: anthropic.AsyncAnthropic | None = None):
        self.cfg = cfg
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if client is None and not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set in environment")
        self.client = client or anthropic.AsyncAnthropic(api_key=api_key)

    @property
    def model(self) -> str:
        if self.use_portfolio_model:
            return self.cfg.firm.llm.portfolio_model
        return self.cfg.firm.llm.analyst_model

    @abstractmethod
    def role_prompt(self) -> str:
        """The agent's cacheable role description / skill text."""

    @abstractmethod
    def build_user_prompt(self, ctx: TickerContext) -> str:
        """The per-scan user prompt — the only non-cacheable part."""

    @abstractmethod
    async def analyze(self, ctx: TickerContext) -> Signal | None:
        """Run the agent and produce a Signal (or None if data insufficient)."""

    # ─── Helpers ───────────────────────────────────────────────────────────

    @retry(
        retry=retry_if_exception_type(RETRYABLE),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        """Single LLM round trip. System prompt is cached if enabled."""
        system_blocks: list[Any]
        if self.cfg.firm.llm.enable_prompt_caching:
            system_blocks = [{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }]
        else:
            system_blocks = [{"type": "text", "text": system_prompt}]

        kwargs = dict(
            model=self.model,
            max_tokens=self.cfg.firm.llm.max_tokens,
            system=system_blocks,
            messages=[{"role": "user", "content": user_prompt}],
        )
        # Opus 4.7 deprecates the `temperature` parameter — only send it for models that accept it.
        if not _model_rejects_temperature(self.model):
            kwargs["temperature"] = self.cfg.firm.llm.temperature
        async with _get_semaphore():
            resp = await self.client.messages.create(**kwargs)
            # Small pause so we drip-feed under the per-minute token rate limit.
            if _INTER_CALL_DELAY_SEC > 0:
                await asyncio.sleep(_INTER_CALL_DELAY_SEC)
        # Concatenate text blocks in the response
        parts: list[str] = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        return "".join(parts)

    def _parse_signal(self, raw: str, symbol: str) -> Signal | None:
        """Parse a JSON signal from a model response.

        We accept either a bare JSON object or a JSON block in markdown fences.
        """
        text = raw.strip()
        # Try to extract a fenced JSON block first
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        candidate = fenced.group(1) if fenced else text

        # If still surrounded by prose, try to grab the first {...} block
        if not candidate.lstrip().startswith("{"):
            m = re.search(r"\{.*\}", candidate, re.DOTALL)
            if not m:
                log.warning("%s: could not find JSON in response: %s", self.name, raw[:200])
                return None
            candidate = m.group(0)

        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as e:
            log.warning("%s: bad JSON (%s) from response: %s", self.name, e, raw[:200])
            return None

        try:
            direction = SignalDirection(data["direction"])
        except (KeyError, ValueError):
            log.warning("%s: bad/missing 'direction' in %s", self.name, data)
            return None
        conviction = float(data.get("conviction", 0.5))
        conviction = max(0.0, min(1.0, conviction))
        rationale = str(data.get("rationale", ""))[:1000]
        extra = data.get("data", {})

        return Signal(
            agent=self.name, symbol=symbol,
            direction=direction, conviction=conviction,
            rationale=rationale, data=extra if isinstance(extra, dict) else {},
        )


def load_skill(path: Path) -> str:
    """Read a skill.md file (the agent's strategy prompt) from disk."""
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _model_rejects_temperature(model: str) -> bool:
    """Opus 4.7+ deprecate temperature; gate so we don't 400 the API."""
    return "opus-4-7" in model or "opus-4-8" in model
