"""Reddit ingestion via the public RSS/Atom feed (/r/<sub>/new/.rss).

The .json endpoint started returning 403 (Reddit block, Jun 2026) even with a
browser User-Agent. The .rss feed still responds 200 with a simple User-Agent —
so we use that. We parse the Atom feed with feedparser and extract the body text
(which comes as HTML) with BeautifulSoup.

Uses the multireddit trick ('sub1+sub2+...') to grab everything in a single
request (less chance of a 429). Dedup by source_id happens in the database.

Caveat: unauthenticated traffic from a datacenter IP (e.g. Railway) may be
blocked/throttled by Reddit. If that happens, Reddit can be routed through an
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
_SUB_RE = re.compile(r"/r/([^/]+)/")


class RedditSource(IngestionSource):
    name = "reddit"

    def __init__(self, subreddits: list[str], user_agent: str, limit: int = 25) -> None:
        self._subs = [s.strip().lstrip("r/").strip("/") for s in subreddits if s.strip()]
        self._ua = user_agent
        self._limit = limit

    async def fetch(self) -> list[IngestedPost]:
        if not self._subs:
            return []

        multi = "+".join(self._subs)  # multireddit = 1 request for all subs
        url = f"{REDDIT_BASE}/r/{multi}/new/.rss"
        headers = {"User-Agent": self._ua}

        async with httpx.AsyncClient(
            timeout=20.0, headers=headers, follow_redirects=True
        ) as client:
            resp = await client.get(url, params={"limit": str(self._limit)})
            resp.raise_for_status()
            text = resp.text

        feed = feedparser.parse(text)
        return [self._to_post(e) for e in feed.entries[: self._limit]]

    @staticmethod
    def _to_post(entry) -> IngestedPost:
        # Atom id: "t3_abc123" -> "abc123" (falls back to the link if absent)
        raw_id = entry.get("id") or entry.get("link") or ""
        source_id = raw_id.rsplit("/", 1)[-1].replace("t3_", "") or raw_id

        # subreddit from the link (the feed mixes the multireddit's subs together)
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
