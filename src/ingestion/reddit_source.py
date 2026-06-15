"""Reddit ingestion via the public RSS/Atom feed (/r/<sub>/new/.rss).

The .json endpoint started returning 403 (a Reddit block, Jun 2026) even with a
browser User-Agent. The .rss feed still responds 200 with a simple User-Agent —
so we use it. We parse the Atom feed with feedparser and extract the body text
(which comes as HTML) with BeautifulSoup.

Uses the multireddit trick ('sub1+sub2+...') to grab everything in a single
request (lower chance of a 429). Dedup by source_id happens in the database.

Caveat: unauthenticated traffic from a datacenter IP (e.g. Railway) may be
blocked/throttled by Reddit. If that happens, you can route Reddit through an
authenticated backend (e.g. agent-reach with a cookie) behind the same interface.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import feedparser
import httpx
from bs4 import BeautifulSoup

from ..common.models import IngestedPost
from .base import IngestionSource

REDDIT_BASE = "https://www.reddit.com"
# Variants of the TOPIC search (focus): top over growing windows + "hot".
# The database deduplicates the overlap; each variant is a separate request.
_REDDIT_SEARCH_VARIANTS = (
    {"sort": "top", "t": "day"},
    {"sort": "top", "t": "week"},
    {"sort": "top", "t": "month"},
    {"sort": "hot"},
)
_SUB_RE = re.compile(r"/r/([^/]+)/")


class RedditSource(IngestionSource):
    name = "reddit"

    def __init__(
        self, subreddits: list[str], user_agent: str, limit: int = 25,
        searches: list[str] | None = None,
    ) -> None:
        self._subs = [s.strip().lstrip("r/").strip("/") for s in subreddits if s.strip()]
        self._ua = user_agent
        self._limit = limit
        # TOPIC searches (e.g. /focus topics) — via /search.rss, also without auth.
        self._searches = [s.strip() for s in (searches or []) if s.strip()]

    async def fetch(self) -> list[IngestedPost]:
        if not self._subs and not self._searches:
            return []

        headers = {"User-Agent": self._ua}
        posts: list[IngestedPost] = []
        async with httpx.AsyncClient(
            timeout=20.0, headers=headers, follow_redirects=True
        ) as client:
            # fixed subs: multireddit in a single request
            if self._subs:
                multi = "+".join(self._subs)
                posts += await self._fetch_feed(
                    client, f"{REDDIT_BASE}/r/{multi}/new/.rss",
                    {"limit": str(self._limit)},
                )
            # TOPIC searches (focus): top day/week/month + "hot" —
            # relevance by upvotes across multiple time scales. The RSS doesn't carry
            # the score (Reddit sorts server-side); the database dedups the overlap.
            for q in self._searches:
                for variant in _REDDIT_SEARCH_VARIANTS:
                    posts += await self._fetch_feed(
                        client, f"{REDDIT_BASE}/search.rss",
                        {"q": q, **variant, "limit": str(self._limit)},
                    )
        return posts

    async def _fetch_feed(
        self, client: httpx.AsyncClient, url: str, params: dict
    ) -> list[IngestedPost]:
        """GET + parse ONE Reddit RSS feed. Isolates failures (one feed won't take down the rest)."""
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
        except Exception:  # noqa: BLE001 — feed unavailable/throttled -> skip
            return []
        feed = feedparser.parse(resp.text)
        # real POSTS only (link /comments/) — search sometimes returns communities.
        return [
            self._to_post(e)
            for e in feed.entries[: self._limit]
            if "/comments/" in (e.get("link") or "")
        ]

    @staticmethod
    def _to_post(entry) -> IngestedPost:
        # Atom id: "t3_abc123" -> "abc123" (falls back to the link if absent)
        raw_id = entry.get("id") or entry.get("link") or ""
        source_id = raw_id.rsplit("/", 1)[-1].replace("t3_", "") or raw_id

        # subreddit from the link (the feed mixes the multireddit's subs)
        link = entry.get("link") or ""
        m = _SUB_RE.search(link)
        subreddit = m.group(1) if m else None

        # author comes as "/u/username"
        author = (entry.get("author") or "").replace("/u/", "").strip() or None

        # date
        published = None
        tp = entry.get("published_parsed") or entry.get("updated_parsed")
        if tp:
            published = datetime(*tp[:6], tzinfo=timezone.utc)

        # body: content/summary comes as HTML -> extract text
        html = ""
        if entry.get("content"):
            html = entry["content"][0].get("value", "")
        if not html:
            html = entry.get("summary", "") or ""
        body = BeautifulSoup(html, "html.parser").get_text(" ", strip=True) if html else ""

        title = entry.get("title") or ""
        raw_text = f"{title}\n\n{body}".strip()

        return IngestedPost(
            source_platform="reddit",
            source_id=source_id,
            source_url=link,
            raw_text=raw_text,
            author=author,
            published_at=published,
            metadata={"subreddit": subreddit, "title": title},
        )
