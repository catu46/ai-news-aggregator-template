"""Pipeline runner — ONE complete cycle: ingest → embed → curate.

Runs as `python -m src.pipeline`. Idempotent and safe to run on a schedule
(cron on Railway): each stage only touches what is still pending in the database,
and deduplication by (source_platform, source_id) happens on upsert.

Stages:
    1. Setup    — load_settings, connect the Database, build the Embedder.
    2. Ingest   — for now ONLY Reddit (one RedditSource per user, built from
                  their subreddits + reddit_user_agent). The 'XSource' hook for X
                  is marked as TODO (not built yet — do NOT import).
    3. Embed    — embed posts without an embedding, in batches.
    4. Curate   — global quality verdict via Haiku (no user signal:
                  similarity_signal=None). Respects the spend cap (BudgetExceeded).

Delivery to Telegram does NOT happen here — it is the responsibility of the bot's JobQueue.
"""
from __future__ import annotations

import asyncio
import logging

from .common.config import load_settings, load_sources
from .common.db import Database
from .common.embeddings import Embedder
from .ingestion.reddit_source import RedditSource
from .ingestion.github_source import GitHubSource
from .ingestion.x_source import XSource

# The curator is still a sibling module (src/curation/). The pipeline only
# orchestrates it: it expects AnthropicCurator.classify(...) -> Verdict | None and BudgetExceeded.
from .curation.curator import BudgetExceeded, Curator, make_curator

logger = logging.getLogger("pipeline")

# Batch size when draining the pending queue. Keeps memory usage predictable
# and gives natural log/progress checkpoints during long runs.
EMBED_BATCH = 100
CURATION_BATCH = 100


# ---------------------------------------------------------------------------
# 2. INGEST
# ---------------------------------------------------------------------------
async def run_ingestion(db: Database) -> int:
    """Collect from the feeds and upsert. Returns the count of NEW posts.

    Today only Reddit. Builds one RedditSource per user (from the subreddits
    declared in sources.yaml) and uses the global reddit_user_agent from settings.
    """
    settings = load_settings()
    sources = load_sources()

    new_count = 0
    for user in sources:
        # Active directions (/foco) inject topics into INGESTION — on top of
        # re-ranking delivery in the bot. This way focus actually PULLS in new
        # content, not just reorders what is already in the pool.
        user_id = await db.get_or_create_user(user.telegram_user_id, user.display_name)
        repos_focus = [r["topic"] for r in await db.active_focus(user_id, "repos")]
        news_focus = [r["topic"] for r in await db.active_focus(user_id, "news")]

        # ---- Reddit ----------------------------------------------------
        if user.subreddits:
            reddit = RedditSource(
                subreddits=user.subreddits,
                user_agent=settings.reddit_user_agent,
            )
            new_count += await _ingest_source(db, reddit, user_key=user.key)

        # ---- GitHub (trending repos by topic + active focus) ------------------
        gh_queries = list(user.github_queries) + repos_focus
        if gh_queries:
            gh = GitHubSource(
                queries=gh_queries,
                token=settings.github_token,
            )
            new_count += await _ingest_source(db, gh, user_key=user.key)

        # ---- X / Twitter (accounts + searches + active focus) -------------------
        x_searches = list(user.x_searches) + news_focus
        if user.x_accounts or x_searches:
            x = XSource(
                accounts=user.x_accounts,
                searches=x_searches,
                auth_token=settings.twitter_auth_token,
                ct0=settings.twitter_ct0,
            )
            new_count += await _ingest_source(db, x, user_key=user.key)

    logger.info("ingest: %d new post(s) in total", new_count)
    return new_count


async def _ingest_source(db: Database, source, *, user_key: str) -> int:
    """Run a source, upsert each post, and count the ones that are actually new.

    Failures of a single source (network, 429, etc.) are logged and isolated —
    they do not bring down the cycle for the other sources/users.
    """
    try:
        posts = await source.fetch()
    except Exception:  # noqa: BLE001 — isolate source/network failure
        logger.exception("ingest[%s/%s]: fetch failed", user_key, source.name)
        return 0

    new_count = 0
    for post in posts:
        try:
            post_id = await db.upsert_post(post)
        except Exception:  # noqa: BLE001 — isolate per-post database failure
            logger.exception(
                "ingest[%s/%s]: upsert failed for %s",
                user_key, source.name, post.source_id,
            )
            continue
        if post_id is not None:  # None = duplicate (dedup in the database)
            new_count += 1

    logger.info(
        "ingest[%s/%s]: %d collected, %d new",
        user_key, source.name, len(posts), new_count,
    )
    return new_count


# ---------------------------------------------------------------------------
# 3. EMBED
# ---------------------------------------------------------------------------
async def run_embedding(db: Database, embedder: Embedder, settings) -> int:
    """Embed posts without an embedding, in batches. Returns the total embedded."""
    total = 0
    while True:
        pending = await db.posts_pending_embedding(limit=EMBED_BATCH)
        if not pending:
            break

        texts = [r["raw_text"] for r in pending]
        try:
            vectors = await embedder.embed_documents(texts)
        except Exception:  # noqa: BLE001 — embeddings provider failure
            logger.exception("embed: failed on batch of %d post(s); aborting stage", len(pending))
            break

        for record, vector in zip(pending, vectors):
            try:
                await db.set_embedding(record["id"], vector, settings.embedding_model)
                total += 1
            except Exception:  # noqa: BLE001 — isolate per-post database failure
                logger.exception("embed: failed to write embedding for post %s", record["id"])

        logger.info("embed: batch of %d written (running total %d)", len(pending), total)

        # If the database returned fewer than the batch size, the queue is empty.
        if len(pending) < EMBED_BATCH:
            break

    logger.info("embed: %d post(s) embedded in total", total)
    return total


# ---------------------------------------------------------------------------
# 4. CURATE
# ---------------------------------------------------------------------------
async def run_curation(db: Database, curator: Curator) -> int:
    """Classify pending posts via Haiku. Returns the total classified.

    GLOBAL quality verdict: there is no user signal here, so
    similarity_signal=None. On BudgetExceeded, the stage ends gracefully
    (the pending posts are left for the next cycle).
    """
    total = 0
    while True:
        pending = await db.posts_pending_curation(limit=CURATION_BATCH)
        if not pending:
            break

        for record in pending:
            # Light context (author/subreddit) at the start of the text — helps the
            # curator without departing from the classify(post_text, similarity_signal) signature.
            meta = record["metadata"] or {}
            header_bits = [f"SOURCE: {record['source_platform']}"]
            if record["author"]:
                header_bits.append(f"author: {record['author']}")
            if meta.get("subreddit"):
                header_bits.append(f"subreddit: r/{meta['subreddit']}")
            prefix = " | ".join(header_bits) + "\n\n"
            try:
                verdict = await curator.classify(
                    post_text=prefix + (record["raw_text"] or ""),
                    similarity_signal=None,  # global verdict: no user signal
                )
            except BudgetExceeded as exc:
                # Monthly spend cap reached: stop curating gracefully.
                logger.warning("curate: spend cap reached (%s); ending stage", exc)
                logger.info("curate: %d post(s) classified before the cap", total)
                return total
            except Exception:  # noqa: BLE001 — API/parse error for a single isolated post
                logger.exception("curate: failed to classify post %s", record["id"])
                await db.mark_curation_error(record["id"], curator_model_of(curator))
                continue

            if verdict is None:
                # Failure handled by the curator (refusal / max_tokens / empty parse).
                await db.mark_curation_error(record["id"], curator_model_of(curator))
                continue

            await db.mark_curation(record["id"], verdict, curator_model_of(curator))
            total += 1

        logger.info("curate: batch processed (running total %d)", total)

        if len(pending) < CURATION_BATCH:
            break

    logger.info("curate: %d post(s) classified in total", total)
    return total


def curator_model_of(curator: AnthropicCurator) -> str:
    """Curator's model name to write into the curator_model columns.

    Tolerant of the curator's final interface: uses .model if exposed, otherwise a
    generic label. Keeps the pipeline decoupled from internal details.
    """
    return getattr(curator, "model", "curator")


# ---------------------------------------------------------------------------
# main — orchestrates one complete cycle
# ---------------------------------------------------------------------------
async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 1. Setup
    settings = load_settings()
    db = Database(settings.database_url)
    await db.connect()
    embedder = Embedder(settings)
    curator = make_curator(settings)

    try:
        # 2. Ensure every user exists (idempotent).
        for user in load_sources():
            await db.get_or_create_user(user.telegram_user_id, user.display_name)

        # 3. Ingest → Embed → Curate (in this order; each stage feeds the next).
        await run_ingestion(db)
        await run_embedding(db, embedder, settings)
        await run_curation(db, curator)
    finally:
        await db.close()

    logger.info("pipeline: cycle complete")


if __name__ == "__main__":
    asyncio.run(main())
