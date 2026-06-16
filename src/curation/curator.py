"""SWAPPABLE curator: classifies a post -> Verdict (or None on failure).

Architecture
------------
- `Curator` (ABC): the stable interface. Any impl follows
  `async def classify(post_text, similarity_signal=None) -> Verdict | None`.
- `AnthropicCurator`: the default impl using Haiku 4.5 via Structured Outputs
  (`client.messages.parse(..., output_format=Verdict)`), with the large rubric
  in a CACHED system block (cache_control ephemeral).
- `BudgetExceeded`: raised by `classify()` when the estimated monthly spend
  exceeds `settings.curator_monthly_budget_usd`.

Swapping providers is trivial: see `DeepSeekCurator` (commented sketch at the
end). Just implement `classify()` with the same signature and return a `Verdict`.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from ..common.config import Settings
from ..common.models import Verdict
from .prompt import RUBRIC, build_user_message

logger = logging.getLogger("curator")

# Haiku 4.5 pricing (USD per 1M tokens), verified Jun/2026.
# Tokens read from cache cost ~0.1x of input; here we use a conservative
# estimate (we charge cache_read at the same price as input). Overestimating
# the spend is safe: worst case we stop a little before the real budget.
_PRICE_INPUT_PER_TOKEN = 1.0 / 1_000_000   # $1 / 1M input
_PRICE_OUTPUT_PER_TOKEN = 5.0 / 1_000_000  # $5 / 1M output

# Default path of the persisted spend file (keyed by YYYY-MM).
_DEFAULT_SPEND_PATH = Path(
    os.path.expanduser("~/.cache/ai-news-aggregator/spend.json")
)


class BudgetExceeded(RuntimeError):
    """The curator's estimated monthly spend exceeded the configured budget."""

    def __init__(self, month: str, spent_usd: float, budget_usd: float) -> None:
        self.month = month
        self.spent_usd = spent_usd
        self.budget_usd = budget_usd
        super().__init__(
            f"Curator budget exceeded in {month}: "
            f"${spent_usd:.4f} >= ${budget_usd:.2f}"
        )


# --------------------------------------------------------------------------
# Spend guard: accumulates approximate USD in a small JSON, keyed by month.
# Thread-safe (simple lock) — the I/O is trivial and rare enough.
# --------------------------------------------------------------------------
class SpendGuard:
    """Persists the estimated monthly spend in ~/.cache/.../spend.json."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _DEFAULT_SPEND_PATH
        self._lock = threading.Lock()

    @staticmethod
    def _month_key(now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        return now.strftime("%Y-%m")

    def _read(self) -> dict[str, float]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return {}
        # Sanitize: only str->float pairs.
        return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}

    def _write(self, data: dict[str, float]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: write to tmp and rename.
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)

    def spent_this_month(self) -> float:
        """Accumulated spend (USD) for the current month."""
        with self._lock:
            return self._read().get(self._month_key(), 0.0)

    def add(self, usd: float) -> float:
        """Adds `usd` to the current month; returns the new month total."""
        if usd <= 0:
            return self.spent_this_month()
        with self._lock:
            data = self._read()
            key = self._month_key()
            data[key] = data.get(key, 0.0) + usd
            self._write(data)
            return data[key]

    def is_over_budget(self, budget_usd: float) -> bool:
        """True if the month's spend has already reached/exceeded the budget."""
        return self.spent_this_month() >= budget_usd


def estimate_cost_usd(usage: object) -> float:
    """Approximate USD of a response, from `resp.usage`.

    We charge input + cache_read at the input price (conservative estimate)
    and output at the output price. `usage` is the SDK object (attributes may
    be missing depending on the version) — we read defensively.
    """
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)

    billable_input = input_tokens + cache_read + cache_write
    return billable_input * _PRICE_INPUT_PER_TOKEN + output_tokens * _PRICE_OUTPUT_PER_TOKEN


# --------------------------------------------------------------------------
# Stable interface.
# --------------------------------------------------------------------------
class Curator(ABC):
    """Swappable curator. Every impl returns a validated Verdict, or None."""

    @abstractmethod
    async def classify(
        self, post_text: str, similarity_signal: str | None = None,
        interests: list[str] | None = None,
    ) -> Verdict | None:
        """Classifies `post_text`. None on refusal/max_tokens/empty parse.

        May raise `BudgetExceeded` if the spend guard is over budget.
        The caller marks an error (mark_curation_error) when it receives None.
        """
        raise NotImplementedError


# --------------------------------------------------------------------------
# Default impl: Anthropic Haiku 4.5 + Structured Outputs + prompt caching.
# --------------------------------------------------------------------------
class AnthropicCurator(Curator):
    def __init__(
        self,
        settings: Settings,
        *,
        spend_guard: SpendGuard | None = None,
        client: anthropic.AsyncAnthropic | None = None,
        max_tokens: int = 400,  # headroom for the verdict + the summary
    ) -> None:
        self._settings = settings
        self._model = settings.curator_model
        self.model = settings.curator_model  # public: the runner reads it via curator_model_of()
        self._budget_usd = settings.curator_monthly_budget_usd
        self._max_tokens = max_tokens
        self._spend = spend_guard or SpendGuard()
        self._client = client or anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key
        )

        # STATIC and CACHED system block. The rubric is large on purpose
        # (>= 4096 tokens, Haiku's floor) so prompt caching kicks in: the
        # prefix is byte-for-byte identical on every call.
        self._system = [
            {
                "type": "text",
                "text": RUBRIC,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    # ---- budget guard (exposed for the runner to check before running) ------
    @property
    def spent_this_month(self) -> float:
        return self._spend.spent_this_month()

    def is_over_budget(self, budget_usd: float | None = None) -> bool:
        """True if the month's spend reached the budget (default = the one in Settings)."""
        return self._spend.is_over_budget(
            self._budget_usd if budget_usd is None else budget_usd
        )

    async def classify(
        self, post_text: str, similarity_signal: str | None = None,
        interests: list[str] | None = None,
    ) -> Verdict | None:
        # Budget gate BEFORE spending: cheap and prevents blowing past it.
        if self.is_over_budget():
            raise BudgetExceeded(
                SpendGuard._month_key(), self.spent_this_month, self._budget_usd
            )

        user_message = build_user_message(
            raw_text=post_text,
            author=None,
            metadata=None,
            similarity_signal=similarity_signal,
            interests=interests,
        )

        # Structured Outputs enforces the Verdict schema. NEVER pass `effort`
        # (Haiku doesn't support it). Small max_tokens: the verdict is tiny.
        resp = await self._client.messages.parse(
            model=self._model,
            max_tokens=self._max_tokens,
            system=self._system,
            messages=[{"role": "user", "content": user_message}],
            output_format=Verdict,
        )

        # ALWAYS account for the spend (even on failure — tokens were already billed).
        try:
            self._spend.add(estimate_cost_usd(resp.usage))
        except Exception:  # noqa: BLE001 — accounting never takes down curation
            pass

        # Failures treated as None: the caller marks an error and moves on.
        if resp.stop_reason in ("refusal", "max_tokens"):
            return None

        verdict = resp.parsed_output
        if verdict is None:
            return None

        # Hygiene: enforce the confidence range (the schema doesn't impose ge/le).
        if not (0.0 <= verdict.confidence <= 1.0):
            verdict.confidence = max(0.0, min(1.0, verdict.confidence))
        return verdict


# ==========================================================================
# NOTE — second provider behind the SAME interface (sketch).
# --------------------------------------------------------------------------
# DeepSeek could be a second impl of `Curator`. Its API is compatible with the
# OpenAI style; with `response_format`/function-calling for the JSON, you could
# map the output into the same `Verdict` and return it here.
# The rest of the pipeline (runner, db.mark_curation) doesn't change — only the
# injected `Curator` differs. Sketch:
#
#   class DeepSeekCurator(Curator):
#       def __init__(self, settings, *, spend_guard=None, ...):
#           self._client = openai.AsyncOpenAI(
#               api_key=..., base_url="https://api.deepseek.com",
#           )
#           self._spend = spend_guard or SpendGuard()
#           ...
#
#       async def classify(self, post_text, similarity_signal=None):
#           if self.is_over_budget():
#               raise BudgetExceeded(...)
#           resp = await self._client.chat.completions.create(
#               model="deepseek-chat",
#               messages=[
#                   {"role": "system", "content": RUBRIC},
#                   {"role": "user", "content": build_user_message(
#                       post_text, None, None, similarity_signal)},
#               ],
#               response_format={"type": "json_object"},
#               max_tokens=300,
#           )
#           # ...accumulate spend using DeepSeek's pricing table...
#           # ...parse the JSON and validate with Verdict.model_validate_json(...)...
#           # ...return Verdict or None on failure.
#
# The prices and the `usage` reading are provider-specific, but
# `SpendGuard`/`BudgetExceeded` are reused. This sketch is REALIZED below as
# `KimiCurator` (Moonshot/Kimi, selectable via CURATOR_PROVIDER).
# ==========================================================================


# Output contract for Kimi: it doesn't have Anthropic's `messages.parse`, so we
# ask for JSON mode + describe the schema and validate with Pydantic in the app.
_KIMI_JSON_CONTRACT = """

=====================================================================
OUTPUT FORMAT (STRICT JSON)
=====================================================================
Return ONLY a single JSON object — no markdown, no code fences, no prose — with
EXACTLY these keys:
{
  "verdict": "approve" or "reject",
  "confidence": a number between 0 and 1,
  "primary_category": one of "data_engineering", "automation",
      "autonomous_agents", "advanced_frameworks", "modern_architecture", "other",
  "reject_reason": one of "basic_tutorial", "corporate_hype", "clickbait",
      "off_topic", "none",
  "summary": a 1-2 sentence summary in English,
  "one_line_rationale": one terse sentence
}
"""

# Kimi k2.6 pricing (USD per 1M tokens), reported by the provider (Moonshot).
_KIMI_INPUT_HIT = 0.16 / 1_000_000   # input on cache hit
_KIMI_INPUT_MISS = 0.95 / 1_000_000  # input on cache miss
_KIMI_OUTPUT = 4.00 / 1_000_000


def estimate_kimi_cost_usd(usage: object) -> float:
    """Approximate USD of a Kimi response, from the OpenAI-style `usage`.

    Uses `prompt_tokens_details.cached_tokens` when available (charges cache hit
    cheaper); otherwise, charges all input at the cache miss price (conservative).
    """
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    cached = 0
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = int(getattr(details, "cached_tokens", 0) or 0)
    miss = max(0, prompt - cached)
    return cached * _KIMI_INPUT_HIT + miss * _KIMI_INPUT_MISS + completion * _KIMI_OUTPUT


class KimiCurator(Curator):
    """Alternative curator via Moonshot/Kimi (OpenAI-compatible API).

    Selectable via `CURATOR_PROVIDER=kimi`. Reuses the `RUBRIC`, the
    `SpendGuard` and the `BudgetExceeded` — the rest of the pipeline doesn't
    change. Since Kimi has no `messages.parse`, we use JSON mode + Pydantic
    validation (`Verdict`).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        spend_guard: SpendGuard | None = None,
        client: object | None = None,
        max_tokens: int = 400,
    ) -> None:
        import openai  # lazy: only Kimi users need the `openai` SDK

        if not settings.moonshot_api_key:
            raise RuntimeError(
                "CURATOR_PROVIDER=kimi, but MOONSHOT_API_KEY is missing from .env."
            )
        self._model = settings.kimi_model
        self.model = settings.kimi_model  # public: the runner reads it via curator_model_of()
        self._budget_usd = settings.curator_monthly_budget_usd
        self._max_tokens = max_tokens
        self._spend = spend_guard or SpendGuard()
        self._client = client or openai.AsyncOpenAI(
            api_key=settings.moonshot_api_key,
            base_url=settings.moonshot_base_url,
        )
        # Static system prefix (RUBRIC + JSON contract) -> Kimi cache.
        self._system = RUBRIC + _KIMI_JSON_CONTRACT

    @property
    def spent_this_month(self) -> float:
        return self._spend.spent_this_month()

    def is_over_budget(self, budget_usd: float | None = None) -> bool:
        return self._spend.is_over_budget(
            self._budget_usd if budget_usd is None else budget_usd
        )

    async def classify(
        self, post_text: str, similarity_signal: str | None = None,
        interests: list[str] | None = None,
    ) -> Verdict | None:
        if self.is_over_budget():
            raise BudgetExceeded(
                SpendGuard._month_key(), self.spent_this_month, self._budget_usd
            )

        user_message = build_user_message(
            raw_text=post_text, author=None, metadata=None,
            similarity_signal=similarity_signal, interests=interests,
        )
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=self._max_tokens,
                temperature=1.0,  # kimi-k2.6 requires temperature=1 (rejects 0)
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": self._system},
                    {"role": "user", "content": user_message},
                ],
            )
        except Exception:  # noqa: BLE001 — API/network error -> None (caller marks an error)
            logger.exception("kimi: classification call failed")
            return None

        # ALWAYS account for the spend (tokens were already billed).
        try:
            self._spend.add(estimate_kimi_cost_usd(resp.usage))
        except Exception:  # noqa: BLE001 — accounting never takes down curation
            pass

        try:
            choice = resp.choices[0]
        except (AttributeError, IndexError):
            return None
        if getattr(choice, "finish_reason", None) == "length":
            return None
        content = (getattr(choice.message, "content", None) or "").strip()
        if not content:
            return None

        try:
            verdict = Verdict.model_validate_json(content)
        except Exception:  # noqa: BLE001 — invalid JSON / off-schema
            logger.warning("kimi: response did not validate against Verdict")
            return None

        if not (0.0 <= verdict.confidence <= 1.0):
            verdict.confidence = max(0.0, min(1.0, verdict.confidence))
        return verdict


def make_curator(settings: Settings, *, spend_guard: SpendGuard | None = None) -> Curator:
    """Curator factory: picks the provider via `settings.curator_provider`.

    Default = Anthropic (Haiku). `CURATOR_PROVIDER=kimi` switches to Kimi without
    touching the rest of the pipeline (the `Curator` interface is the same).
    """
    if (settings.curator_provider or "anthropic").lower() == "kimi":
        return KimiCurator(settings, spend_guard=spend_guard)
    return AnthropicCurator(settings, spend_guard=spend_guard)
