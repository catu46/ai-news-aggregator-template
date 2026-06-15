# 🤖 AI News Aggregator

A personal AI news and repo aggregator, **multi-tenant-ready**: it collects GitHub/Reddit/X, curates quality with **Claude Haiku 4.5**, delivers **once a day on Telegram** in 2 buckets (📦 repos / 🗞️ news) with 👍/👎 votes — and even exposes your curated archive to Claude via **MCP**.

> **Open-source template.** Each person spins up their own copy: your bot, your database, your data. Nothing is shared between instances.

---

## ✨ Features

- **📦 / 🗞️ Two daily buckets.** The digest arrives split into **repos** (GitHub) and **news** (Reddit + X), each ranked within itself — no mixing apples with oranges.
- **🧠 Curation via Claude Haiku 4.5.** Each post gets a **global quality** verdict (approve/reject + category + summary + rationale) via Structured Outputs, with the rubric cached to keep it cheap. A `SpendGuard` gracefully pauses curation when the monthly spend cap is hit.
- **👍 / 👎 with PER-BUCKET affinity.** Your votes train the ranking, and affinity is **separate per bucket**: what you like in repos doesn't affect what shows up in news. Affinity only **ranks**, it never hides.
- **🎯 `/foco` by speech.** Say in natural language "I want more about agents" and focus becomes **bidirectional**: it re-ranks delivery **and** injects the topic into collection, pulling in new content on that subject.
- **🔎 Conversational recall.** Ask in chat "did I like something about XPTO?" and the bot searches your vote archive semantically.
- **⚖️ New × relevant rebalancing.** Adjust by speech how much of the digest is **freshness** (newer) vs **relevance** (affinity + focus). It's saved in your settings.
- **🔌 MCP server.** Plug your curated archive into Claude Code/Desktop and query it with `buscar_acervo`, `lembrar_votos`, and `ver_foco`.
- **🔗 Pasted link = 👍.** Paste a URL into the chat: the bot reads the content via Jina Reader and saves it to your archive already as `origin='manual'` with a positive vote.

---

## 🏗️ Architecture

Everything runs **inside the bot itself**: two jobs on the `JobQueue` (delivery **once a day** at a fixed time via `DIGEST_HOUR`, pipeline every **30min**). **No separate cron needed.**

```
                       config/sources.yaml (sources per user)
                                    │
        ┌───────────────────────────┼───────────────────────────┐
        ▼                           ▼                           ▼
  RedditSource                GitHubSource                  XSource
  (feed .rss)            (Search API + README)         (twitter-cli/cookies)
        └───────────────────────────┼───────────────────────────┘
                                    ▼
                      INGEST  →  upsert_post (dedup)
                                    │
                       SHARED POOL: posts  ◀── curated once (quality)
                                    │
                  ┌─────────────────┼─────────────────┐
                  ▼                                   ▼
        EMBED (Voyage voyage-4-lite)       CURATE (Haiku 4.5 → Verdict)
        embedding IS NULL, batches 100     verdict IS NULL, batches 100
                  └─────────────────┬─────────────────┘  SpendGuard pauses $$
                                    ▼
              DELIVERY IN 2 BUCKETS  (daily job or /feed)
              approved_undelivered  →  ranks WITHIN each bucket:
                  📦 repos  = (github)
                  🗞️ news   = (reddit, twitter)
              slots split:  RELEVANCE (affinity+focus)  ×  FRESHNESS
                                    │  (governed by BALANCE)
                                    ▼
              Telegram: cards with 👍/👎  →  on_vote writes to votes
                                    │
            ┌───────────────────────┼───────────────────────┐
            ▼                       ▼                       ▼
       AFFINITY                   FOCUS                  BALANCE
   (ranks, per bucket)    (ranks + pulls collection)  (mixes new×relevant)
            └───────────────────────┼───────────────────────┘
                                    ▼
                 RECALL / MCP  →  search_pool · recall_voted · active_focus
                    (/buscar, chat, and the MCP server use the SAME methods)

                       ▸ EVERYTHING scoped by user_id ◂
```

### Components

| Component | Path | Role |
| --- | --- | --- |
| **Telegram bot** | `src/bot/bot.py` | Delivery and interface (python-telegram-bot 22.8, long-polling). Locks to a `sources.yaml` allowlist, delivers the digest in 2 buckets, records inline votes, `/start /feed /buscar /foco` commands, saves pasted links, and routes free chat to steer/recall/balance. Runs the `_job_deliver` (daily, fixed hour) and `_job_pipeline` (30min) jobs. |
| **Pipeline runner** | `src/pipeline.py` | One `ingest → embed → curate` cycle, idempotent. Runs standalone (`python -m src.pipeline`) or via the bot's job. It does **not** deliver to Telegram. |
| **Reddit source** | `src/ingestion/reddit_source.py` | Collects via the **public RSS/Atom feed** (`/r/<sub1+sub2+...>/new/.rss`) — the `.json` endpoint started returning 403. Parses with feedparser + BeautifulSoup. |
| **GitHub source** | `src/ingestion/github_source.py` | Trending repos by topic via the Search API (recent + `stars>=min`, ordered by stars) and reads the README best-effort. `GITHUB_TOKEN` optional (60→5000 req/h). |
| **X/Twitter source** | `src/ingestion/x_source.py` | Collects via a subprocess of the `twitter` CLI (free mode via cookies): `user-posts` and `search -t Latest`, both `--json`. |
| **Source interface** | `src/ingestion/base.py` | `IngestionSource` ABC: every source implements `async fetch() -> list[IngestedPost]`. Dedup is the database's job. |
| **Curator (Anthropic)** | `src/curation/curator.py` | `AnthropicCurator` (Haiku 4.5, `Verdict` Structured Outputs, cached rubric). **Global** quality verdict. `SpendGuard` persists spend and raises `BudgetExceeded`. A `DeepSeekCurator` sketch shows how to swap providers. |
| **Steerer (chat→intent)** | `src/curation/steering.py` | Classifies free chat into `ChatIntent` (steer/recall/balance/other) via Haiku. `steer` → directives for `/foco`; `recall` → searches the votes; `balance` → mixes new×relevant. |
| **Config / Settings** | `src/common/config.py` | Loads `.env`, `config/sources.yaml`, and `config/seeds.yaml`. `load_settings/load_sources/load_seeds`. |
| **Database (pgvector)** | `src/common/db.py` | Async access (asyncpg + pgvector, `statement_cache_size=0` for the Supabase pooler). Everything scoped by `user_id`. |
| **Data models** | `src/common/models.py` | `IngestedPost` + the Structured Outputs' Pydantic schemas (`Verdict`, `FocusItem`, `ChatIntent`). |
| **Embedder (Voyage)** | `src/common/embeddings.py` | Voyage AI wrapper (`voyage-4-lite`, 1024-dim, L2-normalized → cosine=dot). |
| **MCP server** | `src/mcp_server/server.py` | FastMCP (stdio) that exposes the archive to Claude: `buscar_acervo`, `lembrar_votos`, `ver_foco`. |
| **SQL schema** | `db/schema.sql` | Postgres 15+/pgvector DDL: `users`, `posts` (shared pool), `deliveries`, `votes`, `focus`. HNSW index, `updated_at` triggers. |
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

> Alternative: paste the contents of `db/schema.sql` into the **SQL Editor** in the Supabase dashboard. This creates the pgvector extension, the tables, the HNSW index, and the triggers.

> ⚠️ **Starting over from scratch:** `db/reset.sql` drops all the tables that `schema.sql` creates (`focus`, `votes`, `deliveries`, `posts`, `users`) — run it **before** reapplying the schema if you need to wipe it. **It DELETES all data.** Use it only on a disposable/freshly-created database, never on one that already has your votes.

### 3. Anthropic and Voyage keys

- **Anthropic** → `ANTHROPIC_API_KEY` (Haiku 4.5 curator + steerer). At [console.anthropic.com](https://console.anthropic.com/).
- **Voyage AI** → `VOYAGE_API_KEY` (embeddings). At [voyageai.com](https://www.voyageai.com/). Generous free tier.

### 4. Telegram bot

1. Talk to [@BotFather](https://t.me/BotFather), `/newbot`, and copy the token → `TELEGRAM_BOT_TOKEN`.
2. Find out **your** numeric `user_id`: talk to [@userinfobot](https://t.me/userinfobot) (or send `/start` to your bot, which logs the id).

### 5. (Optional) X / Twitter cookies

The X source uses `twitter-cli` in **free mode via cookies**. Use a **throwaway account** (risk of banning your main one). Extract 2 cookies from a logged-in session on `x.com` (DevTools or the Cookie-Editor extension):

| Cookie | Variable |
| --- | --- |
| `auth_token` | `TWITTER_AUTH_TOKEN` |
| `ct0` | `TWITTER_CT0` |

> Cookies expire over time — re-extract them when X collection stops. Without them, the X source simply runs without auth.

### 6. Configure sources, seeds, and environment variables

The flow is always **`*.example` → real file**: the repo versions the `*.example` templates (placeholders only) and you create the real files alongside. Copy all three and fill them in:

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

Then fill in the `.env` (the keys from steps 3–5 + the `DATABASE_URL` from step 2):

```dotenv
ANTHROPIC_API_KEY=
CURATOR_MODEL=claude-haiku-4-5
CURATOR_MONTHLY_BUDGET_USD=8
VOYAGE_API_KEY=
EMBEDDING_MODEL=voyage-4-lite
REDDIT_USER_AGENT=ai-news-aggregator/1.0 (personal, non-commercial)
GITHUB_TOKEN=
TWITTER_AUTH_TOKEN=
TWITTER_CT0=
TELEGRAM_BOT_TOKEN=
DATABASE_URL=
EXA_API_KEY=
```

Also edit `config/seeds.yaml` with your taste examples (`gold` = what you want to receive, `noise` = what should be rejected). They feed the cold-start in step 7 — 3–5 of each is enough.

> **Config vs. deploy — be honest about the trade-off.**
> - **Secrets (`.env`)** carry credentials and are **never** versioned — they're already in `.gitignore`. Always inject them via *environment variables* on the host (Railway), never via a file in git.
> - **`config/sources.yaml` and `config/seeds.yaml`** carry *personal* data (your `telegram_user_id`, your interest profile), **not** credentials. In a **clean clone/fork that will become public**, the recommendation is to add them to `.gitignore` so you don't accidentally commit personal data.
> - **However**, deploying on Railway is done **via git**: the service builds from what's committed in **your private deploy repository**. So, for the bot to find these configs in production, `config/sources.yaml` (and `seeds.yaml`, if you're going to run the seed there) **needs to be committed in that private repo** — normally (if it's not in `.gitignore`) or via `git add -f` (if it is). The alternative is to skip the file and provide the configuration through environment variables.
> - In short: **there is no** "the `sources.yaml` is never committed." In a **private** deploy repo it's typically committed on purpose; what you avoid is leaking it in a **public** fork.

### 7. Cold-start (seed) and run locally

Before the first cycle, run the **seed** once to give the system signal on day 1:

```bash
python -m src.seed
```

It reads `config/seeds.yaml`, resolves each user by the `telegram_user_id` in `sources.yaml`, and loads the examples as **preloaded votes** (`gold` → 👍, `noise` → 👎, `origin='seed'`), embedding each one. With that, `/buscar` and recall already **work on day 1**, before any real ingestion. It depends on **`DATABASE_URL`** and **`VOYAGE_API_KEY`** already configured (steps 2 and 3) and the schema already applied. It's idempotent: re-running doesn't duplicate (deterministic source_id). With no seeds filled in, it does nothing.

> Note: the seed writes posts with `source_platform='seed'` — a value already accepted by the `CHECK` on `posts.source_platform` in `db/schema.sql` (alongside `github`/`manual`), nothing to configure.

Now bring up the bot:

```bash
python -m src.bot.bot
```

Brings up the bot in long-polling. The **delivery** job (daily, at the `DIGEST_HOUR` time) and the **pipeline** collect→embed→curate job (30min) run inside it. To force a pipeline cycle manually, without the bot:

```bash
python -m src.pipeline
```

### 8. Deploy on Railway

The project already comes with `Procfile` and `railway.json` (NIXPACKS, `restartPolicyType: ALWAYS`). It's an **always-on** process, so it **needs no separate cron** — the jobs live inside the bot.

- **Start command:** `python -m src.bot.bot`
- Configure the same variables as the `.env` (steps 2–5) as *environment variables* in the Railway dashboard. **Never** commit the `.env`.
- Railway builds **from git**: `config/sources.yaml` (and `seeds.yaml`, if you're going to run the seed there) needs to be **committed in your private deploy repository** — a normal commit, or `git add -f` if you've gitignored those configs (see the note in step 6).

📖 **Full step-by-step in [`DEPLOY.md`](DEPLOY.md)**: bring up the **single service** `bot` (always-on, with pipeline + delivery via the internal JobQueue), environment variables, the one-off seed, and validation.

> Note: datacenter IP traffic (Railway) may be throttled by the Reddit feed.

### 9. Plug into Claude (MCP)

```bash
claude mcp add acervo -- .venv/bin/python -m src.mcp_server.server
```

The command matches `.mcp.json` and exposes `buscar_acervo`, `lembrar_votos`, and `ver_foco` over your curated archive. To run the server directly in stdio:

```bash
python -m src.mcp_server.server
```

---

## 💰 Cost

Runs comfortably at **~$0–10/mo**, leaning on free tiers:

| Service | Plan | Note |
| --- | --- | --- |
| **Supabase** | Free (Postgres + pgvector) | ~500MB; the schema has a retention note pruning `raw_text` from old rejected items. |
| **Voyage AI** | Free tier | `voyage-4-lite`, generous free tier. |
| **Anthropic Haiku 4.5** | Paid, with a cap | `SpendGuard` + `CURATOR_MONTHLY_BUDGET_USD` (default **$8**) + prompt caching of the rubric. Curation **pauses** when the cap is exceeded. |
| **GitHub / Reddit / X** | Free | Public Search API, RSS feed, and twitter-cli via cookie. |
| **Railway** | Usage-based | Always-on process. |

Curation is the only variable cost — and it's **capped by design**.

---

## 👥 Multi-tenant

Everything is scoped by `user_id`, derived from `telegram_user_id` (UNIQUE in `users`). The bot locks to an **allowlist** built from `sources.yaml`; each update is resolved via `get_or_create_user`.

- **`posts` is a SHARED POOL**, curated **once** (quality verdict, user-agnostic) → adding people does **not** multiply the curation cost.
- Each person's **taste** lives in `votes`, `deliveries`, and `focus` per user.
- **Privacy** is guaranteed by the `user_id` filter on all recall/search queries.

As a template, the recommended path is **each person spins up their own instance** (your bot, your database) — but the code already supports multiple users on the same deploy, just by adding blocks to `config/sources.yaml`.

---

## 🔌 Querying via Claude (MCP)

After `claude mcp add acervo`, Claude can see your curated archive through these tools (thin shells over the `Database`, resolving "you" by the `TELEGRAM_USER_ID` from `.env` or the 1st user in `sources.yaml`):

| Tool | What it does |
| --- | --- |
| `buscar_acervo` | Semantic search across the pool of curated posts (`search_pool`). |
| `lembrar_votos` | Recall across your 👍/👎 — "what have I already liked about X?" (`recall_voted`). |
| `ver_foco` | Shows the active `/foco` per bucket (`active_focus`). |

These are the **same methods** that `/buscar` and chat (recall) use on Telegram — just now inside Claude.

---

## 📂 Repository structure

```
src/
  bot/bot.py              # Telegram interface + jobs (daily delivery, pipeline 30min)
  pipeline.py             # 1 ingest→embed→curate cycle (idempotent)
  seed.py                 # cold-start: loads seeds.yaml as votes (python -m src.seed)
  ingestion/
    base.py               # IngestionSource ABC
    reddit_source.py      # public .rss/Atom feed
    github_source.py      # Search API + README
    x_source.py           # twitter-cli via cookies
  curation/
    curator.py            # Haiku 4.5 + SpendGuard (Verdict)
    steering.py           # chat → ChatIntent (steer/recall/balance)
  common/
    config.py             # .env + sources.yaml + seeds.yaml
    db.py                 # asyncpg + pgvector (scoped by user_id)
    models.py             # IngestedPost + Pydantic schemas
    embeddings.py         # Voyage voyage-4-lite
  mcp_server/server.py    # FastMCP (buscar_acervo / lembrar_votos / ver_foco)
db/schema.sql             # Postgres 15+/pgvector DDL
db/reset.sql              # drops the schema's tables (DELETES data)
config/sources.yaml       # sources per user (personal data, not a secret)
config/seeds.yaml         # cold-start examples per user (personal data)
Procfile · railway.json   # Railway deploy (via git)
DEPLOY.md                 # step-by-step deploy guide
```

---

## 📜 License

Open-source template — make your fork, spin up your copy, adjust the sources, and have fun. Always keep your **keys** (`.env`) out of version control. `config/sources.yaml`/`config/seeds.yaml` carry personal data (not credentials): in **public** forks, gitignore them; in your **private** deploy repo, they typically need to be committed for Railway to build (see step 6 and `DEPLOY.md`).
