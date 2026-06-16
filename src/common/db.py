"""Access to Postgres (Supabase) with pgvector. All async via asyncpg."""
from __future__ import annotations

import json
from typing import Any

import asyncpg
from pgvector.asyncpg import register_vector

from .models import IngestedPost, Verdict


class Database:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=1,
            max_size=5,
            init=self._init_conn,
            # Turn off the prepared-statement cache -> compatible with the Supabase
            # pooler (pgbouncer/Supavisor in transaction mode). The perf cost is
            # negligible at our volume.
            statement_cache_size=0,
        )

    @staticmethod
    async def _init_conn(conn: asyncpg.Connection) -> None:
        await register_vector(conn)  # vector type <-> list/ndarray
        await conn.set_type_codec(
            "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database.connect() was not called")
        return self._pool

    # ---------------------------------------------------------------- users
    async def get_or_create_user(
        self, telegram_user_id: int, display_name: str | None = None
    ) -> int:
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                """
                INSERT INTO users (telegram_user_id, display_name)
                VALUES ($1, $2)
                ON CONFLICT (telegram_user_id)
                    DO UPDATE SET display_name =
                        COALESCE(EXCLUDED.display_name, users.display_name)
                RETURNING id
                """,
                telegram_user_id,
                display_name,
            )
            return row["id"]

    # ---------------------------------------------------------------- posts
    async def upsert_post(self, p: IngestedPost) -> int | None:
        """Inserts if new; returns the id, or None if it already existed (dedup)."""
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                """
                INSERT INTO posts
                    (source_platform, source_id, source_url, author,
                     published_at, raw_text, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (source_platform, source_id) DO NOTHING
                RETURNING id
                """,
                p.source_platform, p.source_id, p.source_url, p.author,
                p.published_at, p.raw_text, p.metadata,
            )
            return row["id"] if row else None

    async def posts_pending_embedding(self, limit: int = 100) -> list[asyncpg.Record]:
        async with self.pool.acquire() as c:
            return await c.fetch(
                "SELECT id, raw_text FROM posts "
                "WHERE embedding IS NULL AND raw_text IS NOT NULL "
                "ORDER BY ingested_at LIMIT $1",
                limit,
            )

    async def set_embedding(self, post_id: int, embedding: list[float], model: str) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                "UPDATE posts SET embedding = $2, embedding_model = $3 WHERE id = $1",
                post_id, embedding, model,
            )

    async def posts_pending_curation(self, limit: int = 100) -> list[asyncpg.Record]:
        async with self.pool.acquire() as c:
            return await c.fetch(
                "SELECT id, source_platform, author, raw_text, metadata FROM posts "
                "WHERE verdict IS NULL AND raw_text IS NOT NULL "
                "ORDER BY ingested_at LIMIT $1",
                limit,
            )

    async def mark_curation(self, post_id: int, v: Verdict, model: str) -> None:
        approved = v.verdict == "approve"
        async with self.pool.acquire() as c:
            await c.execute(
                """
                UPDATE posts SET
                    verdict = $2, confidence = $3, category = $4,
                    reject_reason = $5, rationale = $6, summary = $7,
                    curator_model = $8, curated_at = now()
                WHERE id = $1
                """,
                post_id,
                "approved" if approved else "rejected",
                v.confidence,
                v.primary_category,
                None if v.reject_reason == "none" else v.reject_reason,
                v.one_line_rationale,
                v.summary,
                model,
            )

    async def mark_curation_error(self, post_id: int, model: str) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                "UPDATE posts SET verdict = 'error', curator_model = $2, "
                "curated_at = now() WHERE id = $1",
                post_id, model,
            )

    async def approved_undelivered(
        self, user_id: int, limit: int = 20, max_age_days: int | None = None
    ) -> list[asyncpg.Record]:
        """Approved posts not yet delivered to this user.

        `max_age_days`: if set, ignores posts published more than N days ago
        (keeps those WITHOUT a date) — feed freshness.
        """
        where_age = (
            "AND (p.published_at IS NULL "
            "OR p.published_at > now() - ($3 || ' days')::interval)"
            if max_age_days is not None else ""
        )
        params: list = [user_id, limit]
        if max_age_days is not None:
            params.append(str(max_age_days))
        async with self.pool.acquire() as c:
            return await c.fetch(
                f"""
                SELECT p.id, p.source_platform, p.source_url, p.author, p.raw_text,
                       p.category, p.summary, p.embedding, p.metadata, p.published_at
                FROM posts p
                WHERE p.verdict = 'approved'
                  AND NOT EXISTS (
                      SELECT 1 FROM deliveries d
                      WHERE d.user_id = $1 AND d.post_id = p.id
                  )
                  {where_age}
                ORDER BY p.published_at DESC NULLS LAST
                LIMIT $2
                """,
                *params,
            )

    # ------------------------------------------------------------ deliveries
    async def record_delivery(
        self, user_id: int, post_id: int,
        telegram_message_id: int | None, affinity_score: float | None,
    ) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                """
                INSERT INTO deliveries
                    (user_id, post_id, telegram_message_id, affinity_score)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id, post_id) DO NOTHING
                """,
                user_id, post_id, telegram_message_id, affinity_score,
            )

    # ----------------------------------------------------------------- votes
    async def record_vote(
        self, user_id: int, post_id: int, vote: int,
        origin: str = "telegram", telegram_message_id: int | None = None,
    ) -> None:
        """UPSERT: 1 vote per (user, post). Re-voting overwrites."""
        async with self.pool.acquire() as c:
            await c.execute(
                """
                INSERT INTO votes
                    (user_id, post_id, vote, origin, telegram_message_id)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id, post_id)
                    DO UPDATE SET vote = EXCLUDED.vote, updated_at = now()
                """,
                user_id, post_id, vote, origin, telegram_message_id,
            )

    async def vote_counts(
        self, user_id: int, platforms: list[str] | None = None
    ) -> tuple[int, int]:
        """(likes, dislikes) for the user, optionally only for certain platforms."""
        async with self.pool.acquire() as c:
            if platforms:
                row = await c.fetchrow(
                    "SELECT "
                    "COUNT(*) FILTER (WHERE v.vote = 1)  AS likes, "
                    "COUNT(*) FILTER (WHERE v.vote = -1) AS dislikes "
                    "FROM votes v JOIN posts p ON p.id = v.post_id "
                    "WHERE v.user_id = $1 AND p.source_platform = ANY($2)",
                    user_id, platforms,
                )
            else:
                row = await c.fetchrow(
                    "SELECT "
                    "COUNT(*) FILTER (WHERE vote = 1)  AS likes, "
                    "COUNT(*) FILTER (WHERE vote = -1) AS dislikes "
                    "FROM votes WHERE user_id = $1",
                    user_id,
                )
            return int(row["likes"]), int(row["dislikes"])

    async def nearest_votes(
        self, user_id: int, query_embedding, k: int = 5,
        platforms: list[str] | None = None,
    ) -> list[asyncpg.Record]:
        """The k nearest ALREADY-VOTED posts, optionally only from `platforms`.

        Restricting by platform keeps affinity SEPARATE per bucket (votes on
        repos don't influence news and vice versa).
        """
        where_plat = "AND p.source_platform = ANY($4)" if platforms else ""
        params: list = [user_id, query_embedding, k]
        if platforms:
            params.append(platforms)
        async with self.pool.acquire() as c:
            return await c.fetch(
                f"""
                SELECT v.vote AS vote, (p.embedding <=> $2) AS dist
                FROM votes v JOIN posts p ON p.id = v.post_id
                WHERE v.user_id = $1 AND p.embedding IS NOT NULL
                  {where_plat}
                ORDER BY p.embedding <=> $2
                LIMIT $3
                """,
                *params,
            )

    async def liked_centroid(self, user_id: int) -> list[float] | None:
        """Centroid of the liked embeddings — the user's affinity prior."""
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                """
                SELECT AVG(p.embedding) AS centroid
                FROM votes v JOIN posts p ON p.id = v.post_id
                WHERE v.user_id = $1 AND v.vote = 1 AND p.embedding IS NOT NULL
                """,
                user_id,
            )
        centroid = row["centroid"] if row else None
        if centroid is None:
            return None
        return centroid.tolist() if hasattr(centroid, "tolist") else list(centroid)

    async def recall_liked(
        self, user_id: int, query_embedding: list[float],
        limit: int = 20, since_days: int | None = None,
    ) -> list[asyncpg.Record]:
        """RECALL: posts THIS user liked, by similarity to the query.

        Filtering by user_id is the privacy invariant — only the user's own 👍
        come in; other people's votes never show up.
        """
        where_since = "AND p.published_at > now() - ($4 || ' days')::interval" if since_days else ""
        params: list[Any] = [user_id, query_embedding, limit]
        if since_days:
            params.append(str(since_days))
        async with self.pool.acquire() as c:
            return await c.fetch(
                f"""
                SELECT p.id, p.source_url, p.author, p.raw_text, p.category,
                       p.embedding <=> $2 AS distance
                FROM votes v JOIN posts p ON p.id = v.post_id
                WHERE v.user_id = $1 AND v.vote = 1
                  AND p.embedding IS NOT NULL
                  {where_since}
                ORDER BY p.embedding <=> $2
                LIMIT $3
                """,
                *params,
            )

    # ----------------------------------------------------------------- focus
    async def set_focus(
        self, user_id: int, bucket: str, topic: str, embedding, days: int
    ) -> None:
        """Sets the active direction of a bucket (replaces the previous one for that bucket).

        One direction per (user, bucket) at a time: re-steering swaps the topic.
        `days` sets the validity (expires_at = now() + days).
        """
        async with self.pool.acquire() as c:
            async with c.transaction():
                await c.execute(
                    "DELETE FROM focus WHERE user_id = $1 AND bucket = $2",
                    user_id, bucket,
                )
                await c.execute(
                    """
                    INSERT INTO focus (user_id, bucket, topic, embedding, expires_at)
                    VALUES ($1, $2, $3, $4, now() + ($5 || ' days')::interval)
                    """,
                    user_id, bucket, topic, embedding, str(days),
                )

    async def active_focus(self, user_id: int, bucket: str) -> list[asyncpg.Record]:
        """Current (non-expired) directions of a bucket."""
        async with self.pool.acquire() as c:
            return await c.fetch(
                """
                SELECT topic, embedding, weight, expires_at
                FROM focus
                WHERE user_id = $1 AND bucket = $2
                  AND (expires_at IS NULL OR expires_at > now())
                ORDER BY created_at DESC
                """,
                user_id, bucket,
            )

    async def clear_focus(self, user_id: int, bucket: str | None = None) -> int:
        """Deletes the user's directions (from one bucket, or all of them). Returns the count."""
        async with self.pool.acquire() as c:
            if bucket:
                row = await c.fetchrow(
                    "WITH d AS (DELETE FROM focus WHERE user_id = $1 AND bucket = $2 "
                    "RETURNING 1) SELECT count(*) AS n FROM d",
                    user_id, bucket,
                )
            else:
                row = await c.fetchrow(
                    "WITH d AS (DELETE FROM focus WHERE user_id = $1 RETURNING 1) "
                    "SELECT count(*) AS n FROM d",
                    user_id,
                )
            return int(row["n"])

    async def all_active_focus_topics(self) -> list[str]:
        """Topics with an active /focus (from any user) — they lower the curator's bar."""
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                "SELECT DISTINCT topic FROM focus "
                "WHERE expires_at IS NULL OR expires_at > now()"
            )
        return [r["topic"] for r in rows]

    # --------------------------------------------------------------- search
    async def search_pool(
        self, user_id: int, query_embedding, limit: int = 10
    ) -> list[asyncpg.Record]:
        """Semantic search over the CURATED archive: approved + your saved/liked.

        Unlike recall_liked (👍 only): here you get anything good that already
        passed curation, whether you voted on it or not — so "ask and ye shall
        find". The `liked` flag marks what you liked.
        """
        async with self.pool.acquire() as c:
            async with c.transaction():
                # High ef_search: the 'approved' filter discards many neighbors (the
                # pool has lots of rejected ones); without it the search comes back nearly empty.
                await c.execute("SET LOCAL hnsw.ef_search = 200")
                return await c.fetch(
                    """
                    SELECT p.id, p.source_url, p.author, p.raw_text, p.category,
                           p.source_platform,
                           (p.embedding <=> $2) AS distance,
                           EXISTS (
                               SELECT 1 FROM votes v
                               WHERE v.user_id = $1 AND v.post_id = p.id AND v.vote = 1
                           ) AS liked
                    FROM posts p
                    WHERE p.embedding IS NOT NULL
                      AND (
                          p.verdict = 'approved'
                          OR p.source_platform = 'manual'
                          OR EXISTS (
                              SELECT 1 FROM votes v
                              WHERE v.user_id = $1 AND v.post_id = p.id AND v.vote = 1
                          )
                      )
                    ORDER BY p.embedding <=> $2
                    LIMIT $3
                    """,
                    user_id, query_embedding, limit,
                )

    async def recall_voted(
        self, user_id: int, query_embedding, vote: int | None = None,
        limit: int = 10,
    ) -> list[asyncpg.Record]:
        """Conversational recall: posts YOU voted on, by similarity to the topic.

        vote=1 -> 👍 only; vote=-1 -> 👎 only; None -> any vote. Returns v.vote
        so the bot can mark ❤️/👎. Scoped by user_id = privacy (only your votes).
        """
        where_vote = "AND v.vote = $4" if vote is not None else ""
        params: list[Any] = [user_id, query_embedding, limit]
        if vote is not None:
            params.append(vote)
        async with self.pool.acquire() as c:
            return await c.fetch(
                f"""
                SELECT p.id, p.source_url, p.author, p.raw_text, p.category,
                       v.vote AS vote, (p.embedding <=> $2) AS distance
                FROM votes v JOIN posts p ON p.id = v.post_id
                WHERE v.user_id = $1 AND p.embedding IS NOT NULL
                  {where_vote}
                ORDER BY p.embedding <=> $2
                LIMIT $3
                """,
                *params,
            )

    # --------------------------------------------------------------- balance
    async def get_balance(self, user_id: int, bucket: str) -> float | None:
        """Fraction of NEW content desired in this bucket (None = app default)."""
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                "SELECT (settings #>> ARRAY['balance', $2])::real AS frac "
                "FROM users WHERE id = $1",
                user_id, bucket,
            )
        return None if row is None or row["frac"] is None else float(row["frac"])

    async def set_balance(self, user_id: int, bucket: str, fresh_fraction: float) -> None:
        """Writes the bucket's freshness fraction into users.settings->balance->bucket."""
        async with self.pool.acquire() as c:
            await c.execute(
                """
                UPDATE users
                SET settings = jsonb_set(
                    CASE WHEN settings ? 'balance' THEN settings
                         ELSE settings || '{"balance":{}}'::jsonb END,
                    ARRAY['balance', $2], to_jsonb($3::real), true)
                WHERE id = $1
                """,
                user_id, bucket, float(fresh_fraction),
            )

    async def balance_signal(
        self, user_id: int, platforms: list[str]
    ) -> list[asyncpg.Record]:
        """(vote, delivery affinity_score) of the voted posts in this bucket.

        Feeds the auto-balancing: votes on LOW-score cards (freshness slots) vs
        HIGH-score cards (relevance slots) reveal whether the user likes
        novelty or relevance.
        """
        async with self.pool.acquire() as c:
            return await c.fetch(
                """
                SELECT v.vote AS vote, COALESCE(d.affinity_score, 0.0) AS score
                FROM votes v
                JOIN deliveries d ON d.user_id = v.user_id AND d.post_id = v.post_id
                JOIN posts p ON p.id = v.post_id
                WHERE v.user_id = $1 AND p.source_platform = ANY($2)
                """,
                user_id, platforms,
            )
