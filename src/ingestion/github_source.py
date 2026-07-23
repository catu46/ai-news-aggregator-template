"""GitHub ingestion: trending repos by topic, via the public Search API.

For each query/topic, it fetches repos CREATED recently, sorted by stars
(an honest proxy for "a new repo that's already taking off"). For each repo, it
also fetches the README (best-effort) — that way the embedding has real content
and the curator can summarize "what the repo is".

Auth: OPTIONAL, but a GITHUB_TOKEN is RECOMMENDED here — without a token, the
core REST API (used to read READMEs) is only 60 req/h; with a token, 5000/h.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

import httpx

from ..common.models import IngestedPost
from .base import IngestionSource

GITHUB_API = "https://api.github.com"
GITHUB_SEARCH_URL = f"{GITHUB_API}/search/repositories"
README_MAX = 5000  # README chars stored/embedded

# A repo link pasted into the bot: github.com/{owner}/{repo}[/whatever]. Ignores
# any extra path (tree/blob/issues/...) and always reads the root repo (deliberate
# simplification — see fetch_repo). Does NOT match gist.github.com (different host).
_REPO_URL_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)(?:/.*)?/?$",
    re.IGNORECASE,
)


def _github_headers(token: str | None = None) -> dict[str, str]:
    """Default headers for the GitHub REST API. With no explicit token, falls
    back to the process's GITHUB_TOKEN (the same var Settings uses; already in
    os.environ via load_dotenv() when config.py is imported). Shared by
    GitHubSource._headers and fetch_repo (a link pasted into the bot)."""
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ai-news-aggregator",
    }
    tok = token or os.environ.get("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


async def _fetch_readme(client: httpx.AsyncClient, full_name: str | None) -> str:
    """Reads the README as raw text (best-effort). Empty on failure/rate-limit.
    Shared by the continuous ingestion (GitHubSource.fetch) and fetch_repo
    (a link pasted into the bot)."""
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


async def fetch_repo(url: str, *, timeout: float = 20.0) -> tuple[str, str | None]:
    """Reads a single repo via the GitHub REST API — used when the owner pastes
    a github.com link into the bot (Jina Reader sometimes chokes/rate-limits
    there; unlike x.com, here Jina still serves as a 2nd option, see bot.py).

    Extracts {owner}/{repo} from the URL (ignores extra path like /tree/main/...;
    always reads the root repo — deliberate simplification). A github.com link
    that is NOT a repo (github.com/orgs/..., github.com/settings, gist.github.com
    — different host, etc.) either doesn't match the regex or 404s on the API ->
    RuntimeError/HTTPStatusError, and the caller falls back to Jina.

    Returns (text, title). Title = full_name + 1st line of the description;
    text = full_name + description + topics + stars + README excerpt (up to
    README_MAX chars — same shape used by continuous ingestion).
    """
    m = _REPO_URL_RE.match(url.strip())
    if not m:
        raise RuntimeError(f"github: not a recognizable repo link: {url}")
    owner, repo = m.group(1), m.group(2)
    if repo.endswith(".git"):
        repo = repo[:-4]

    async with httpx.AsyncClient(timeout=timeout, headers=_github_headers()) as client:
        resp = await client.get(f"{GITHUB_API}/repos/{owner}/{repo}")
        resp.raise_for_status()
        data = resp.json()
        readme = await _fetch_readme(client, data.get("full_name"))

    full_name = data.get("full_name") or f"{owner}/{repo}"
    description = (data.get("description") or "").strip()
    stars = data.get("stargazers_count") or 0
    topics = data.get("topics") or []
    topics_str = (" · " + " ".join(f"#{t}" for t in topics[:6])) if topics else ""

    text = f"{full_name} — {description or '(no description)'}\n⭐ {stars}{topics_str}"
    if readme:
        text += f"\n\n--- README ---\n{readme}"

    first_line = description.splitlines()[0] if description else ""
    title = f"{full_name}: {first_line}" if first_line else full_name
    return text, title


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
        min_stars_established: int = 200,
    ) -> None:
        self._queries = [q.strip() for q in queries if q.strip()]
        self._token = token
        self._per_query = per_query
        self._recent_days = recent_days
        self._min_stars = min_stars
        # star floor for the ESTABLISHED pass (big, actively-maintained repos)
        self._min_stars_established = min_stars_established

    def _headers(self) -> dict[str, str]:
        return _github_headers(self._token)

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
                # Two passes per topic: (A) NEW trending — created recently with
                # traction; (B) ESTABLISHED — high stars and recently active
                # (pushed), to bring the big/useful repos, not just newborns.
                searches = (
                    f"{query} created:>={since} stars:>={self._min_stars}",
                    f"{query} stars:>={self._min_stars_established} pushed:>={since}",
                )
                for q in searches:
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
                    except Exception:  # isolate one query's failure; continue
                        continue

                    for repo in items:
                        rid = str(repo.get("id") or repo.get("full_name") or "")
                        if not rid or rid in seen:
                            continue
                        seen.add(rid)
                        readme = await _fetch_readme(client, repo.get("full_name"))
                        posts.append(self._to_post(repo, query, readme))
        return posts

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
