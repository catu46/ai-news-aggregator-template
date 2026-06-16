"""Classifies a chat message from the feed's owner into an intent (ChatIntent).

Four possible intents:
  - "steer"   -> steer the feed for a while ("for 2 days I want repos about
                 skills and news about AI finance").
  - "recall"  -> recall something HE ALREADY received and voted on ("what was
                 that RAG news I liked?", "there was a repo about agents I
                 disliked, which one was it?").
  - "balance" -> change the feed's fresh×relevant mix ("send me more new stuff",
                 "focus on what's relevant to me").
  - "other"   -> anything else.

Same model family as the curator (Haiku 4.5) via Structured Outputs. It's cheap
and only runs when the user sends a text message WITHOUT a link.
"""
from __future__ import annotations

import logging

import anthropic

from ..common.config import Settings
from ..common.models import ChatIntent

logger = logging.getLogger("steering")

# Default validity when the user doesn't say for how long (~2 weeks).
DEFAULT_FOCUS_DAYS = 14
# Safety ceiling: nobody pins a focus for more than ~2 months by accident.
MAX_FOCUS_DAYS = 60

_SYSTEM = """You interpret messages from the owner of a personal AI feed and
classify their intent into ONE of four.

The feed delivers TWO buckets per day:
- "repos": trending GitHub repositories.
- "news": "what people are talking about" — X/Twitter + Reddit (news, discussions).
On each card they tap 👍 or 👎.

1) kind = "steer" — they want to STEER the feed for a while. E.g.: "for the next
   two days I want news about the AI finance world and repos about skills". Fill
   `directives` with one item per (bucket, topic):
     - bucket: "repos" or "news"
     - topic: a few words, GOOD FOR SEARCH (in ENGLISH for technical topics —
       e.g. "AI funding venture capital", "agent skills")
     - days: the stated timeframe converted to days; if unstated, 0 (the app applies the default).

2) kind = "recall" — they want to FIND content from the archive about a topic
   (whether something they already voted on, or news/items about the subject in
   general). E.g.: "what was that RAG news I liked?", "any news about AI venture
   capital?", "bring me new stuff about agents". Fill:
     - recall_query: the topic to search for (in ENGLISH for technical topics)
     - recall_polarity: "liked" ONLY if they explicitly say they LIKED/tapped
       👍; "disliked" ONLY if they say they did NOT like it/tapped 👎; "any" for
       any general question about the topic (the COMMON case — search the whole
       archive, not just the votes). "new news about X" = recall with polarity "any".

3) kind = "balance" — they want to change the overall RATIO between freshness and
   relevance in the feed. Only classify as balance when there is an explicit
   fresh-vs-relevant COMPARISON (or a mention of "too old" / "mix" /
   "balance"): "I want MORE fresh than relevant", "stuff coming in is TOO OLD",
   "half fresh half relevant", "focus on what's RELEVANT to me".
   NOT balance (it's recall "any" or other): asking for CONTENT without
   comparing — "new news", "bring me what's new", "I want what's new about X". A
   plain "New news!" (no comparison, no topic) is NOT balance. Fill:
     - balance_bucket: "repos", "news" or "both" (if unspecified, "both")
     - balance_fresh: fraction 0..1 of how much should be FRESH. Map the wording:
       "only fresh"≈0.9, "more fresh"≈0.6, "half-and-half"=0.5,
       "more relevant"≈0.25, "only the relevant"≈0.1.
     - balance_reset: true if they want to GO BACK TO DEFAULT / UNDO / RESET the
       mix ("revert", "reset the mix", "leave it at the default", "undo this",
       "cancel that adjustment"). In that case do NOT invent a fraction — the app
       clears the adjustment and returns to the default. Otherwise, false. (No
       named bucket -> "both".)

4) kind = "other" — small talk, a question about the bot, a thank-you, etc.

`reply`: ALWAYS in English, short. On "steer" confirm what you understood; on
"recall" say you'll look it up; on "balance" confirm the new mix; on "other"
give one line of guidance. NEVER claim to have EXECUTED an action you don't
control — on "other" you ONLY guide/clarify: don't say "done", "reverted",
"reset", "undone", "cancelled" (the app does the executing, not you). If it's a
request to undo/reset the mix, it's "balance" (with balance_reset=true), not
"other".

Unused fields stay with: directives=[], recall_query="",
recall_polarity="any", balance_bucket="both", balance_fresh=0.4,
balance_reset=false."""


class Steerer:
    """Chat intent parser (steer / recall / balance / other) via Haiku."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: anthropic.AsyncAnthropic | None = None,
        max_tokens: int = 500,
    ) -> None:
        self._model = settings.curator_model
        self._max_tokens = max_tokens
        self._client = client or anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key
        )

    async def parse(self, message: str) -> ChatIntent | None:
        """Returns a ChatIntent, or None on failure/refusal/empty message."""
        message = (message or "").strip()
        if not message:
            return None
        try:
            resp = await self._client.messages.parse(
                model=self._model,
                max_tokens=self._max_tokens,
                system=_SYSTEM,
                messages=[{"role": "user", "content": message}],
                output_format=ChatIntent,
            )
        except Exception:  # noqa: BLE001 — never takes down the message handler
            logger.exception("steering: failed to interpret message")
            return None

        if resp.stop_reason in ("refusal", "max_tokens"):
            return None
        intent = resp.parsed_output
        if intent is None:
            return None

        # `days` hygiene: 0/negative -> default; apply the safety ceiling.
        for item in intent.directives:
            if item.days <= 0:
                item.days = DEFAULT_FOCUS_DAYS
            item.days = min(item.days, MAX_FOCUS_DAYS)
        return intent

    async def translate_to_en(self, text: str) -> str:
        """Translates a short query to English (the archive's language) before embedding.

        The archive is embedded in English; translating the search improves
        recall. If it's already English, returns it unchanged. On any failure,
        returns the original (never takes down the search).
        """
        text = (text or "").strip()
        if not text:
            return text
        try:
            resp = await self._client.messages.create(
                model=self._model,
                max_tokens=120,
                system=(
                    "Translate the user's short search query to English for "
                    "semantic search. If it is already English, return it "
                    "unchanged. Reply with ONLY the translation — no quotes, no "
                    "explanation."
                ),
                messages=[{"role": "user", "content": text}],
            )
        except Exception:  # noqa: BLE001 — translation never takes down the search
            logger.exception("translate_to_en: failed; using the original query")
            return text
        out = "".join(
            getattr(b, "text", "") for b in resp.content
            if getattr(b, "type", None) == "text"
        ).strip()
        return out or text
