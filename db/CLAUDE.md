# db/ — Database schema (PostgreSQL + pgvector on Supabase)

This folder holds the SQL that defines the project's database. The database is
multi-tenant from the start: the design separates a post's **quality** (curated
exactly once, in a pool shared by all users) from each person's **taste** (which
lives in the per-user votes and deliveries). The practical upshot is that adding
more people to the project doesn't multiply the curation cost — a post is curated
once and reused. The main operator is simply user #1. Everything runs on
PostgreSQL 15+ with the pgvector extension (>= 0.7.0), on Supabase's Free plan.
Embeddings are 1024-dimensional (Voyage `voyage-4-lite`, L2-normalized, so cosine
== inner product) and the curator is Claude Haiku 4.5.

## How to apply it on Supabase

On Supabase: **SQL Editor → paste the file's contents → Run**. For a clean
database, first run `reset.sql` (wipes everything) and then `schema.sql`
(recreates it). On a brand-new database, `schema.sql` alone is enough. The
pgvector extension is created by `schema.sql` itself via
`CREATE EXTENSION IF NOT EXISTS vector`.

## Files

- **`schema.sql`** -> The entire database structure (tables, indexes, triggers).
  It's the source of truth for the data model. Key points:
  - **`users`** -> one row per person in the project. `telegram_user_id` is unique
    and acts as a lock: only that id may vote. `settings` is a free-form JSONB —
    it's where `settings->balance` lives (the user's feed balance preference,
    e.g. repos vs. news). The operator is just `id = 1`.
  - **`posts`** -> the **shared pool**: one row per unique post from any source
    (`reddit`, `twitter`, `seed`, `github`, `manual`), curated ONCE. There's no
    vote column here, because voting is per-user. Dedup is guaranteed by the
    unique constraint `(source_platform, source_id)` — the same post never enters
    twice (ingestion uses `ON CONFLICT ... DO NOTHING`). It stores the curator's
    quality verdict (`verdict` approved/rejected/error, `confidence`, `category`,
    `summary` for the Telegram card, `curator_model`), the per-platform variable
    `metadata` JSONB (subreddit, score, likes…), the raw `raw_text` (which the
    retention job prunes on old rejected posts, recording `raw_text_pruned_at`),
    and the `embedding vector(1024)` with a pinned `embedding_model` (if the model
    changes, it can be detected and re-embedded).
  - **`deliveries`** -> what was delivered to each user (defines the scope of the
    feed and recall). Links `user_id` + `post_id` (unique per pair, with
    `ON DELETE CASCADE`), stores the `telegram_message_id` and the
    `affinity_score` (similarity to the user's taste at delivery time, may be
    null).
  - **`votes`** -> the per-user "Gold Standard": 1 vote per `(user_id, post_id)`,
    `vote` is `1` (👍) or `-1` (👎). Re-voting overwrites via UPSERT (`ON CONFLICT
    ... DO UPDATE`). `origin` distinguishes a Telegram click from cold-start
    examples (`seed`) or `manual`. It's the basis for semantic recall ("did I
    like something about XPTO?") and the affinity prior (centroid of the
    embeddings of liked posts).
  - **`focus`** -> a temporary feed "steer", per user and per bucket
    (`bucket` repos/news). E.g. "for the next 2 days I want news about the
    financial world of AI". The `topic` (text) feeds collection/search, the
    `embedding` re-ranks delivery, and `weight` controls the intensity.
    `expires_at` null = no expiry. The idea is one active steer per (user,
    bucket); re-steering replaces the previous one.
  - **Indexes** -> notably `posts_embedding_hnsw`: an **HNSW** index with
    `vector_cosine_ops` (chosen over IVFFlat because the table grows with
    incremental inserts). The `ORDER BY` operator in the search must be `<=>`
    (cosine) to match the index. There's also a GIN on `metadata`, indexes on
    `verdict`, `source_platform`, `published_at`, and per-user indexes on
    `deliveries` and `votes`.
  - **Triggers** -> `set_updated_at()` keeps `updated_at` fresh on `posts` and
    `votes` on every UPDATE.
  - **Operational notes (at the end of the file, they're comments — not DDL)** ->
    ready-made SQL snippets for dedup at ingestion, vote UPSERT, the recall query
    (with the `SET hnsw.ef_search = 100` tip to improve recall), the affinity
    prior (cold-start gate: only kicks in with ~20 votes per class), and the
    retention pruning that clears `raw_text` from old rejected posts that nobody
    liked (keeping verdict + metadata + embedding) to hold the 500MB line.

- **`reset.sql`** -> **WARNING: DESTRUCTIVE. WIPES EVERYTHING.** Runs `DROP TABLE
  ... CASCADE` on the schema's 5 tables (`focus`, `votes`, `deliveries`, `posts`,
  `users`) and drops the associated functions. It's for zeroing out the database
  before reapplying `schema.sql`. With real data, running it loses everything
  irreversibly. The pgvector extension is deliberately left standing (`schema.sql`
  recreates it with `IF NOT EXISTS`).
