"""GitHub ingestion: trending repos by topic, via the public Search API.

For each query/topic, it searches for recently CREATED repos, sorted by stars
(an honest proxy for "a new repo that's already taking off"). For each repo, it
also fetches the README (best-effort) — so the embedding has real content and
the curator can summarize "what the repo is".

Auth: OPTIONAL, but a GITHUB_TOKEN is RECOMMENDED here — without a token, the
core REST API (used to read READMEs) is only 60 req/h; with a token, 5000/h.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from ..common.models import IngestedPost
from .base import IngestionSource

GITHUB_API = "https://api.github.com"
GITHUB_SEARCH_URL = f"{GITHUB_API}/search/repositories"
README_MAX = 5000  # README chars stored/embedded


class GitHubSource(IngestionSource):
    name = "github"

    def __init__(
        self,
        queries: list[str],
        token: str | None = None,
        *,
        per_query: int = 10,
        recent_days: int = 30,
        min_stars: int = 5,
    ) -> None:
        self._queries = [q.strip() for q in queries if q.strip()]
        self._token = token
        self._per_query = per_query
        self._recent_days = recent_days
        self._min_stars = min_stars

    def _headers(self) -> dict[str, str]:
        h = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ai-news-aggregator",
        }
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def fetch(self) -> list[IngestedPost]:
        if not self._queries:
            return []

        since = (
            datetime.now(timezone.utc) - timedelta(days=self._recent_days)
        ).strftime("%Y-%m-%d")

        posts: list[IngestedPost] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(timeout=20.0, headers=self._headers()) as client:
            for query in self._queries:
                q = f"{query} created:>={since} stars:>={self._min_stars}"
                params = {
                    "q": q,
                    "sort": "stars",
                    "order": "desc",
                    "per_page": str(self._per_query),
                }
                try:
                    resp = await client.get(GITHUB_SEARCH_URL, params=params)
                    resp.raise_for_status()
                    items = resp.json().get("items", [])
                except Exception:  # isolate one query's failure; keep going with the rest
                    continue

                for repo in items:
                    rid = str(repo.get("id") or repo.get("full_name") or "")
                    if not rid or rid in seen:
                        continue
                    seen.add(rid)
                    readme = await self._fetch_readme(client, repo.get("full_name"))
                    posts.append(self._to_post(repo, query, readme))
        return posts

    async def _fetch_readme(self, client: httpx.AsyncClient, full_name: str | None) -> str:
        """Read the README as raw text (best-effort). Empty on failure/rate-limit."""
        if not full_name:
            return ""
        try:
            r = await client.get(
                f"{GITHUB_API}/repos/{full_name}/readme",
                headers={"Accept": "application/vnd.github.raw+json"},
            )
            if r.status_code == 200:
                return r.text[:README_MAX]
        except Exception:
            pass
        return ""

    @staticmethod
    def _to_post(repo: dict, query: str, readme: str = "") -> IngestedPost:
        full_name = repo.get("full_name") or repo.get("name") or ""
        description = (repo.get("description") or "").strip()
        language = repo.get("language") or "?"
        stars = repo.get("stargazers_count") or 0
        topics = repo.get("topics") or []
        created = repo.get("created_at")

        published = None
        if created:
            try:
                published = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                published = None

        topics_str = (" · " + " ".join(f"#{t}" for t in topics[:6])) if topics else ""
        body = (
            f"{full_name} — {description or '(no description)'}\n"
            f"⭐ {stars} · {language}{topics_str}"
        )
        if readme:
            body += f"\n\n--- README ---\n{readme}"

        return IngestedPost(
            source_platform="github",
            source_id=str(repo.get("id") or full_name),
            source_url=repo.get("html_url") or "",
            raw_text=body,
            author=(repo.get("owner") or {}).get("login"),
            published_at=published,
            metadata={
                "full_name": full_name,
                "stars": stars,
                "language": language,
                "topics": topics,
                "forks": repo.get("forks_count"),
                "query": query,
            },
        )
