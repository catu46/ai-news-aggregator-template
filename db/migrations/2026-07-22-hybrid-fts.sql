-- =============================================================================
-- Migration: hybrid search (FTS + vector) over the archive — generated column
-- + GIN index
-- =============================================================================
-- NOT applied automatically. This file is just the record of what needs to run
-- against the database (Supabase) — apply it manually (e.g. the Supabase SQL
-- editor, or `psql "$DATABASE_URL" -f db/migrations/2026-07-22-hybrid-fts.sql`)
-- whenever you decide to turn it on. Until then, the code is already prepared
-- to live without it: `src/common/db.py:hybrid_recall` references `posts.fts`,
-- and if the column doesn't exist Postgres raises `UndefinedColumnError`;
-- `src/common/recall.py` catches that error and falls back to pure vector
-- recall (`search_pool`), logging a single warning. In other words: you can
-- deploy this code BEFORE running this migration, without breaking anything.
--
-- WHAT IT DOES:
--   1. Adds `posts.fts` — a GENERATED column (computed automatically by
--      Postgres from raw_text + summary, never written to directly) holding
--      the full-text search vector (tsvector).
--   2. Creates a GIN index on it, so text search (`@@`, `ts_rank`) stays fast
--      as the archive grows.
--
-- WHY 'simple' (not a language-specific dictionary like 'english'): if you
-- point this template at a bilingual or multilingual archive, a dictionary
-- with aggressive stemming for ONE language ("running" -> "run") would hurt
-- search in the others. The 'simple' config does only basic normalization
-- (lowercase + tokenization, no stemming, no stopwords) — more neutral for
-- mixed-language content, at the cost of not matching morphological variants
-- automatically (e.g. "agent" won't automatically match "agents"). If your
-- archive is single-language and this bothers you in practice, swap 'simple'
-- for your language's dictionary (e.g. 'english') — not part of this migration.
--
-- WHY STORED (not a virtual generated column): it's the only kind of
-- generated column Postgres supports indexing. The recompute cost only runs
-- when raw_text/summary change (INSERT/UPDATE), not on every read.
--
-- HEADS UP: because this is a GENERATED ALWAYS AS ... STORED column, Postgres
-- recomputes the tsvector for ALL existing rows when you run the ALTER TABLE
-- (it rewrites the whole table). On a small personal archive this should be
-- fast (seconds); on a large table it would be a blocking operation to plan
-- carefully. If you're applying this against Supabase's pooled connection
-- string, consider running it against the DIRECT (non-pooled) connection
-- instead, and `SET statement_timeout = 0;` first in that session — the
-- pooled connection's default statement timeout can otherwise kill a
-- full-table rewrite partway through on a bigger archive. (This is a
-- one-off migration concern; it's unrelated to the app's own
-- `statement_cache_size=0` on the asyncpg pool in `src/common/db.py`, which
-- exists for a different reason — pgbouncer/Supavisor transaction-mode
-- compatibility for every regular query, not for running this ALTER TABLE.)
--
-- How it's queried: `websearch_to_tsquery('simple', :query)` on the search
-- side (see `src/common/db.py:hybrid_recall`) — "Google-style" search syntax
-- (accepts quoted phrases, `-word` to exclude, etc.).
-- =============================================================================

ALTER TABLE posts
    ADD COLUMN fts tsvector
    GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(raw_text, '') || ' ' || coalesce(summary, ''))
    ) STORED;

CREATE INDEX posts_fts_gin ON posts USING gin (fts);
