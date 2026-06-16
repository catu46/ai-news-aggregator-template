"""MCP server: exposes YOUR curated archive as tools for Claude.

A THIN shell over the Database — it reuses exactly what the bot already uses
(search_pool / recall_voted / active_focus). That way you ask Claude directly
("what did I like about RAG?") without opening Telegram.

Run it as a local MCP server (stdio) and plug it into any Claude Code/Desktop:

    claude mcp add archive -- .venv/bin/python -m src.mcp_server.server

Each person runs THEIR own instance (their Supabase, their telegram_user_id), so
the data never mixes — it's the same multi-tenant isolation as the rest of the
project. The user is resolved via TELEGRAM_USER_ID (.env) or the 1st in sources.yaml.

Identifiers in English; comments in English.
"""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from ..common.config import load_settings, load_sources
from ..common.db import Database
from ..common.embeddings import Embedder

mcp = FastMCP("archive-ai")

# Lazy state: connects on the 1st use of a tool and reuses the pool afterward.
_db: Database | None = None
_embedder: Embedder | None = None


def _resolve_telegram_id() -> int:
    """Who 'you' are in this instance: TELEGRAM_USER_ID from .env, or 1st in the yaml."""
    env = os.getenv("TELEGRAM_USER_ID")
    if env:
        return int(env)
    users = load_sources()
    if users:
        return users[0].telegram_user_id
    raise RuntimeError(
        "Set TELEGRAM_USER_ID in .env (or a user in config/sources.yaml)."
    )


async def _ensure() -> tuple[Database, Embedder, int]:
    """Ensures DB + Embedder are connected; also returns the internal user_id."""
    global _db, _embedder
    if _db is None:
        settings = load_settings()
        _db = Database(settings.database_url)
        await _db.connect()
        _embedder = Embedder(settings)
    user_id = await _db.get_or_create_user(_resolve_telegram_id())
    return _db, _embedder, user_id


def _headline(raw_text: str | None, width: int = 160) -> str:
    for line in (raw_text or "").splitlines():
        line = line.strip()
        if line:
            return line[:width]
    return (raw_text or "").strip()[:width]


@mcp.tool()
async def search_archive(query: str, limit: int = 10) -> str:
    """Semantic search over the curated archive (approved + what you saved/liked).

    Use it for "is there anything in my archive about X?". ❤️ marks what you liked.
    """
    db, embedder, user_id = await _ensure()
    vec = await embedder.embed_query(query)
    rows = await db.search_pool(user_id, vec, limit=limit)
    if not rows:
        return f"Nothing in the archive about “{query}”."
    lines = []
    for i, r in enumerate(rows, start=1):
        mark = " ❤️" if r["liked"] else ""
        lines.append(
            f"{i}.{mark} [{r['source_platform']}] {_headline(r['raw_text'])}\n"
            f"   {r['source_url'] or ''}"
        )
    return "\n".join(lines)


@mcp.tool()
async def recall_votes(query: str, vote: str = "any", limit: int = 10) -> str:
    """Recalls what YOU voted on a topic, by similarity.

    vote: "liked" (👍 only), "disliked" (👎 only) or "any" (any vote).
    Use it for "what was that thing about X I liked/disliked?".
    """
    db, embedder, user_id = await _ensure()
    vec = await embedder.embed_query(query)
    vote = {"liked": 1, "disliked": -1}.get(vote)  # None = any vote
    rows = await db.recall_voted(user_id, vec, vote=vote, limit=limit)
    if not rows:
        return f"You haven't voted on anything about “{query}” yet."
    lines = []
    for i, r in enumerate(rows, start=1):
        mark = "❤️" if r["vote"] == 1 else "👎"
        lines.append(f"{i}. {mark} {_headline(r['raw_text'])}\n   {r['source_url'] or ''}")
    return "\n".join(lines)


@mcp.tool()
async def see_focus() -> str:
    """Shows the active direction (/focus) of each bucket: 📦 repos and 🗞️ news."""
    db, _embedder, user_id = await _ensure()
    lines = []
    for bucket, label in (("repos", "📦 repos"), ("news", "🗞️ news")):
        for r in await db.active_focus(user_id, bucket):
            lines.append(f"{label}: {r['topic']}")
    return "\n".join(lines) if lines else "No active focus."


def main() -> None:
    """Starts the MCP server over stdio (default mode for Claude Code/Desktop)."""
    mcp.run()


if __name__ == "__main__":
    main()
