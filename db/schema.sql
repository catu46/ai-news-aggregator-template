-- =============================================================================
-- AI News Aggregator — MULTI-TENANT-READY schema
-- PostgreSQL 15+ / pgvector >= 0.7.0   (Supabase Free)
-- Embeddings: Voyage voyage-4-lite, 1024-dim, L2-normalized (cosine == dot)
-- Curator:    Claude Haiku 4.5 (claude-haiku-4-5)
--
-- Data model: SHARED POOL of posts (curated once = a QUALITY verdict,
-- user-agnostic). Each person's TASTE lives in per-user votes/deliveries.
-- Adding people does not multiply the cost.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- users: each person in the project (the operator is just user #1)
-- ---------------------------------------------------------------------------
CREATE TABLE users (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    telegram_user_id  BIGINT      NOT NULL UNIQUE,   -- lock: only this id may vote
    display_name      TEXT,
    settings          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    is_active         BOOLEAN     NOT NULL DEFAULT true,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- posts: SHARED POOL. One row per unique post from any source,
-- curated ONCE. No vote columns here (votes are per-user).
-- ---------------------------------------------------------------------------
CREATE TABLE posts (
    id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- Source metadata (typed, indexable)
    source_platform     TEXT        NOT NULL
                            CHECK (source_platform IN ('reddit', 'twitter', 'seed', 'github', 'manual')),
    source_id           TEXT        NOT NULL,          -- native platform id (dedup)
    source_url          TEXT        NOT NULL,
    author              TEXT,
    published_at        TIMESTAMPTZ,                   -- published at the source
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Per-platform variable fields (subreddit, score, likes, media flags...)
    metadata            JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- Raw text (pruned for old rejected posts by the retention job)
    raw_text            TEXT,
    raw_text_pruned_at  TIMESTAMPTZ,

    -- Curator verdict (global content QUALITY)
    verdict             TEXT        CHECK (verdict IN ('approved','rejected','error')),
    confidence          REAL        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    category            TEXT,
    reject_reason       TEXT,
    rationale           TEXT,
    summary             TEXT,                          -- short summary for the Telegram card
    curator_model       TEXT,                          -- e.g. claude-haiku-4-5
    curated_at          TIMESTAMPTZ,

    -- Embedding for semantic search + pinned model (detects a swap = re-embed)
    embedding           vector(1024),
    embedding_model     TEXT,                          -- e.g. voyage-4-lite

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Dedup: the same post is never ingested twice
    CONSTRAINT posts_source_uniq UNIQUE (source_platform, source_id),
    -- Pruning bookkeeping
    CONSTRAINT posts_prune_consistency CHECK (
        (raw_text IS NOT NULL AND raw_text_pruned_at IS NULL)
        OR (raw_text_pruned_at IS NOT NULL)
    )
);

-- ---------------------------------------------------------------------------
-- deliveries: what was DELIVERED to each user (scope of the feed + recall).
-- ---------------------------------------------------------------------------
CREATE TABLE deliveries (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id              BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    post_id              BIGINT      NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    telegram_message_id  BIGINT,
    affinity_score       REAL,        -- similarity to the user's taste (prior, nullable)
    delivered_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT deliveries_user_post_uniq UNIQUE (user_id, post_id)
);

-- ---------------------------------------------------------------------------
-- votes: the PER-USER "Gold Standard". UPSERT: 1 vote per (user, post).
-- origin = 'telegram' (click in the bot) | 'seed' (cold-start example).
-- ---------------------------------------------------------------------------
CREATE TABLE votes (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id              BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    post_id              BIGINT      NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    vote                 SMALLINT    NOT NULL CHECK (vote IN (-1, 1)),   -- 1=👍, -1=👎
    origin               TEXT        NOT NULL DEFAULT 'telegram'
                             CHECK (origin IN ('telegram','seed','manual')),
    telegram_message_id  BIGINT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 1 vote per post per user (re-voting overwrites via ON CONFLICT)
    CONSTRAINT votes_user_post_uniq UNIQUE (user_id, post_id)
);

-- ---------------------------------------------------------------------------
-- focus: temporary feed "direction", per user and per bucket (repos/news).
-- e.g. "for the next 2 days I want news about the AI finance world".
-- Re-ranks delivery toward the topic AND injects the topic into collection (search + repos).
-- One ACTIVE direction per (user, bucket); redirecting replaces the previous one.
-- ---------------------------------------------------------------------------
CREATE TABLE focus (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     BIGINT      NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bucket      TEXT        NOT NULL CHECK (bucket IN ('repos', 'news')),
    topic       TEXT        NOT NULL,                  -- topic, good for search
    embedding   vector(1024),                          -- for re-ranking delivery
    weight      REAL        NOT NULL DEFAULT 1.2,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ                             -- NULL = no expiry
);
CREATE INDEX focus_active_idx ON focus (user_id, bucket, expires_at);

-- ---------------------------------------------------------------------------
-- Triggers: keep updated_at fresh
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at := now(); RETURN NEW; END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER posts_set_updated_at BEFORE UPDATE ON posts
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER votes_set_updated_at BEFORE UPDATE ON votes
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------
-- Vector search (recall + affinity). HNSW for incremental inserts on a
-- growing table (not IVFFlat). cosine == dot because the vectors are
-- L2-normalized; the ORDER BY operator must match: <=> (cosine).
CREATE INDEX posts_embedding_hnsw ON posts
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 200);

CREATE INDEX posts_source_platform_idx ON posts (source_platform);
CREATE INDEX posts_published_at_idx    ON posts (published_at DESC);
CREATE INDEX posts_verdict_idx         ON posts (verdict);
CREATE INDEX posts_metadata_gin        ON posts USING gin (metadata);

CREATE INDEX deliveries_user_idx ON deliveries (user_id, delivered_at DESC);
CREATE INDEX deliveries_post_idx ON deliveries (post_id);

CREATE INDEX votes_user_vote_idx ON votes (user_id, vote);
CREATE INDEX votes_post_idx      ON votes (post_id);

-- =============================================================================
-- OPERATIONAL NOTES (not DDL — reference)
-- =============================================================================
--
-- DEDUP on ingestion:
--   INSERT INTO posts (...) VALUES (...)
--   ON CONFLICT (source_platform, source_id) DO NOTHING;
--
-- VOTE (UPSERT latest-vote-per-post, per user):
--   INSERT INTO votes (user_id, post_id, vote, origin) VALUES (:u, :p, :v, 'telegram')
--   ON CONFLICT (user_id, post_id) DO UPDATE SET vote = EXCLUDED.vote, updated_at = now();
--
-- RECALL ("did I recently like something about XPTO?") — per user:
--   SELECT p.source_url, p.embedding <=> :q AS dist
--   FROM votes v JOIN posts p ON p.id = v.post_id
--   WHERE v.user_id = :uid AND v.vote = 1 AND p.embedding IS NOT NULL
--     -- AND p.published_at > now() - interval '30 days'    -- optional filter
--   ORDER BY p.embedding <=> :q
--   LIMIT 20;
--   (run SET hnsw.ef_search = 100; in the session to improve recall)
--
-- AFFINITY PRIOR (cold-start gate: only kicks in with >= ~20 votes/class):
--   centroid = SELECT AVG(embedding) FROM votes v JOIN posts p ON p.id=v.post_id
--              WHERE v.user_id = :uid AND v.vote = 1;
--
-- RETENTION (keeps 500MB under control): prunes raw_text of old rejected posts
-- that nobody liked (keeps verdict + metadata + embedding):
--   UPDATE posts SET raw_text = NULL, raw_text_pruned_at = now()
--   WHERE verdict = 'rejected' AND raw_text IS NOT NULL
--     AND curated_at < now() - interval '30 days'
--     AND id NOT IN (SELECT post_id FROM votes WHERE vote = 1);
