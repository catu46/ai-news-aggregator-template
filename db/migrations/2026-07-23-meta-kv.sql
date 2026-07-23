-- =============================================================================
-- Migration: generic system-state key/value table
-- =============================================================================
-- NOT applied automatically. This file is just the record of what needs to run
-- against the database (Supabase) — apply it manually (e.g. the Supabase SQL
-- editor, or `psql "$DATABASE_URL" -f db/migrations/2026-07-23-meta-kv.sql`)
-- whenever you decide to turn it on. Until then, the code is already prepared
-- to live without it: `SpendGuard` (src/curation/curator.py, via
-- `src/common/db.py:meta_get` / `add_curator_spend`) tries the database first
-- and, if the table doesn't exist, Postgres raises `UndefinedTableError` —
-- caught, logs a single warning, and falls back to the local file
-- (`~/.cache/ai-news-aggregator/spend.json`). In other words: you can deploy
-- this code BEFORE running this migration, without breaking anything — same
-- pattern as `2026-07-22-hybrid-fts.sql`.
--
-- WHY THIS EXISTS: Railway's disk is EPHEMERAL — every deploy brings up a
-- fresh container, without the previous one's filesystem. `SpendGuard` used
-- to persist the curator's monthly spend only in that local file; in
-- practice, every deploy reset the counter, and the `CURATOR_MONTHLY_BUDGET_USD`
-- cap never actually fired (the spend "seen" by the process never
-- accumulated across deploys). A table in Postgres (which IS persistent)
-- fixes that. `meta` is generic (not just for spend) because this same
-- problem — "I need to remember a number/state across deploys" — tends to
-- recur; `curator_spend` is just the first key to use it.
--
-- WHAT IT DOES:
--   Creates the `meta` table: `key` (text, primary key) -> `value` (JSONB,
--   any shape) + `updated_at` (timestamp of the last write). One row per
--   logical key; the curator's spend lives in value = {"YYYY-MM":
--   total_usd, ...} under the 'curator_spend' key (see
--   `Database.add_curator_spend` in src/common/db.py).
--
-- WITHOUT the timing concerns of the previous migration: there's no `ALTER
-- TABLE` rewriting existing rows and no GIN index to build here — it's just a
-- `CREATE TABLE` on a new, empty table, instant regardless of the rest of
-- the database's size. Still, if applying via asyncpg (a Python script)
-- instead of the Supabase SQL editor, remember `statement_cache_size=0` on
-- the pool (see `Database.connect` in src/common/db.py) — required by the
-- Supabase pooler (pgbouncer/Supavisor in transaction mode), otherwise
-- asyncpg's implicit `PREPARE` fails.
--
-- How it's used: `src/common/db.py:meta_get(key)` (generic read) and
-- `add_curator_spend(month_key, usd)` (atomic increment specific to the
-- curator's spend, read+write in a single transaction/UPSERT).
-- =============================================================================

CREATE TABLE meta (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
