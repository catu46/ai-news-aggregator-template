"""Curation layer: classifies posts into a Verdict via a swappable curator.

The default curator is `AnthropicCurator` (Haiku 4.5 + Structured Outputs +
prompt caching), but the `Curator` interface is swappable — a `DeepSeekCurator`
could slot in behind it without touching the rest of the pipeline (see curator.py).
"""
from .curator import (
    AnthropicCurator,
    BudgetExceeded,
    Curator,
    SpendGuard,
    estimate_cost_usd,
)
from .prompt import RUBRIC, build_user_message

__all__ = [
    "Curator",
    "AnthropicCurator",
    "BudgetExceeded",
    "SpendGuard",
    "estimate_cost_usd",
    "RUBRIC",
    "build_user_message",
]
