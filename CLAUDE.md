# CLAUDE.md — Onboarding guide (project root)

> Project convention: comments and docstrings in **PT-BR**; identifiers (variables, functions, classes, tables, columns) in **English**. This file follows the same idea: prose in PT-BR, code names in English.

## What it is (in 30s)

A **personal** AI news and repository aggregator, designed as a **multi-tenant open-source template**: each person spins up their own copy (their bot, their database, their data — nothing is shared between instances). It collects from **GitHub / Reddit / X**, curates the **quality** of each item with **Claude Haiku 4.5** (a **swappable** curator — there's also a `KimiCurator` selectable via `CURATOR_PROVIDER=kimi`), and delivers a **morning digest 1x/day** at a **fixed time** (`DIGEST_HOUR`/`DIGEST_TZ`) on Telegram, split into **2 buckets** — 📦 repos (GitHub) and 🗞️ news (Reddit + X) — with cards that have 👍/👎 buttons. Your votes train the ranking, and the curated archive is also accessible to Claude via an **MCP server**. It runs comfortably on free tiers (~$0–10/month); the only variable cost is curation, which has a spend cap by design.

## Architecture / flow

Everything runs **inside the bot itself** (an always-on process): two jobs on python-telegram-bot's `JobQueue` — **daily delivery at a fixed time** (`run_daily` at hour `DIGEST_HOUR` in timezone `DIGEST_TZ`) and a pipeline every **30 min** (`PIPELINE_INTERVAL_SECONDS`). **No separate cron needed.**

```
sources (config/sources.yaml, per user) + active /focus topics
   Reddit (.rss) · GitHub (Search API + README) · X (twitter-cli via cookies)
        │   (/focus injects the topic: news → Reddit+X · repos → GitHub)
   INGEST  → upsert_post (dedup by (source_platform, source_id))
        │
   SHARED POOL: `posts` table (curated 1x = QUALITY verdict, user-agnostic)
        ├── EMBED  (Voyage voyage-4-lite, 1024-dim, L2-normalized)  → embedding IS NULL, batches of 100
        └── CURATE (Haiku 4.5 — or Kimi via CURATOR_PROVIDER — → Verdict: approve/reject + category + summary + rationale)
                    verdict IS NULL, batches of 100; aware of /focus topics (interests loosen the bar);
                    SpendGuard pauses when the cap is exceeded
        │
   DELIVERY IN 2 BUCKETS (daily job at DIGEST_HOUR or /feed/​/run on demand)
        approved_undelivered → ranks WITHIN each bucket (📦 repos = github · 🗞️ news = reddit+twitter)
        freshness cutoff: nothing published more than 30 days ago (DELIVERY_MAX_AGE_DAYS)
        slots split between RELEVANCE (affinity + focus) × FRESHNESS, governed by BALANCE
        │
   Telegram: cards with 👍/👎 → on_vote writes to `votes`
        ├── AFFINITY      (ranks, separated PER BUCKET; only ranks, never hides)
        ├── FOCO          (/focus by speech: re-ranks delivery AND injects the topic into INGESTION)
        ├── BALANCE       (by speech: new × relevant mix; saved in the user's settings)
        └── AUTO-BALANCE  (1x/day, in the daily job: learns the new×relevant mix from YOUR votes)
        │
   RECALL / MCP  →  semantic_recall (2 stages: broad vector recall → RERANK) · active_focus  (serves /search, chat, and the MCP)
                  ▸ chat recall: a GENERAL question ('any') searches the WHOLE archive (mode=archive); the RERANKER does the relevance cut (cosine alone can't separate in a single-domain archive) ◂
                  ▸ 'liked'/'disliked' recalls only what YOU voted on (mode=voted) ◂

                  ▸ EVERYTHING scoped by user_id (derived from telegram_user_id) ◂
```

## Folder map (one line each)

- `src/bot/` — Telegram interface + the 2 jobs (delivery and pipeline). See `src/bot/CLAUDE.md`.
- `src/ingestion/` — the sources (Reddit / GitHub / X) behind the `IngestionSource` ABC. See `src/ingestion/CLAUDE.md`.
- `src/curation/` — Haiku curator (quality) + steerer (chat→intent). See `src/curation/CLAUDE.md`.
- `src/common/` — config, database (asyncpg+pgvector), Pydantic models, Voyage embedder. See `src/common/CLAUDE.md`.
- `src/mcp_server/` — FastMCP server that exposes the archive to Claude. See `src/mcp_server/CLAUDE.md`.
- `db/` — `schema.sql` (DDL) and `reset.sql`. See `db/CLAUDE.md`.
- `config/` — `sources.yaml` (sources per user) and `seeds.yaml` (cold-start); `*.example.yaml` version-controlled. See `config/CLAUDE.md`.
- `src/pipeline.py` — runner for 1 `ingest → embed → curate` cycle (standalone or called by the bot's job).
- `src/seed.py` — cold-start load: turns taste examples into `source_platform='seed'` posts + preloaded votes.

> The per-folder `CLAUDE.md` files go deeper into each module; this file is just the overall map. If a folder's `CLAUDE.md` doesn't exist yet, the entry point is the `README.md` and the folder's own files.

## Conventions

- **Language:** comments/docstrings in PT-BR; identifiers in English (code, tables, columns).
- **Curator and embedder are swappable.** The curator is a `Curator` ABC with `async classify(post_text, similarity_signal=None, interests=...) -> Verdict | None`; the `make_curator(settings)` factory picks the impl by `CURATOR_PROVIDER`: `AnthropicCurator` (Haiku 4.5, default) or `KimiCurator` (Moonshot/Kimi, OpenAI-compatible API, `CURATOR_PROVIDER=kimi`). There's also a commented-out `DeepSeekCurator` sketch as a third example. The curator is **/focus-aware**: the pipeline passes the owner's active topics in `interests=`, loosening the bar for them. The embedder is a thin wrapper over Voyage. The pipeline reads the model name via `curator_model_of()` / `getattr(embedder, "_model", ...)` precisely to stay decoupled.
- **Multi-tenant by `telegram_user_id`.** `users.telegram_user_id` is UNIQUE; everything is scoped by the `user_id` resolved with `get_or_create_user`. `posts` is a **shared pool**, curated **once** (quality, user-agnostic) → adding people does **not** multiply the curation cost. Each person's **taste** lives in `votes`, `deliveries`, and `focus`. Affinity is separated **per bucket** (repos vs news).
- **Idempotent steps.** Each pipeline step only touches what's pending (`embedding IS NULL`, `verdict IS NULL`); dedup happens at upsert. A source failure is isolated and logged, it doesn't bring down the cycle.

## Security rules

- Secrets live **only** in `.env` (local) or in environment variables (in deploy). **NEVER** commit secrets in the code.
- `.env`, `config/sources.yaml`, and `config/seeds.yaml` carry personal data (the bot token, your `telegram_user_id`, interest profile) and are **gitignored**. Version-control only the `*.example.*` files.
- The maintainer's repository is **PRIVATE**. The public template is a fork without your keys or your `sources.yaml`.
- For X/Twitter, use a **throwaway account** (ban risk): only the `auth_token`/`ct0` cookies in `TWITTER_AUTH_TOKEN`/`TWITTER_CT0`.

## How to run / test

Prerequisites: `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`, then `cp .env.example .env` and `cp config/sources.example.yaml config/sources.yaml` and fill them in (see `README.md` for the step-by-step on keys).

- **Database:** apply `db/schema.sql` on Supabase — `psql "$DATABASE_URL" -f db/schema.sql` (or paste it into the SQL Editor). Use the **POOLED** connection string.
- **Cold-start (optional):** `python -m src.seed` — loads examples from `config/seeds.yaml` as preloaded votes (they don't go through the curator).
- **Pipeline alone:** `python -m src.pipeline` — runs 1 ingest→embed→curate cycle, without delivering to Telegram. Good for testing ingestion.
- **Full bot:** `python -m src.bot.bot` — brings up the bot in long-polling with the 2 embedded jobs: daily delivery at `DIGEST_HOUR` (timezone `DIGEST_TZ`) and a pipeline every 30 min. It's the `startCommand` in `Procfile`/`railway.json`. Chat commands: `/start`, `/feed` (on-demand delivery), `/run` (full cycle ingest→embed→curate→deliver on the spot), `/search <query>`, `/focus` (view/clear/steer), `/mix` (view the new×relevant balance of each bucket); free text steers the feed, adjusts the mix, does recall (a general question searches the whole archive), or queries the state ("what's in focus?", "what's the mix?"); "undo that" resets the mix to default, and pasting a link saves it to the archive.
- **MCP in Claude:** `claude mcp add archive -- .venv/bin/python -m src.mcp_server.server` (matches `.mcp.json`); to run it directly in stdio: `python -m src.mcp_server.server`.

## "I want to touch X → file Y"

- Add/adjust an ingestion source → `src/ingestion/` (`reddit_source.py`, `github_source.py`, `x_source.py`); the contract is `src/ingestion/base.py` (`IngestionSource.fetch()`).
- Cycle orchestration (ingest/embed/curate order, batches) and how /focus injects topics into ingestion (Reddit+X for news, GitHub for repos) and into the curator's `interests` → `src/pipeline.py`.
- Swap the curator (Anthropic/Kimi) or adjust the quality rubric/budget → `src/curation/curator.py` (`make_curator`, `AnthropicCurator`, `KimiCurator`, `SpendGuard`, `BudgetExceeded`); the prompt is in `src/curation/prompt.py`.
- How free chat becomes intent (steer/recall/balance/status/capacity) → `src/curation/steering.py`.
- Swap the embeddings model or the reranker (`Embedder.rerank`) → `src/common/embeddings.py`.
- Two-stage semantic search (vector recall → rerank, thresholds) → `src/common/recall.py`.
- SQL queries, database access, recall/affinity methods, already-delivered embeddings (dedup) → `src/common/db.py`.
- Data schemas (`IngestedPost`, `Verdict`, `FocusItem`, `ChatIntent`) → `src/common/models.py`.
- Environment variables and YAML parsing → `src/common/config.py` (and the templates in `.env.example`, `config/*.example.yaml`).
- Telegram commands (`/start`, `/feed`, `/run`, `/search`, `/focus`, `/mix`), card formatting, votes, 2-bucket delivery ranking with focus quota + adjustable digest size, freshness cutoff (30d), repeated-news dedup (`_dedup_pending`), auto-balancing, chat recall (a general question searches the archive via `semantic_recall`; "undo that" resets the mix), chat state queries + resize (`_do_status`/`_apply_capacity`), digest time (`DIGEST_HOUR`) and the two jobs → `src/bot/bot.py`.
- Tools exposed to Claude (`search_archive`, `recall_votes`, `see_focus`) → `src/mcp_server/server.py`.
- Tables, indexes (HNSW), triggers, retention policy → `db/schema.sql`.
- Sources per user / cold-start → `config/sources.yaml` and `config/seeds.yaml`.
- Deploy (Railway, always-on, restart) → `Procfile` and `railway.json`.
