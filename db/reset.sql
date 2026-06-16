-- =============================================================================
-- RESET — drops everything schema.sql creates. Run BEFORE reapplying the schema.
-- WARNING: DESTRUCTIVE — deletes ALL data (posts, votes, deliveries, focus).
-- Usage on Supabase: SQL Editor → paste this file → Run; then paste schema.sql → Run.
-- =============================================================================

DROP TABLE IF EXISTS focus       CASCADE;
DROP TABLE IF EXISTS votes       CASCADE;
DROP TABLE IF EXISTS deliveries  CASCADE;
DROP TABLE IF EXISTS posts       CASCADE;
DROP TABLE IF EXISTS users       CASCADE;

DROP FUNCTION IF EXISTS set_updated_at()          CASCADE;
DROP FUNCTION IF EXISTS sync_post_vote_tallies()  CASCADE;  -- defensive (may not exist)

-- The pgvector extension can stay — schema.sql uses CREATE EXTENSION IF NOT EXISTS.
