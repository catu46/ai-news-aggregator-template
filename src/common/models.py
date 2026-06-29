"""Shared data models."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


# --------------------------------------------------------------------------
# Normalized post emitted by any ingestion source (Reddit, X, ...)
# --------------------------------------------------------------------------
@dataclass
class IngestedPost:
    source_platform: Literal["reddit", "twitter", "seed", "github", "manual"]
    source_id: str           # platform-native id (dedup key)
    source_url: str
    raw_text: str
    author: str | None = None
    published_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Curator verdict — Structured Output schema (Haiku 4.5).
# IMPORTANT: no numeric (ge/le) or string (min/max length) constraints:
# Structured Outputs does not support them. Range validation is done in the app.
# --------------------------------------------------------------------------
PrimaryCategory = Literal[
    "ai_tools",            # useful AI tools/products/apps/agents/libs you'd adopt
    "ai_capabilities",     # model/feature releases that change what you can DO
    "applied_techniques",  # how to USE AI well (agent patterns, RAG, prompting, evals)
    "autonomous_agents",   # agent architectures/tools/postmortems (applied)
    "ai_industry",         # AI news/industry that matters to a practitioner
    "other",
]
RejectReason = Literal[
    "ai_slop",             # clickbait/hype/engagement-bait (the #1 enemy)
    "low_signal",          # generic/rambling/low-info — tech-portal grade
    "research_only",       # ML/infra research or tinkering with no applicability
    "corporate_hype",      # vendor/product marketing
    "basic_tutorial",      # 101 / getting-started / beginner listicle
    "off_topic",           # not about AI
    "none",
]


class Verdict(BaseModel):
    verdict: Literal["approve", "reject"]
    confidence: float                 # 0..1 (validate in the app, not in the schema)
    primary_category: PrimaryCategory
    reject_reason: RejectReason
    summary: str                      # short summary (1-2 sentences) for the card
    one_line_rationale: str


# --------------------------------------------------------------------------
# Feed "direction" in natural language -> Structured Output schema.
# The owner says something like "for the next 2 days I want news about AI
# finance and repos about skills"; the Steerer returns a FocusPlan.
# --------------------------------------------------------------------------
FocusBucket = Literal["repos", "news"]


class FocusItem(BaseModel):
    bucket: FocusBucket               # 'repos' (GitHub) or 'news' (X+Reddit)
    topic: str                        # short topic, GOOD FOR SEARCH (may be in English)
    days: int                         # validity in days (0 = use app default)
    quota: int                        # # of bucket cards for this focus (0 = unstated -> app asks/uses default)


ChatKind = Literal["steer", "recall", "balance", "status", "capacity", "other"]
RecallPolarity = Literal["liked", "disliked", "any"]
BalanceBucket = Literal["repos", "news", "both"]
StatusAbout = Literal["focus", "balance", "both"]


class ChatIntent(BaseModel):
    """Intent of a chat message: steer / recall / rebalance / query state / resize / other."""

    kind: ChatKind                    # steer | recall | balance | status | capacity | other
    directives: list[FocusItem]       # when kind="steer"
    recall_query: str                 # when kind="recall" (topic; "" otherwise)
    recall_polarity: RecallPolarity   # liked | disliked | any
    balance_bucket: BalanceBucket     # when kind="balance"
    balance_fresh: float              # 0..1: desired fraction of NEW content
    balance_reset: bool               # kind="balance": reset the mix to DEFAULT
    status_about: StatusAbout         # kind="status": focus | balance | both
    capacity_bucket: BalanceBucket    # kind="capacity": which bucket to resize
    capacity_count: int               # kind="capacity": new per-day card cap of the bucket
    reply: str                        # short confirmation/guidance in PT-BR
