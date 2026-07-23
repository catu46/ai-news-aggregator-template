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

Swapping providers is trivial: see `KimiCurator` and `DeepSeekCurator`
(alternative impls via OpenAI-style APIs, selectable via CURATOR_PROVIDER).
Just implement `classify()` with the same signature and return a `Verdict`.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import anthropic
import asyncpg

from ..common.config import Settings
from ..common.models import Verdict
from .prompt import RUBRIC, build_user_message

if TYPE_CHECKING:  # avoids a circular import at runtime; only for the type hint
    from ..common.db import Database

logger = logging.getLogger("curator")

# Haiku 4.5 pricing (USD per 1M tokens), verified Jun/2026.
_PRICE_INPUT_PER_TOKEN = 1.0 / 1_000_000   # $1 / 1M input (fresh, uncached)
_PRICE_OUTPUT_PER_TOKEN = 5.0 / 1_000_000  # $5 / 1M output
# Prompt caching: a cache READ costs 0.1x of input; a cache WRITE (5-min TTL)
# costs 1.25x. Estimating with these REAL factors keeps the spend cap aligned
# with the actual bill — charging cache_read at full input price overestimates
# ~5x (the big rubric is cached), which makes the SpendGuard PAUSE curation way
# too early and pile up an uncurated backlog.
_PRICE_CACHE_READ_PER_TOKEN = 0.10 / 1_000_000   # $0.10 / 1M (0.1x input)
_PRICE_CACHE_WRITE_PER_TOKEN = 1.25 / 1_000_000  # $1.25 / 1M (1.25x input)

# Default path of the persisted spend file (keyed by YYYY-MM).
_DEFAULT_SPEND_PATH = Path(
    os.path.expanduser("~/.cache/ai-news-aggregator/spend.json")
)

# "Table doesn't exist" error raised when the db/migrations/2026-07-23-meta-kv.sql
# migration hasn't been applied yet — caught so we fall back to the local file
# instead of taking curation down with it. Same pattern as
# `recall.py`'s missing-hybrid-column handling.
_MISSING_META_TABLE_ERRORS = (asyncpg.exceptions.UndefinedTableError,)


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
# Spend guard: accumulates approximate USD, keyed by month.
#
# Source of truth: the database's `meta` table (key 'curator_spend'), when a
# `Database` is available — it's the only persistent source of truth on
# Railway. Fallback: a local file (~/.cache/.../spend.json), used when
# `db=None` OR the `meta` table doesn't exist yet (the
# db/migrations/2026-07-23-meta-kv.sql migration hasn't been applied).
#
# WHY THE FILE ALONE ISN'T ENOUGH: Railway's disk is EPHEMERAL — every deploy
# brings up a fresh container, without the previous one's filesystem. The
# local file used to reset on every deploy, so the `CURATOR_MONTHLY_BUDGET_USD`
# cap never actually fired (the spend "seen" by the process never accumulated
# across deploys). It still exists as a fallback (works fine locally, and
# keeps curation from breaking if the DB is down or the migration hasn't run
# yet) — but it stops being the reliable source of truth in production as
# soon as the database is available.
#
# Thread-safe on the file path (a simple lock) — the I/O is trivial and rare
# enough. The DB path uses Postgres's own transaction (see
# `Database.add_curator_spend`) for atomicity.
# --------------------------------------------------------------------------
class SpendGuard:
    """Persists the estimated monthly spend. The database (meta table) is the
    source of truth when available; the local file (~/.cache/.../spend.json)
    is the fallback — see the comment above the class."""

    def __init__(self, path: Path | None = None, db: "Database | None" = None) -> None:
        self._path = path or _DEFAULT_SPEND_PATH
        self._lock = threading.Lock()
        self._db = db
        self._warned_meta_missing = False  # pending-migration warning only once

    @staticmethod
    def _month_key(now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        return now.strftime("%Y-%m")

    def _warn_meta_missing_once(self) -> None:
        if self._warned_meta_missing:
            return
        logger.warning(
            "SpendGuard: the meta table doesn't exist yet (migration "
            "db/migrations/2026-07-23-meta-kv.sql pending) — falling back to "
            "the local file (%s). This warning only appears once.", self._path,
        )
        self._warned_meta_missing = True

    # ---- file fallback (sync; only used when db=None or the DB fails) -------
    def _read_file(self) -> dict[str, float]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return {}
        # Sanitize: only str->float pairs.
        return {k: float(v) for k, v in data.items() if isinstance(v, (int, float))}

    def _write_file(self, data: dict[str, float]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: write to tmp and rename.
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)

    # ---- public API (async: the DB path needs it) ---------------------------
    async def spent_this_month(self) -> float:
        """Accumulated spend (USD) for the current month."""
        if self._db is not None:
            try:
                data = await self._db.meta_get("curator_spend")
                return float((data or {}).get(self._month_key(), 0.0))
            except _MISSING_META_TABLE_ERRORS:
                self._warn_meta_missing_once()
            except Exception:  # noqa: BLE001 — a flaky DB can't block the read
                logger.exception("SpendGuard: failed to read spend from the DB; using the local file")
        with self._lock:
            return self._read_file().get(self._month_key(), 0.0)

    async def add(self, usd: float) -> float:
        """Adds `usd` to the current month; returns the new month total."""
        if usd <= 0:
            return await self.spent_this_month()
        if self._db is not None:
            try:
                return await self._db.add_curator_spend(self._month_key(), usd)
            except _MISSING_META_TABLE_ERRORS:
                self._warn_meta_missing_once()
            except Exception:  # noqa: BLE001 — a flaky DB can't block accounting
                logger.exception("SpendGuard: failed to write spend to the DB; using the local file")
        with self._lock:
            data = self._read_file()
            key = self._month_key()
            data[key] = data.get(key, 0.0) + usd
            self._write_file(data)
            return data[key]

    async def is_over_budget(self, budget_usd: float) -> bool:
        """True if the month's spend has already reached/exceeded the budget."""
        return await self.spent_this_month() >= budget_usd


def estimate_cost_usd(usage: object) -> float:
    """Approximate USD of a response, from `resp.usage`.

    Charges each component at its REAL price: fresh input at 1x, cache_read at
    0.1x, cache_write at 1.25x, output at 5x. Since the big rubric is cached,
    most of the input comes back as cache_read (10x cheaper) — estimating that
    correctly is what keeps the spend cap from pausing curation too early.
    `usage` is the SDK object (attributes may be missing) — read defensively.
    """
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)

    return (
        input_tokens * _PRICE_INPUT_PER_TOKEN
        + cache_read * _PRICE_CACHE_READ_PER_TOKEN
        + cache_write * _PRICE_CACHE_WRITE_PER_TOKEN
        + output_tokens * _PRICE_OUTPUT_PER_TOKEN
    )


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
    async def spent_this_month(self) -> float:
        return await self._spend.spent_this_month()

    async def is_over_budget(self, budget_usd: float | None = None) -> bool:
        """True if the month's spend reached the budget (default = the one in Settings)."""
        return await self._spend.is_over_budget(
            self._budget_usd if budget_usd is None else budget_usd
        )

    async def classify(
        self, post_text: str, similarity_signal: str | None = None,
        interests: list[str] | None = None,
    ) -> Verdict | None:
        # Budget gate BEFORE spending: cheap and prevents blowing past it.
        if await self.is_over_budget():
            raise BudgetExceeded(
                SpendGuard._month_key(), await self.spent_this_month(), self._budget_usd
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
            await self._spend.add(estimate_cost_usd(resp.usage))
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
# Alternative providers behind the SAME interface.
# --------------------------------------------------------------------------
# Any OpenAI-style API (chat.completions + JSON mode) fits here: it reuses
# `RUBRIC`, `SpendGuard` and `BudgetExceeded`; the rest of the pipeline
# (runner, db.mark_curation) doesn't change — only the injected `Curator`.
# Implemented: `KimiCurator` (Moonshot) and `DeepSeekCurator` — selectable
# via CURATOR_PROVIDER=kimi|deepseek (see `make_curator` at the end).
# ==========================================================================


# Output contract for OpenAI-style providers (Kimi, DeepSeek): they don't have
# Anthropic's `messages.parse`, so we ask for JSON mode + describe the schema
# and validate with Pydantic in the app.
_KIMI_JSON_CONTRACT = """

=====================================================================
OUTPUT FORMAT (STRICT JSON)
=====================================================================
Return ONLY a single JSON object — no markdown, no code fences, no prose — with
EXACTLY these keys:
{
  "verdict": "approve" or "reject",
  "confidence": a number between 0 and 1,
  "primary_category": one of "ai_tools", "ai_capabilities",
      "applied_techniques", "autonomous_agents", "ai_industry", "other",
  "reject_reason": one of "ai_slop", "low_signal", "research_only",
      "corporate_hype", "basic_tutorial", "off_topic", "none",
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

    async def spent_this_month(self) -> float:
        return await self._spend.spent_this_month()

    async def is_over_budget(self, budget_usd: float | None = None) -> bool:
        return await self._spend.is_over_budget(
            self._budget_usd if budget_usd is None else budget_usd
        )

    async def classify(
        self, post_text: str, similarity_signal: str | None = None,
        interests: list[str] | None = None,
    ) -> Verdict | None:
        if await self.is_over_budget():
            raise BudgetExceeded(
                SpendGuard._month_key(), await self.spent_this_month(), self._budget_usd
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
            await self._spend.add(estimate_kimi_cost_usd(resp.usage))
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


# deepseek-v4-flash pricing (USD per 1M tokens), verified in the official docs
# in jul/2026. Prompt caching is AUTOMATIC on DeepSeek (no `cache_control`):
# the big rubric repeated on every call becomes a cache hit (~50x cheaper).
_DEEPSEEK_INPUT_HIT = 0.0028 / 1_000_000   # input on cache hit
_DEEPSEEK_INPUT_MISS = 0.14 / 1_000_000    # input on cache miss
_DEEPSEEK_OUTPUT = 0.28 / 1_000_000


def estimate_deepseek_cost_usd(usage: object) -> float:
    """Approximate USD of a DeepSeek response, from `usage`.

    DeepSeek exposes `prompt_cache_hit_tokens`/`prompt_cache_miss_tokens`
    directly on usage; if missing (SDK change), tries the OpenAI shape
    (`prompt_tokens_details.cached_tokens`) and, as a last resort, charges all
    input at the cache miss price (conservative — overestimates, never under).
    """
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    hit = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)
    if not hit:
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            hit = int(getattr(details, "cached_tokens", 0) or 0)
    miss = max(0, prompt - hit)
    return hit * _DEEPSEEK_INPUT_HIT + miss * _DEEPSEEK_INPUT_MISS + completion * _DEEPSEEK_OUTPUT


class DeepSeekCurator(Curator):
    """Alternative curator via DeepSeek (OpenAI-compatible API).

    Selectable via `CURATOR_PROVIDER=deepseek`. Same recipe as `KimiCurator`:
    RUBRIC + JSON contract in the system prompt, JSON mode, Pydantic
    validation (`Verdict`). DeepSeek-specific differences:
    - `thinking` is ON by default on deepseek-v4-flash — we disable it via
      `extra_body` (classification doesn't need long reasoning, and thinking
      tokens are billed as output);
    - automatic prompt caching (no `cache_control`).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        spend_guard: SpendGuard | None = None,
        client: object | None = None,
        max_tokens: int = 400,
    ) -> None:
        import openai  # lazy: only DeepSeek users need the `openai` SDK

        if not settings.deepseek_api_key:
            raise RuntimeError(
                "CURATOR_PROVIDER=deepseek, but DEEPSEEK_API_KEY is missing from .env."
            )
        self._model = settings.deepseek_model
        self.model = settings.deepseek_model  # public: the runner reads it via curator_model_of()
        self._budget_usd = settings.curator_monthly_budget_usd
        self._max_tokens = max_tokens
        self._spend = spend_guard or SpendGuard()
        self._client = client or openai.AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
        )
        # Static system prefix (RUBRIC + JSON contract) -> automatic cache.
        self._system = RUBRIC + _KIMI_JSON_CONTRACT

    async def spent_this_month(self) -> float:
        return await self._spend.spent_this_month()

    async def is_over_budget(self, budget_usd: float | None = None) -> bool:
        return await self._spend.is_over_budget(
            self._budget_usd if budget_usd is None else budget_usd
        )

    async def classify(
        self, post_text: str, similarity_signal: str | None = None,
        interests: list[str] | None = None,
    ) -> Verdict | None:
        if await self.is_over_budget():
            raise BudgetExceeded(
                SpendGuard._month_key(), await self.spent_this_month(), self._budget_usd
            )

        user_message = build_user_message(
            raw_text=post_text, author=None, metadata=None,
            similarity_signal=similarity_signal, interests=interests,
        )
        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                max_tokens=self._max_tokens,
                temperature=0.0,  # classification: determinism > creativity
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": self._system},
                    {"role": "user", "content": user_message},
                ],
                # DeepSeek-specific parameter (outside the OpenAI schema) —
                # without this, v4-flash "thinks" before answering (default
                # enabled), multiplying output cost and latency for nothing.
                extra_body={"thinking": {"type": "disabled"}},
            )
        except Exception:  # noqa: BLE001 — API/network error -> None (caller marks an error)
            logger.exception("deepseek: classification call failed")
            return None

        # ALWAYS account for the spend (tokens were already billed).
        try:
            await self._spend.add(estimate_deepseek_cost_usd(resp.usage))
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
            logger.warning("deepseek: response did not validate against Verdict")
            return None

        if not (0.0 <= verdict.confidence <= 1.0):
            verdict.confidence = max(0.0, min(1.0, verdict.confidence))
        return verdict


def make_curator(
    settings: Settings, *,
    spend_guard: SpendGuard | None = None,
    db: "Database | None" = None,
) -> Curator:
    """Curator factory: picks the provider via `settings.curator_provider`.

    `CURATOR_PROVIDER` defaults to `deepseek` (cheapest — see .env.example);
    `=kimi` or `=anthropic` switches provider without touching the rest of
    the pipeline (the `Curator` interface is the same).

    `db`: if given (and `spend_guard` isn't passed explicitly), the internal
    `SpendGuard` uses the database (`meta` table) as the source of truth for
    the monthly spend instead of just the local file — see `SpendGuard`
    above. Pass `None` (default) to keep the file-only behavior, e.g. in
    tests.
    """
    guard = spend_guard if spend_guard is not None else SpendGuard(db=db)
    provider = (settings.curator_provider or "anthropic").lower()
    if provider == "kimi":
        return KimiCurator(settings, spend_guard=guard)
    if provider == "deepseek":
        return DeepSeekCurator(settings, spend_guard=guard)
    return AnthropicCurator(settings, spend_guard=guard)
