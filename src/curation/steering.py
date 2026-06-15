"""Classifies a chat message from the feed owner into an intent (ChatIntent).

Four possible intents:
  - "steer"   -> steer the feed for a while ("for 2 days I want repos about
                 skills and news about AI finance").
  - "recall"  -> recall something HE ALREADY received and voted on ("what was
                 that RAG news I liked?", "there was a repo about agents I
                 disliked, which one was it?").
  - "balance" -> change the fresh×relevant mix of the feed ("send me more fresh
                 stuff", "focus on what's relevant to me").
  - "other"   -> anything else.

Same model family as the curator (Haiku 4.5) via Structured Outputs. It is cheap
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
# Safety cap: nobody pins a focus for more than ~2 months by accident.
MAX_FOCUS_DAYS = 60

_SYSTEM = """You interpret messages from the owner of a personal AI feed and
classify their intent into ONE of four.

The feed delivers TWO buckets per day:
- "repos": trending GitHub repositories.
- "news": "what people are talking about" — X/Twitter + Reddit (news, discussions).
On each card they tap 👍 or 👎.

1) kind = "steer" — they want to STEER the feed for a while. E.g.: "for the next
   two days I want news about the AI finance world and repos about skills".
   Fill `directives` with one item per (bucket, topic):
     - bucket: "repos" or "news"
     - topic: a few words, GOOD FOR SEARCH (in ENGLISH for technical topics —
       e.g.: "AI funding venture capital", "agent skills")
     - days: the stated timeframe converted to days; if unstated, 0 (the app applies the default).

2) kind = "recall" — they want to RECALL something they ALREADY received and
   voted on. E.g.: "what was that RAG news I liked?", "there was a repo about
   agents I disliked, which one was it?". Fill:
     - recall_query: the topic to search (in ENGLISH for technical topics)
     - recall_polarity: "liked" if they say they liked it / gave 👍; "disliked"
       if they say they didn't like it / gave 👎; "any" if unclear.

3) kind = "balance" — they want to change the MIX between FRESH and RELEVANT
   content in the feed. E.g.: "send me more fresh stuff", "too much old stuff is
   coming through", "I want half fresh half relevant", "focus on what's relevant
   to me". Fill:
     - balance_bucket: "repos", "news", or "both" (if unspecified, "both")
     - balance_fresh: fraction 0..1 of how much should be FRESH. Map the phrasing:
       "fresh only"≈0.9, "more fresh"≈0.6, "half-and-half"=0.5,
       "more relevant"≈0.25, "relevant only"≈0.1.

4) kind = "other" — small talk, a question about the bot, a thank-you, etc.

`reply`: ALWAYS in English, short. For "steer" confirm what you understood; for
"recall" say you'll look for it; for "balance" confirm the new mix; for "other"
give a 1-line pointer.

Unused fields default to: directives=[], recall_query="",
recall_polarity="any", balance_bucket="both", balance_fresh=0.4."""


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

        # `days` hygiene: 0/negative -> default; applies the safety cap.
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
