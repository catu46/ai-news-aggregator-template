# src/common — shared core

This folder is the "infrastructure" that everything else in the project uses: how to read configuration, how to talk to the database, what the data shapes that flow between modules are, and how to generate embeddings. None of the files here implement product logic (ingestion, curation, bot) — they just provide the pieces those modules call. If you're onboarding, start here: understanding these four files means understanding the vocabulary of the whole system.

Flow overview: the sources (Reddit/X/GitHub) produce `IngestedPost` → they land in Postgres via `upsert_post` → they get an embedding (Voyage) → they pass through curation (Haiku, produces `Verdict`) → the approved ones are delivered to the user on Telegram, who votes 👍/👎 → votes become a personal archive searchable by meaning (pgvector). The owner's chat messages become `ChatIntent`/`FocusItem` to steer the feed.

## File by file

**`config.py` — typed configuration (`.env` + the YAMLs)**
- `Settings` (frozen dataclass): all the app's keys/secrets and parameters. Required (error if missing): `ANTHROPIC_API_KEY`, `VOYAGE_API_KEY`, `TELEGRAM_BOT_TOKEN`, `DATABASE_URL`. With a default: `CURATOR_MODEL` (claude-haiku-4-5), `CURATOR_MONTHLY_BUDGET_USD` (8), `EMBEDDING_MODEL` (voyage-4-lite), `REDDIT_USER_AGENT`. Optional (can be None): Twitter tokens (`auth_token`/`ct0`), `EXA_API_KEY`, `GITHUB_TOKEN`.
- `load_settings()` reads the `.env` (via `load_dotenv()` at import) and assembles the `Settings`. It's the single entry point for configuration — other modules receive this object.
- `load_sources()` reads `config/sources.yaml` and returns a list of `UserSources` (one per user): where to fetch that user's content from — `subreddits`, X accounts and searches, GitHub queries — tied to `telegram_user_id`. It's what ingestion consumes.
- `load_seeds()` reads `config/seeds.yaml` and returns `{user_key: [SeedExample]}`. These are cold-start examples labeled `gold` or `noise` to give curation/affinity a reference before the user has any history. Skips placeholders (empty text or starting with `<`). Ties to the user by the same `key` used in `sources.yaml`.
- `ROOT`/`CONFIG_DIR` point to the project root and the `config/` folder (resolved from this file's path).

**`db.py` — Postgres/pgvector layer (Supabase), everything async via `asyncpg`**
- `Database(dsn)` class: connection pool (1–5). `connect()`/`close()` manage the pool; `pool` (property) gives access and blows up if you forget to connect. In `_init_conn` it registers the `vector` type (pgvector) and the `jsonb` codec. `statement_cache_size=0` is intentional — compatibility with the Supabase pooler (pgbouncer/Supavisor in transaction mode).
- `get_or_create_user(telegram_user_id, display_name)` → ensures the user's row and returns the internal `id` (the rest of the API uses this `user_id`, not the Telegram one).
- `upsert_post(IngestedPost)` → inserts a new post and returns the `id`; if it already existed (same `source_platform` + `source_id`), returns `None`. It's the ingestion dedup.
- `posts_pending_embedding(limit)` / `set_embedding(post_id, embedding, model)` → queue and write the posts that don't have a vector yet. Pairs with `embeddings.py`.
- `posts_pending_curation(limit)` → posts without a verdict yet, for the curator to process.
- `mark_curation(post_id, Verdict, model)` → writes the curation result (approved/rejected, confidence, category, reject reason, summary, rationale). `mark_curation_error(...)` marks the verdict `error` when the LLM call fails.
- `approved_undelivered(user_id, limit)` → approved posts NOT yet delivered to this user (LEFT-anti-join with `deliveries`). It's the feed queue.
- `record_delivery(user_id, post_id, telegram_message_id, affinity_score)` → records that a post was delivered (idempotent per user+post).
- `record_vote(user_id, post_id, vote, origin, telegram_message_id)` → writes the vote (1 or -1); UPSERT, 1 vote per (user, post), re-voting overwrites.
- `vote_counts(user_id, platforms?)` → `(likes, dislikes)` tuple, optionally filtering by platform.
- `nearest_votes(user_id, query_embedding, k, platforms?)` → the k closest ALREADY-VOTED posts to the given vector; filtering by platform keeps affinity separate per "bucket" (repos don't influence news and vice versa). Used to estimate the affinity of a new post.
- `liked_centroid(user_id)` → centroid (mean) of the liked embeddings; a "prior" of the user's taste (can be `None` if there are no 👍 yet).
- `recall_liked(user_id, query_embedding, limit, since_days?)` → searches ONLY the user's own 👍 by similarity to the topic. The `user_id` filter is the privacy invariant — other people's votes never appear.
- `set_focus(user_id, bucket, topic, embedding, days)` → sets the active direction of a bucket (`repos`/`news`); replaces the previous one for that bucket and sets the validity (`expires_at = now + days`). `active_focus(user_id, bucket)` reads the non-expired directions. `clear_focus(user_id, bucket?)` deletes directions (from one bucket or all) and returns how many. Fed by `FocusItem` (see models).
- `search_pool(user_id, query_embedding, limit)` → semantic search over the CURATED archive: approved + manual + whatever the user liked (anything good that passed curation, voted on or not). Marks `liked` on what the user liked. Unlike `recall_liked`, which is only the 👍.
- `recall_voted(user_id, query_embedding, vote?, limit)` → conversational recall: posts YOU voted on, by similarity to the topic. `vote=1` only 👍, `vote=-1` only 👎, `None` any; returns `v.vote` so the bot can mark ❤️/👎. Scoped by `user_id` = privacy.
- `get_balance(user_id, bucket)` / `set_balance(user_id, bucket, fresh_fraction)` → desired fraction of NEW content per bucket, stored in `users.settings->balance->bucket` (JSONB). `None` on get = use the app default.

**`models.py` — the data shapes that flow between modules**
- `IngestedPost` (dataclass): the normalized post that ANY source emits (Reddit, X, GitHub, seed, manual). Key fields: `source_platform`, `source_id` (dedup key), `source_url`, `raw_text`, plus optional `author`/`published_at`/`metadata`. It's what goes into `upsert_post`.
- `Verdict` (Pydantic): the curator's verdict, Haiku 4.5's Structured Output schema — `verdict` (approve/reject), `confidence` (0..1), `primary_category`, `reject_reason`, `summary` (for the card), and `one_line_rationale`. Important: no numeric/string constraints in the schema (Structured Outputs doesn't support them); range validation is done in the app. It's what goes to `mark_curation`.
- `FocusItem` (Pydantic): a feed "direction" — `bucket` (`repos`=GitHub or `news`=X+Reddit), `topic` (short topic, good for search, can be in English), and `days` (validity). Becomes a `set_focus` call.
- `ChatIntent` (Pydantic): the interpreted intent of an owner's chat message — `kind` (`steer`/`recall`/`balance`/`other`), `directives` (list of `FocusItem`, when steer), `recall_query`/`recall_polarity` (when recall), `balance_bucket`/`balance_fresh` (when balance), and `reply` (short confirmation in PT-BR). It's the bridge between natural language and the `db.py` methods (focus/recall/balance).

**`embeddings.py` — Voyage AI wrapper**
- `Embedder(settings)`: client for `voyage-4-lite` (L2-normalized vectors, 1024-dim). Exposes a public `model` (e.g. the bot uses it when saving a manual link).
- `embed_documents(texts)` (input_type `document`, for posts) and `embed_query(text)` (input_type `query`, for search queries) — using the right type is what makes the query↔post similarity work well.
- Internally it processes in batches of 32 and runs the synchronous Voyage client in a thread (`asyncio.to_thread`) so it doesn't block the event loop. The vectors produced here are exactly the ones that go into `set_embedding` and the ones the similarity queries in `db.py` consume.
