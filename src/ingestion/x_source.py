"""X/Twitter ingestion via twitter-cli (free mode, cookie-based).

Calls the `twitter` CLI via subprocess:
  - accounts:  twitter user-posts <handle> --max N --json
  - searches:  twitter search "<query>" -t Latest --max N --json

Auth: twitter-cli reads TWITTER_AUTH_TOKEN/TWITTER_CT0 from the environment
(headless server) or the cookies from the logged-in browser (on your Mac). We
inject the 2 tokens into the subprocess env when available, so it works on Railway.

Accounts and searches are a CONTINUOUS feed (not a backfill). Everything goes
through the curator afterwards.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from datetime import datetime

from ..common.models import IngestedPost
from .base import IngestionSource

logger = logging.getLogger("ingestion.x")


def _resolve_twitter_bin() -> str:
    """Find the `twitter` executable: PATH -> venv bin -> ~/.local/bin."""
    candidates = [
        shutil.which("twitter"),
        os.path.join(sys.prefix, "bin", "twitter"),
        os.path.expanduser("~/.local/bin/twitter"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return "twitter"


class XSource(IngestionSource):
    name = "x"

    def __init__(
        self,
        accounts: list[str],
        searches: list[str],
        auth_token: str | None = None,
        ct0: str | None = None,
        *,
        per_account: int = 10,
        per_search: int = 20,
        timeout: float = 60.0,
    ) -> None:
        self._accounts = [a.strip().lstrip("@") for a in accounts if a.strip()]
        self._searches = [s.strip() for s in searches if s.strip()]
        self._auth_token = auth_token
        self._ct0 = ct0
        self._per_account = per_account
        self._per_search = per_search
        self._timeout = timeout
        self._bin = _resolve_twitter_bin()

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self._auth_token:
            env["TWITTER_AUTH_TOKEN"] = self._auth_token
        if self._ct0:
            env["TWITTER_CT0"] = self._ct0
        return env

    async def fetch(self) -> list[IngestedPost]:
        seen: set[str] = set()
        posts: list[IngestedPost] = []

        for handle in self._accounts:
            for tw in await self._run(["user-posts", handle, "--max", str(self._per_account)]):
                self._collect(tw, seen, posts)

        # Each search in TWO tabs: Latest (newest) + Top (most engaged) —
        # freshness and relevance. Dedup by id (seen) covers the overlap.
        for query in self._searches:
            for tab in ("Latest", "Top"):
                for tw in await self._run(
                    ["search", query, "-t", tab, "--max", str(self._per_search)]
                ):
                    self._collect(tw, seen, posts)

        return posts

    def _collect(self, tw: dict, seen: set[str], posts: list[IngestedPost]) -> None:
        tid = str(tw.get("id") or "")
        if not tid or tid in seen:
            return
        seen.add(tid)
        post = self._to_post(tw)
        if post is not None:
            posts.append(post)

    async def _run(self, args: list[str]) -> list[dict]:
        """Run `twitter <args> --json` and return the data[] list. Isolates failures."""
        cmd = [self._bin, *args, "--json"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,  # never waits for input (avoids hangs)
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env(),
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except (asyncio.TimeoutError, FileNotFoundError, OSError):
            logger.exception("x: failed to run %s", " ".join(args))
            return []

        if proc.returncode != 0:
            logger.warning(
                "x: '%s' exited with code %s: %s",
                " ".join(args), proc.returncode,
                (err or b"").decode("utf-8", "replace")[:300],
            )
            return []

        try:
            payload = json.loads(out.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            logger.warning("x: non-JSON output from '%s'", " ".join(args))
            return []

        if not payload.get("ok", True):
            logger.warning("x: '%s' returned ok=false", " ".join(args))
            return []
        data = payload.get("data") or []
        return data if isinstance(data, list) else []

    @staticmethod
    def _to_post(tw: dict) -> IngestedPost | None:
        tid = str(tw.get("id") or "")
        if not tid:
            return None
        author = tw.get("author") or {}
        screen = author.get("screenName") or "i"
        text = (tw.get("text") or "").strip()

        # Include the quoted tweet as context (relevant for the curator).
        quoted = tw.get("quotedTweet")
        if quoted and quoted.get("text"):
            qauthor = (quoted.get("author") or {}).get("screenName") or "?"
            text = f"{text}\n\n↪ quoting @{qauthor}: {quoted['text'].strip()}"

        published = None
        iso = tw.get("createdAtISO")
        if iso:
            try:
                published = datetime.fromisoformat(iso)
            except ValueError:
                published = None

        metrics = tw.get("metrics") or {}
        return IngestedPost(
            source_platform="twitter",
            source_id=tid,
            source_url=f"https://x.com/{screen}/status/{tid}",
            raw_text=text,
            author=screen,
            published_at=published,
            metadata={
                "name": author.get("name"),
                "like_count": metrics.get("likes"),
                "retweet_count": metrics.get("retweets"),
                "reply_count": metrics.get("replies"),
                "views": metrics.get("views"),
                "lang": tw.get("lang"),
                "is_retweet": tw.get("isRetweet"),
                "urls": tw.get("urls") or [],
                "has_media": bool(tw.get("media")),
            },
        )
