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

logger = logging.getLogger("recall")

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


async def semantic_recall(
    db, embedder, user_id: int, query: str, *,
    mode: str = "archive", vote: int | None = None, limit: int = 10,
    min_score: float = RERANK_MIN_SCORE,
):
    """Two-stage recall. `query` is the TEXT (the reranker needs it, not the vector).

    mode="archive" -> the whole curated archive (`db.search_pool`).
    mode="voted"   -> only what the user voted on (`db.recall_voted`, with `vote`).

    Returns the Records ALREADY reordered by the reranker and filtered by
    `min_score` (at most `limit`). On reranker failure, falls back to the old
    cosine-distance cut — it never breaks the search.
    """
    qv = await embedder.embed_query(query)
    if mode == "voted":
        cands = await db.recall_voted(
            user_id, qv, vote=vote, limit=CANDIDATE_POOL, max_dist=CANDIDATE_MAX_DIST,
        )
    else:
        cands = await db.search_pool(
            user_id, qv, limit=CANDIDATE_POOL, max_dist=CANDIDATE_MAX_DIST,
        )
    if not cands:
        return []

    docs = [((r["raw_text"] or " ")[:_RERANK_DOC_CHARS]) for r in cands]
    try:
        ranked = await embedder.rerank(query, docs)
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
