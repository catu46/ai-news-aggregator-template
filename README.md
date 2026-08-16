# 🤖 AI News Aggregator

A personal AI news and repo aggregator, **multi-tenant-ready**: it collects GitHub/Reddit/X, curates quality with a **swappable LLM curator (DeepSeek by default)**, delivers **1x/day on Telegram** in 2 buckets (📦 repos / 🗞️ news) with 👍/👎 votes — and even exposes your curated archive to Claude via **MCP**.

> **Open-source template.** Each person spins up their own copy: their bot, their database, their data. Nothing is shared between instances.

> **Not just AI.** It ships configured for AI/tech, but the **engine is topic-agnostic**. Point `config/sources.yaml` at any subreddits / X searches you want (World Cup, a niche market, a sports league…), add your own `gold`/`noise` examples to `seeds.yaml`, and rewrite the curator prompt (`src/curation/prompt.py`) to judge **your** topic's quality. Semantic search, dedup, steering and delivery work regardless of domain. *(The 📦 repos bucket is GitHub-specific — drop it or repurpose the source if your topic isn't code.)*

---

## ✨ Features

- **📦 / 🗞️ Morning digest 1x/day.** The digest arrives **once a day, at a fixed time** (`DIGEST_HOUR`/`DIGEST_TZ`, via `run_daily`) — a little morning "newspaper," not an "every-24h" blast. It comes split into **repos** (GitHub) and **news** (Reddit + X), each ranked within itself — no mixing apples and oranges. Delivery only includes posts **discovered** in the last **7 days** (`DELIVERY_MAX_AGE_DAYS`) — measured by ingestion date, not the source's publish date, so a freshly-found repo counts even if the repo itself is old.
- **🏃 `/run` on demand.** Runs a **full cycle now** — ingest → embed → curate → deliver — without waiting for the digest time. It has a lock so two cycles don't run at once.
- **🧠 Curation via a swappable LLM.** An **APPLIED-AI** persona (tools, capabilities, techniques, useful news for someone who USES AI — enemy #1 is **AI Slop**). Each post gets a **global quality** verdict (approve/reject + category + summary + rationale) via structured output, with the rubric cached to keep it cheap. Approve categories: `ai_tools`/`ai_capabilities`/`applied_techniques`/`autonomous_agents`/`ai_industry`; rejects: `ai_slop`/`low_signal`/`research_only`/`corporate_hype`/`basic_tutorial`/`off_topic`. **Concrete substance survives a hype tone** (a number/release/event approves; rumor/leak/hot-take is `ai_slop`). The **card summary comes out in English**. Active `/focus` topics loosen the bar. **Swappable provider** via `CURATOR_PROVIDER` — **`deepseek` (default, cheapest, ~10x less than Haiku with automatic prompt caching)**, `anthropic` (Claude Haiku 4.5) or `kimi` (Moonshot). `ANTHROPIC_API_KEY` is **always required** even on the DeepSeek/Kimi path — the steerer (chat intent) stays on Anthropic regardless of the curator provider.
- **💰 Two-layer cost control.** A `SpendGuard` (accurate cost estimate, with the cache discount) pauses curation once the **monthly** cap (`CURATOR_MONTHLY_BUDGET_USD`) is hit — persisted in the DB `meta` table so it survives Railway's ephemeral disk (a local-file-only counter would reset on every deploy and never actually fire). A second, independent **per-cycle** cap (`CURATION_MAX_PER_CYCLE`, default 150) keeps a big backlog (e.g. after 24h of broken ingestion) from being judged all at once — it dilutes across several ~30min cycles instead. The first time in a month the budget pauses curation, the bot **DMs every authorized user on Telegram** (at most once a month) so it never fails silently.
- **👍 / 👎 with PER-BUCKET affinity.** Your votes train the ranking, and affinity is **separated per bucket**: what you like in repos doesn't interfere with what shows up in news. Affinity only **ranks**, it never hides.
- **🎯 `/focus` by speech.** Say in natural language "I want more about agents" and the focus becomes **bidirectional**: it re-ranks delivery **and** injects the topic into ingestion, pulling in new content on that subject — broadening the search on **X** (Latest + Top), on **Reddit** (top of the day/week/month + hot), and on **GitHub**. And more: the **curator now listens to the focus** — active topics loosen the quality bar (approving on-topic content, including funding/VC) instead of just reordering what already exists.
- **🔎 Conversational recall & `/search`.** Chat recall distinguishes **the polarity of your question**: if you ask about what you **voted** on ("did I like something about XPTO?"), the bot searches your 👍/👎; but a **general** question ("any news about XPTO?") searches the **whole archive** — anything good that passed curation, whether you voted on it or not, with ❤️ marking what you liked. `/search` does semantic search and, since the archive is embedded in English, it **translates the query** (`translate_to_en`) before searching — so you can ask in PT-BR.
- **🎯 Hybrid search (vector + full-text) with relevance rerank.** In a single-domain archive (all AI), cosine distance alone can't separate relevant from irrelevant. So search has **two stages** (`semantic_recall`): broad **candidate recall** → a **reranker** (Voyage `rerank-2.5`) that reads query+text together and gives the real relevance score. Candidate recall is itself **hybrid**: vector (embedding) + full-text search (Postgres `websearch_to_tsquery`), fused with **RRF** (`db.hybrid_recall`) — catches exact keywords/names the embedding alone can miss. Requires the `db/migrations/2026-07-22-hybrid-fts.sql` migration; until you apply it, the code **falls back automatically** to pure vector recall (`search_pool`), so it's safe to deploy first and migrate later. The reranker query also carries a **taste instruction** (`RERANK_PROFILE`, see below) — off-topic is **discarded**; if nothing passes, the bot says **"I found nothing about X"** instead of filling the screen with off-topic. Applies to chat, `/search` and MCP.
- **🎛️ `RERANK_PROFILE` — tell the reranker your taste.** rerank-2.5 is instruction-following: the search's reranker (above) can be steered by a couple of sentences of natural-language taste ("prefers hands-on, immediately-useful developer content; downrank hype") appended to every query. The template ships a **generic placeholder** — write your own in `.env` (`RERANK_PROFILE`) once you know what you like.
- **🚀 `QUERY_EMBEDDING_MODEL` — a bigger encoder just for the question.** The `voyage-4` family (nano/lite/voyage-4/large) shares the **same vector space** across sizes, so the archive can stay on the cheap `voyage-4-lite` while the (much rarer) search queries use a larger, higher-quality encoder — no re-embedding the whole archive. Off by default (same model as the archive); set `QUERY_EMBEDDING_MODEL=voyage-4-large` to turn it on.
- **♻️ No repeated news.** The bot won't send you the **same story twice** — even from another source or on another day (and even if you liked it). Before delivering, it runs **semantic dedup** (`_dedup_pending`) against everything already delivered; distinct stories still come through. Repos **and** news.
- **🎚️ Focus quota + digest size.** A `/focus` is a **dial, not a switch**: it occupies up to **N** of the bucket's slots ("up to 6 VC news"), the rest stays normal — so one topic never starves a platform. If you don't give a number, the bot **asks** "how many?". And you can **resize the digest** by speech ("up to 20 news a day").
- **⚖️ New × relevant rebalancing.** Adjust by speech how much of the digest is **freshness** (newer) vs **relevance** (affinity + focus) — and, beyond the manual adjustment, the bot **auto-balances** by learning from your votes (it raises novelty if you like what's discovered by the freshness slot, lowers it if you reject). It's saved in your settings. Say **"undo that" / "reset"** and the bot **zeroes out the adjustment** for that bucket (or both) and the mix returns to default. **`/mix`** shows the current new×relevant balance of each bucket (marked `default` or `adjusted`).
- **🔌 MCP server.** Plug your curated archive into Claude Code/Desktop and query it with `search_archive`, `recall_votes`, and `see_focus`.
- **🔗 Pasted link = 👍, with native readers.** Paste a URL in the chat: the bot reads the content and saves it to your archive as `origin='manual'` with a positive vote, and that vote **counts toward your ranking affinity** too (per-bucket, same as a 👍 on a card). Most links go through **Jina Reader**, but two platforms get a dedicated reader instead: **x.com/twitter.com** links are read via **twitter-cli** (Jina 403s on that domain) and **github.com** links are read via the **GitHub REST API** (description + topics + stars + README), falling back to Jina if that fails. If a link's read fails outright, it isn't dropped: it enters a **retry queue** (`needs_fetch` in `posts.metadata`) that the pipeline job (and `/run`) retries automatically — you get a Telegram message once it succeeds.

---

## 💬 Commands

All commands respond only to the allowlist in `sources.yaml`:

| Command | What it does |
| --- | --- |
| `/start` | Welcome + registers you in the database; lists the commands. |
| `/feed` | Delivers **now** whatever is approved-and-undelivered (doesn't touch the mix). |
| `/run` | Runs a **full cycle** now: ingest → embed → curate → deliver (with a lock so two don't run at once). |
| `/search <query>` | Semantic search on the curated archive (translates PT→EN first; ❤️ = you liked it). |
| `/focus` | No argument: shows the active direction per bucket. `/focus clear` (or `off`/`reset`): clears it. `/focus <text>`: steers (same path as free chat). |
| `/mix` | Shows the current **new×relevant balance** of each bucket (marked `default` or `adjusted`). |

Beyond the commands, **just talk normally** to the bot: steer the feed ("for 3 days I want repos about RAG"), ask ("any news about agents?" → searches the whole archive; "what was that news I liked?" → searches your votes), query the state ("what's in focus?", "what's the mix?"), adjust the mix ("more novelty in the news") or reset it ("undo that" / "reset") — or paste a link to save it to the archive.

---

## 🏗️ Architecture

Everything runs **inside the bot itself**: two jobs on the `JobQueue` (delivery **1x/day** at a fixed time via `run_daily` at `DIGEST_HOUR`/`DIGEST_TZ`, pipeline every **30min**) — and `/run` forces a full cycle (ingest → embed → curate → deliver) any time. **No separate cron needed.**

```
                config/sources.yaml  +  active /focus (broadens ingestion)
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
  RedditSource                GitHubSource                  XSource
  (fixed subs +          (Search API + README,         (twitter-cli/cookies:
   top day/week/month+hot)  + focus queries)           Latest + Top of focus)
        └───────────────────────────┼───────────────────────────┘
                                    ▼
                      INGEST  →  upsert_post (dedup)
                                    │
                       SHARED POOL: posts  ◀── curated 1x (quality)
                                    │
                  ┌─────────────────┼─────────────────┐
                  ▼                                   ▼
        EMBED (Voyage voyage-4-lite)       CURATE (curator → Verdict, English summary)
        embedding IS NULL, batches of 100  verdict IS NULL, batches of 100
                  │                        ▲ LISTENS to /focus (interests loosen the bar)
                  └─────────────────┬─────────────────┘  SpendGuard pauses $$
                                    ▼
              DELIVERY IN 2 BUCKETS  (daily digest · /feed · /run)
              approved_undelivered (≤ 7 days by ingested_at)  →  ranks WITHIN the bucket:
                  📦 repos  = (github)
                  🗞️ news   = (reddit, twitter)
              slots split:  RELEVANCE (affinity+focus)  ×  FRESHNESS
                            │  (governed by BALANCE, auto-tuned from votes)
                                    ▼
              Telegram: cards with 👍/👎  →  on_vote writes to votes
                                    │
            ┌───────────────────────┼───────────────────────┐
            ▼                       ▼                       ▼
       AFFINITY                   FOCO                   BALANCE
   (ranks, per bucket)    (ingest + curate + rank)  (new×relevant, learns from votes)
            └───────────────────────┼───────────────────────┘
                                    ▼
                 RECALL / MCP  →  semantic_recall (broad vector recall → RERANK) · active_focus
                    (/search, chat, and the MCP server use the SAME methods)
              ▸ GENERAL question → whole archive; "liked X?" → votes
              ▸ RERANK relevance cut: off-topic is discarded → "I found nothing about X"

                       ▸ EVERYTHING scoped by user_id ◂
```

### Components

| Component | Path | Role |
| --- | --- | --- |
| **Telegram bot** | `src/bot/bot.py` | Delivery and interface (python-telegram-bot 22.8, long-polling). Locked to an allowlist from `sources.yaml`, delivers the morning digest in 2 buckets (30-day cutoff), records inline votes, commands `/start /feed /run /search /focus /mix`, saves pasted links (native readers for x.com/github.com, retry queue for failed reads) and routes free chat to steer/recall/balance/status (general recall falls back to the whole archive; "undo that"/"reset" zeroes the mix; "what's in focus?"/"what's the mix?" query the state). Runs the `_job_deliver` (daily, fixed hour via `run_daily`, with auto-balancing) and `_job_pipeline` (30min, also drains the link retry queue) jobs. |
| **Pipeline runner** | `src/pipeline.py` | One `ingest → embed → curate` cycle, idempotent. Runs standalone (`python -m src.pipeline`), via the bot's job, or via `/run`. The active `/focus` topics enter ingestion (Reddit/X/GitHub) **and** curation (as *interests*). It does **not** deliver to Telegram. |
| **Reddit source** | `src/ingestion/reddit_source.py` | Collects via the **public RSS/Atom feed** of the fixed subreddits; `/focus` (news) topics broaden the search (top of day/week/month + hot). Parses with feedparser + BeautifulSoup. |
| **GitHub source** | `src/ingestion/github_source.py` | Trending repos by topic via the Search API (recent + `stars>=min`, ordered by stars) and reads the README best-effort; `/focus` (repos) topics enter as extra queries. `GITHUB_TOKEN` optional. Also exports `fetch_repo(url)`, used by the bot to read a single pasted github.com link via the same REST API. |
| **X/Twitter source** | `src/ingestion/x_source.py` | Collects via subprocess of the `twitter` CLI (free mode via cookies): `user-posts` and `search`, both `--json`. `/focus` (news) topics broaden the search (Latest + Top). Also exports `fetch_tweet(url)`, used by the bot to read a single pasted x.com/twitter.com link (`twitter tweet <url> --json`) since Jina Reader 403s on that domain. |
| **Source interface** | `src/ingestion/base.py` | `IngestionSource` ABC: every source implements `async fetch() -> list[IngestedPost]`. Dedup is the database's job. |
| **Curator (swappable)** | `src/curation/curator.py` | `make_curator(settings, db=db)` picks the provider by `CURATOR_PROVIDER` (`deepseek` default → `deepseek-v4-flash`, ~10x cheaper, automatic prompt caching, thinking disabled; `anthropic` → `AnthropicCurator` with Haiku 4.5; `kimi` → Moonshot/Kimi). **Global** quality verdict (Structured Outputs `Verdict`, cached rubric, English summary), with the `/focus` *interests* loosening the bar. `SpendGuard` persists monthly spend in the DB `meta` table (file fallback) and raises `BudgetExceeded`; `run_curation` also enforces a per-cycle cap (`CURATION_MAX_PER_CYCLE`). |
| **Steerer (chat→intent)** | `src/curation/steering.py` | Classifies free chat into `ChatIntent` (steer/recall/balance/status/capacity/other) via Haiku. `steer` → directives for `/focus` (with an optional `quota`); `recall` → search (polarity `any` falls back to the whole archive, `liked`/`disliked` to votes); `balance` → new×relevant mix (`balance_reset` to default); `status` → QUERIES the state (focus/mix); `capacity` → resizes a bucket's per-day cap. |
| **Config / Settings** | `src/common/config.py` | Loads `.env`, `config/sources.yaml`, and `config/seeds.yaml`. `load_settings/load_sources/load_seeds`. Also owns the `RERANK_PROFILE` default (generic placeholder — write your own) and `QUERY_EMBEDDING_MODEL` (defaults to `EMBEDDING_MODEL`). |
| **Database (pgvector)** | `src/common/db.py` | Async access (asyncpg + pgvector, `statement_cache_size=0` for the Supabase pooler). Everything scoped by `user_id`. Includes `hybrid_recall` (vector+FTS RRF fusion) and the pasted-link retry queue (`posts_needing_fetch`/`resolve_fetched_post`/`get_post_by_source`). |
| **Data models** | `src/common/models.py` | `IngestedPost` (`raw_text` is optional — `None` means "queued for retry") + Pydantic schemas for the Structured Outputs (`Verdict`, `FocusItem`, `ChatIntent`). |
| **Embedder + reranker (Voyage)** | `src/common/embeddings.py` | Voyage AI wrapper: document embeddings (`voyage-4-lite`, 1024-dim, L2-normalized → cosine=dot), a separate query embedding model (`QUERY_EMBEDDING_MODEL`, same voyage-4 vector space so it can be larger) + reranker (`rerank-2.5`, `RERANK_MODEL`, with an optional taste instruction from `RERANK_PROFILE`) for the search's 2nd stage. |
| **Hybrid two-stage search** | `src/common/recall.py` | `semantic_recall`: broad candidate recall — hybrid vector+FTS (`db.hybrid_recall`) once `db/migrations/2026-07-22-hybrid-fts.sql` is applied, else pure vector (`search_pool`) — → rerank (cut by `RERANK_MIN_SCORE`). Used by `/search`, chat and MCP. |
| **MCP server** | `src/mcp_server/server.py` | FastMCP (stdio) that exposes the archive to Claude: `search_archive`, `recall_votes`, `see_focus`. |
| **SQL schema** | `db/schema.sql` | Postgres 15+/pgvector DDL: `users`, `posts` (shared pool), `deliveries`, `votes`, `focus`. HNSW index, `updated_at` triggers. |
| **SQL migrations** | `db/migrations/` | Optional, manually-applied changes on top of `schema.sql`. Currently: `2026-07-22-hybrid-fts.sql` (adds `posts.fts` + GIN index for hybrid search) and `2026-07-23-meta-kv.sql` (generic `meta` key/value table — persists the curator's monthly spend across Railway deploys). The app works fine before you apply either — see the notes below. |
| **config/sources.yaml** | `config/sources.yaml` | Sources per user (multi-tenant). The bot's allowlist is derived from here. |

---

## 🚀 Step-by-step setup

### 1. Clone, create a venv, and install dependencies

```bash
git clone <your-fork> ai-news-aggregator
cd ai-news-aggregator
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Database (Supabase + pgvector)

1. Create a free project on [Supabase](https://supabase.com/).
2. Copy the **POOLED connection string** (the *Connection Pooler* one, not the direct one) — it becomes your `DATABASE_URL`.
3. Apply the schema:

```bash
psql "$DATABASE_URL" -f db/schema.sql
```

> Alternative: paste the contents of `db/schema.sql` into the **SQL Editor** of the Supabase dashboard. This creates the pgvector extension, the tables, the HNSW index, and the triggers.

> ⚠️ **Starting over from scratch:** `db/reset.sql` drops all the tables that `schema.sql` creates (`focus`, `votes`, `deliveries`, `posts`, `users`) — run it **before** reapplying the schema if you need to wipe it. **It ERASES all data.** Use it only on a throwaway/freshly-created database, never on one that already has your votes.

**Optional: hybrid search migration.** `db/migrations/2026-07-22-hybrid-fts.sql` adds a generated `posts.fts` column + GIN index, turning search from pure-vector into hybrid **vector + full-text** (RRF fusion, `db.hybrid_recall`). It's **not required to run the app** — until you apply it, `semantic_recall` detects the missing column and falls back to pure vector recall automatically (1 log warning, nothing breaks). Apply it whenever you're ready:

```bash
psql "$DATABASE_URL" -f db/migrations/2026-07-22-hybrid-fts.sql
```

> ⚠️ **Two Supabase gotchas for this one:**
> - It's a `GENERATED ALWAYS AS ... STORED` column, so Postgres **rewrites the whole `posts` table** at ALTER TABLE time. Fine in seconds on a small personal archive; on a large one, apply it against the **direct** (non-pooled) connection string and run `SET statement_timeout = 0;` first in that session, so the pooler's default timeout doesn't kill it mid-rewrite.

> - That's unrelated to the app's own `statement_cache_size=0` on the asyncpg pool (`src/common/db.py`) — that one exists for every regular query, to stay compatible with Supabase's pgbouncer/Supavisor **transaction-mode** pooler, and is already handled for you; you don't need to do anything about it.

**Optional but recommended: persistent spend-cap migration.** `db/migrations/2026-07-23-meta-kv.sql` creates a generic `meta` key/value table, used by the curator's `SpendGuard` to persist the monthly spend (`CURATOR_MONTHLY_BUDGET_USD`) in the **database** instead of only a local file. This matters specifically for Railway: its disk is **ephemeral** (every deploy resets it), so a file-only counter never actually protects your budget in production. It's **not required to run the app** — until applied, `SpendGuard` falls back to the local file automatically (1 log warning, nothing breaks). Apply it whenever you're ready:

```bash
psql "$DATABASE_URL" -f db/migrations/2026-07-23-meta-kv.sql
```

> Both migrations are idempotent-safe to run once each — apply every file in `db/migrations/` after `db/schema.sql` when you set up a new database (or when deploying to Railway for the first time), in addition to `schema.sql` itself.

### 3. Anthropic and Voyage keys

- **Anthropic** → `ANTHROPIC_API_KEY`. **Always required**, even though the curator defaults to DeepSeek: it powers the steerer (chat intent) regardless of `CURATOR_PROVIDER`, and it's also the curator itself if you set `CURATOR_PROVIDER=anthropic`. At [console.anthropic.com](https://console.anthropic.com/). The **curator is swappable** via `CURATOR_PROVIDER` — **`deepseek`** (default, shipped in `.env.example`; fill in `DEEPSEEK_API_KEY` at [platform.deepseek.com](https://platform.deepseek.com/), the cheapest option for classification), `anthropic` (Haiku 4.5, uses the key above), or `kimi` (fill in `MOONSHOT_API_KEY`/`MOONSHOT_BASE_URL`/`KIMI_MODEL`).
- **Voyage AI** → `VOYAGE_API_KEY` (embeddings). At [voyageai.com](https://www.voyageai.com/). Generous free tier.

### 4. Telegram bot

1. Talk to [@BotFather](https://t.me/BotFather), `/newbot`, and copy the token → `TELEGRAM_BOT_TOKEN`.
2. Find **your** numeric `user_id`: talk to [@userinfobot](https://t.me/userinfobot) (or send `/start` to your bot, which logs the id).

### 5. (Optional) X / Twitter cookies

The X source uses `twitter-cli` in **free mode via cookies**. Use a **throwaway account** (risk of banning your main one). Extract 2 cookies from a session logged into `x.com` (DevTools or the Cookie-Editor extension):

| Cookie | Variable |
| --- | --- |
| `auth_token` | `TWITTER_AUTH_TOKEN` |
| `ct0` | `TWITTER_CT0` |

> The cookies expire over time — re-extract them when X ingestion stops. Without them, the X source simply runs without auth. The same cookies also power reading a **pasted x.com/twitter.com link** (`fetch_tweet`) — Jina Reader 403s on that domain, so without cookies that specific paste-a-link path fails (and queues for retry) even though the rest of the bot works fine.

> `GITHUB_TOKEN` (used for both ingestion and reading a pasted github.com link) only needs **public read access** — a "fine-grained" token at [github.com/settings/tokens](https://github.com/settings/tokens) with no repository access/scopes checked is enough. It's optional either way (10 req/min unauthenticated vs. 30 authenticated for search).

### 6. Configure sources, seeds, and environment variables

The flow is always **`*.example` → real file**: the repo version-controls the `*.example` templates (placeholders only) and you create the real files next to them. Copy the three and fill them in:

```bash
cp config/sources.example.yaml config/sources.yaml
cp config/seeds.example.yaml   config/seeds.yaml
cp .env.example .env
```

Edit `config/sources.yaml` with **your** `telegram_user_id` and the sources you follow. The `owner` block is you; uncomment a second block to add another person:

```yaml
users:
  owner:
    telegram_user_id: 0            # <-- your numeric Telegram id
    display_name: "you"

    reddit:
      subreddits:                  # without the "r/"
        - LocalLLaMA
        - MachineLearning

    x:
      accounts:                    # without the "@"
        - example_handle
      searches:                    # X operators: from:, OR, min_faves:, lang:, -filter:
        - '(agentic OR "autonomous agents") min_faves:50 lang:en -filter:replies'

    github:
      queries:                     # trending repos by topic (Search API)
        - "AI agents"
        - "MCP server"
```

Then fill in `.env` (the keys from steps 3–5 + the `DATABASE_URL` from step 2):

```dotenv
# Curator provider: deepseek (default, cheapest) | anthropic | kimi
CURATOR_PROVIDER=deepseek
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
# Required regardless of CURATOR_PROVIDER — powers the steerer (chat intent):
ANTHROPIC_API_KEY=
CURATOR_MODEL=claude-haiku-4-5
CURATOR_MONTHLY_BUDGET_USD=8
# Per-cycle cap: a big backlog dilutes across cycles instead of one big bill
CURATION_MAX_PER_CYCLE=150
# Only if CURATOR_PROVIDER=kimi (OpenAI-compatible API):
MOONSHOT_API_KEY=
MOONSHOT_BASE_URL=https://api.moonshot.ai/v1
KIMI_MODEL=kimi-k2.6
VOYAGE_API_KEY=
EMBEDDING_MODEL=voyage-4-lite
# Optional: bigger encoder for the search QUESTION only (same voyage-4 vector space)
QUERY_EMBEDDING_MODEL=
RERANK_MODEL=rerank-2.5
# Optional: your taste, in a sentence or two — steers the archive-search reranker
RERANK_PROFILE=
REDDIT_USER_AGENT=ai-news-aggregator/1.0 (personal, non-commercial)
GITHUB_TOKEN=
TWITTER_AUTH_TOKEN=
TWITTER_CT0=
TELEGRAM_BOT_TOKEN=
TELEGRAM_USER_ID=
# Morning digest: fixed time of the daily delivery (run_daily)
DIGEST_HOUR=7                 # 0-23, local hour
DIGEST_TZ=America/Sao_Paulo   # IANA timezone
DATABASE_URL=
```

Also edit `config/seeds.yaml` with your taste examples (`gold` = what you want to receive, `noise` = what should be rejected). They feed the cold-start in step 7 — 3–5 of each is enough.

> **Config vs deploy — be honest about the trade-off.**
> - **Secrets (`.env`)** carry credentials and are **never** version-controlled — they're already in `.gitignore`. Always inject them via *environment variables* on the host (Railway), never via a file in git.
> - **`config/sources.yaml` and `config/seeds.yaml`** carry *personal* data (your `telegram_user_id`, your interest profile), **not** credentials. In a **clean clone/fork that's going to become public**, the recommendation is to add them to `.gitignore` so you don't accidentally commit personal data.
> - **However**, the Railway deploy is done **via git**: the service builds from what's committed in **your private deploy repository**. So, for the bot to find these configs in production, `config/sources.yaml` (and `seeds.yaml`, if you're going to run the seed there) **must be committed in that private repo** — normally (if it's not in `.gitignore`) or via `git add -f` (if it is). The alternative is to not use a file and provide the configuration via environment variables.
> - In short: there's **no such thing** as "`sources.yaml` is never committed." In a **private** deploy repo it's typically committed on purpose; what you avoid is leaking it in a **public** fork.

### 7. Cold-start (seed) and run locally

Before the first cycle, run the **seed** once to give the system a signal on day 1:

```bash
python -m src.seed
```

It reads `config/seeds.yaml`, resolves each user by the `telegram_user_id` from `sources.yaml`, and loads the examples as **preloaded votes** (`gold` → 👍, `noise` → 👎, `origin='seed'`), embedding each one. With this, `/search` and recall already **work on day 1**, before any real ingestion. It depends on **`DATABASE_URL`** and **`VOYAGE_API_KEY`** already configured (steps 2 and 3) and the schema already applied. It's idempotent: re-running doesn't duplicate (deterministic source_id). With no seeds filled in, it does nothing.

> Note: the seed writes posts with `source_platform='seed'` — a value already accepted by the `CHECK` on `posts.source_platform` in `db/schema.sql` (alongside `github`/`manual`), nothing to configure.

Now bring up the bot:

```bash
python -m src.bot.bot
```

Brings up the bot in long-polling. The **delivery** job (daily, at the fixed time of `DIGEST_HOUR`/`DIGEST_TZ`, via `run_daily`) and the ingest→embed→curate **pipeline** job (30min) run inside it. To force a full cycle any time, send **`/run`** in the chat (ingest → embed → curate → deliver). To run just the pipeline manually, without the bot and without delivering:

```bash
python -m src.pipeline
```

### 8. Deploy on Railway

The project already comes with a `Procfile` and `railway.json` (NIXPACKS, `restartPolicyType: ALWAYS`). It's an **always-on** process, so it **needs no separate cron** — the jobs live inside the bot.

- **Start command:** `python -m src.bot.bot`
- Configure the same variables from `.env` (steps 2–5) as *environment variables* in the Railway dashboard. **Never** commit `.env`.
- Railway builds **from git**: `config/sources.yaml` (and `seeds.yaml`, if you're going to run the seed there) must be **committed in your private deploy repository** — a normal commit, or `git add -f` if you've gitignored those configs (see the note in step 6).
- Make sure `db/schema.sql` **and** every file in `db/migrations/` have been applied to your Supabase database (step 2) — they're idempotent-safe to run once each, and Railway's ephemeral disk is exactly why the `2026-07-23-meta-kv.sql` migration matters for the spend cap to actually persist in production.

📖 **Full step-by-step in [`DEPLOY.md`](DEPLOY.md)**: bringing up the **single service** `bot` (always-on, with pipeline + delivery via the internal JobQueue), environment variables, the one-off seed, and validation.

> Note: datacenter IP traffic (Railway) may be throttled by the Reddit feed.

### 9. Plug into Claude (MCP)

```bash
claude mcp add archive -- .venv/bin/python -m src.mcp_server.server
```

The command matches `.mcp.json` and exposes `search_archive`, `recall_votes`, and `see_focus` over your curated archive. To run the server directly in stdio:

```bash
python -m src.mcp_server.server
```

---

## 💰 Cost

Runs comfortably at **~$0–10/month**, leaning on free tiers:

| Service | Plan | Note |
| --- | --- | --- |
| **Supabase** | Free (Postgres + pgvector) | ~500MB; the schema has a retention note pruning `raw_text` from old rejected items. |
| **Voyage AI** | Free tier | `voyage-4-lite`, generous free tier. |
| **Curator (DeepSeek, Haiku 4.5, or Kimi)** | Paid, with a cap | Provider swappable via `CURATOR_PROVIDER` (`deepseek` default, ≈ a tenth of Haiku's cost | `anthropic` | `kimi`). Two-layer cost control: a **monthly** `SpendGuard` (`CURATOR_MONTHLY_BUDGET_USD`, default **$8**, persisted in the DB `meta` table) + a **per-cycle** cap (`CURATION_MAX_PER_CYCLE`, default 150), plus prompt caching of the rubric. Curation **pauses** when the monthly cap is exceeded, and the bot **alerts you on Telegram** the first time that happens each month. |
| **GitHub / Reddit / X** | Free | Public Search API, RSS feed, and twitter-cli via cookie. |
| **Railway** | Usage-based | Always-on process. |

Curation is the only variable cost — and it's **limited by design**.

---

## 👥 Multi-tenant

Everything is scoped by `user_id`, derived from `telegram_user_id` (UNIQUE in `users`). The bot is locked to an **allowlist** built from `sources.yaml`; each update is resolved via `get_or_create_user`.

- **`posts` is a SHARED POOL**, curated **once** (quality verdict, user-agnostic) → adding people does **not** multiply the curation cost.
- Each person's **taste** lives in `votes`, `deliveries`, and `focus` per user.
- **Privacy** guaranteed by the `user_id` filter on all recall/search queries.

As a template, the recommended path is **each person spins up their own instance** (their bot, their database) — but the code already supports multiple users in the same deploy, just add blocks to `config/sources.yaml`.

---

## 🔌 Query via Claude (MCP)

After `claude mcp add archive`, Claude starts seeing your curated archive through these tools (thin shells over `Database`, resolving "you" by the `TELEGRAM_USER_ID` from `.env` or by the 1st user in `sources.yaml`):

| Tool | What it does |
| --- | --- |
| `search_archive` | Semantic search (2 stages: hybrid vector+FTS recall → rerank) over the pool of curated posts. |
| `recall_votes` | Recall in your 👍/👎 — "what have I already liked about X?" (same 2-stage search). |
| `see_focus` | Shows the active `/focus` per bucket (`active_focus`). |

These are the **same methods** that `/search` and chat (recall) use on Telegram — just now inside Claude.

---

## 📂 Repository structure

```
src/
  bot/bot.py              # Telegram interface + jobs (daily digest run_daily, pipeline 30min, /run)
  pipeline.py             # 1 ingest→embed→curate cycle (idempotent, /focus enters ingestion and curation)
  seed.py                 # cold-start: loads seeds.yaml as votes (python -m src.seed)
  ingestion/
    base.py               # IngestionSource ABC
    reddit_source.py      # fixed subs + focus search (top day/week/month + hot)
    github_source.py      # Search API + README (+ focus queries); fetch_repo() for pasted links
    x_source.py           # twitter-cli via cookies (Latest + Top of focus); fetch_tweet() for pasted links
  curation/
    curator.py            # swappable curator (CURATOR_PROVIDER, deepseek default) + SpendGuard (Verdict, English summary)
    steering.py           # chat → ChatIntent (steer/recall/balance/status/capacity) + translate_to_en for /search
  common/
    config.py             # .env + sources.yaml + seeds.yaml (incl. RERANK_PROFILE, QUERY_EMBEDDING_MODEL)
    db.py                 # asyncpg + pgvector (scoped by user_id); hybrid_recall + link retry queue
    models.py             # IngestedPost (raw_text optional) + Pydantic schemas
    embeddings.py         # Voyage: doc embeddings, query embeddings, profile-aware reranker
    recall.py             # 2-stage search: hybrid (vector+FTS) or vector-only recall -> rerank
  mcp_server/server.py    # FastMCP (search_archive / recall_votes / see_focus)
db/schema.sql             # Postgres 15+/pgvector DDL
db/reset.sql              # drops the schema's tables (ERASES data)
db/migrations/            # optional, manually-applied changes on top of schema.sql
config/sources.yaml       # sources per user (personal data, not a secret)
config/seeds.yaml         # cold-start examples per user (personal data)
Procfile · railway.json   # Railway deploy (via git)
DEPLOY.md                 # step-by-step deploy guide
```

---

## 🗺️ Next steps

- **`/calibrate` — interactive cold-start for the ranking.** Today the popularity/recency weights are **learned from your votes** over several days (daily EMA). The idea: a `/calibrate` command where the bot shows you 4-5 **real, varied** items (viral/niche × fresh/old × different topics), you react, and it **seeds on the spot** the `pop`/`recency` multipliers (`set_weight_prefs`) + affinity (👍/👎 on the ones you pick) — instead of waiting for the EMA to converge. Keeps **semantic dominant** (the `POP_REC_CAP` cap); it only speeds up bootstrapping your taste.

---

## 📜 License

Open-source template — fork it, spin up your copy, tweak the sources, and have fun. Always keep your **keys** (`.env`) out of version control. `config/sources.yaml`/`config/seeds.yaml` carry personal data (not credentials): in **public** forks, gitignore them; in your **private** deploy repo, they typically need to be committed for Railway to build (see step 6 and `DEPLOY.md`).
