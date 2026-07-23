"""Two-stage semantic search: BROAD vector recall -> rerank (Voyage).

Why two stages: in a single-domain archive (everything is AI) the embedding
space collapses into a cone, so cosine distance can't separate relevant from
irrelevant — a short query vs a long doc, and shared vocabulary ("quantum
COMPUTING" sticks to hardware posts). The reranker reads query+text TOGETHER and
gives a 0..1 score that actually separates (measured on a real archive: present
topic ~0.59-0.82, off-topic / vocabulary-confused ~0.29-0.46). So: the vector
stage brings MANY candidates (cheap, high recall) and the reranker does the fine
cut. This replaces the old brittle cosine floor (`_RELEVANCE_MAX_DIST`), which
cut on-topic results and let off-topic through.
"""
from __future__ import annotations

import logging

import asyncpg

logger = logging.getLogger("recall")

# "Undefined column/table" errors raised by the hybrid migration (posts.fts)
# when it hasn't been applied yet — caught so we fall back to the pure vector
# path instead of breaking search. See db/migrations/2026-07-22-hybrid-fts.sql.
_MISSING_HYBRID_COLUMN_ERRORS = (
    asyncpg.exceptions.UndefinedColumnError,
    asyncpg.exceptions.UndefinedTableError,
)
# Warn about the pending migration only once (not on every search).
_warned_hybrid_migration_pending = False

# Minimum reranker score for a result to count as relevant. Calibrated on real
# data: present topic ~0.59-0.82; off-topic / vocabulary-confused ~0.29-0.46.
# 0.5 falls in the gap between the two.
RERANK_MIN_SCORE = 0.5
# How many candidates the vector stage brings for the reranker to sift.
CANDIDATE_POOL = 40
# LOOSE floor for the vector stage: only avoids reranking absolute garbage
# (real off-topic sits >= ~0.8). The actual relevance cut is the reranker's.
CANDIDATE_MAX_DIST = 0.85
# Text sent to the reranker per doc (trimmed to fit the limit and stay cheap).
_RERANK_DOC_CHARS = 1600


async def _archive_recall(db, user_id: int, query: str, qv) -> list:
    """Stage 1 (candidates) for the archive: hybrid (FTS+vector) once the
    `posts.fts` migration has been applied; otherwise falls back to the pure
    vector path (`search_pool`).

    The fallback is automatic and silent (1 warning only, not per search) —
    it's safe to deploy this code BEFORE applying
    db/migrations/2026-07-22-hybrid-fts.sql.
    """
    global _warned_hybrid_migration_pending
    try:
        return await db.hybrid_recall(user_id, query, qv, limit=CANDIDATE_POOL)
    except _MISSING_HYBRID_COLUMN_ERRORS:
        if not _warned_hybrid_migration_pending:
            logger.warning(
                "hybrid_recall: column posts.fts doesn't exist yet (migration "
                "db/migrations/2026-07-22-hybrid-fts.sql pending) — falling "
                "back to pure vector recall (search_pool). This warning only "
                "shows once."
            )
            _warned_hybrid_migration_pending = True
        return await db.search_pool(
            user_id, qv, limit=CANDIDATE_POOL, max_dist=CANDIDATE_MAX_DIST,
        )


async def semantic_recall(
    db, embedder, user_id: int, query: str, *,
    mode: str = "archive", vote: int | None = None, limit: int = 10,
    min_score: float = RERANK_MIN_SCORE,
):
    """Two-stage recall. `query` is the TEXT (the reranker needs it, not the vector).

    mode="archive" -> the whole curated archive (`db.hybrid_recall` FTS+vector,
                       or `db.search_pool` pure vector if the `posts.fts` column
                       migration hasn't been applied yet).
    mode="voted"   -> only what the user voted on (`db.recall_voted`, with `vote`).

    Returns the Records ALREADY reordered by the reranker and filtered by
    `min_score` (at most `limit`). On reranker failure, falls back to the old
    cosine-distance cut — it never breaks the search.
    """
    qv = await embedder.embed_query(query)
    if mode == "voted":
        # The voted archive is small (only posts the user voted on); the gain
        # from FTS there is marginal and hybrid_recall's contract mirrors
        # search_pool, not recall_voted — keep pure vector here.
        cands = await db.recall_voted(
            user_id, qv, vote=vote, limit=CANDIDATE_POOL, max_dist=CANDIDATE_MAX_DIST,
        )
    else:
        cands = await _archive_recall(db, user_id, query, qv)
    if not cands:
        return []

    docs = [((r["raw_text"] or " ")[:_RERANK_DOC_CHARS]) for r in cands]
    try:
        ranked = await embedder.rerank(query, docs, use_profile=True)
    except Exception:  # noqa: BLE001 — rerank never breaks the search
        logger.exception("rerank failed; falling back to the cosine-distance cut")
        from .db import _RELEVANCE_MAX_DIST
        return [r for r in cands if float(r["distance"]) < _RELEVANCE_MAX_DIST][:limit]

    out = []
    for idx, score in ranked:  # already sorted by score desc
        if score < min_score:
            continue
        out.append(cands[idx])
        if len(out) >= limit:
            break
    return out
