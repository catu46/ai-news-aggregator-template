# Deploying on Railway

Architecture: **a single always-on service**. The bot runs the Telegram
long-polling **and**, through an internal `JobQueue`, the pipeline (collect →
embed → curate) and the digest delivery. **No separate cron service needed.**

| Service | Type | Start command | What it runs |
|---|---|---|---|
| **bot** | Always-on | `python -m src.bot.bot` | 24/7: Telegram polling + 2 internal jobs — **pipeline every 30 min** and **digest delivery once a day** (`/feed` forces it anytime) |

> The **seed** (cold-start) is an **optional one-off**, not a service: run it once
> (locally is usually simpler) and you're done.

---

## 1. Prerequisites

- Database on **Supabase Free** with `db/schema.sql` applied (SQL Editor).
- GitHub repo — **private** (it carries your `config/sources.yaml`).
- A **Railway** account.

## 2. Connect the repository

1. Railway → **New Project** → **Deploy from GitHub repo** → pick this repo.
2. This creates **one** service. The build uses `Procfile`/`railway.json` (Nixpacks
   builder, Python 3.12) with `startCommand: python -m src.bot.bot` and
   `restartPolicyType: ALWAYS`.

> ⚠️ Create **only one** service. If Railway suggests a second one (e.g., because
> of a comment in the Procfile), remove it — the pipeline already runs inside the
> bot.

## 3. Environment variables (Service → Variables)

| Var | Required | Note |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Haiku 4.5 curator + steerer |
| `VOYAGE_API_KEY` | yes | `voyage-4-lite` embeddings |
| `TELEGRAM_BOT_TOKEN` | yes | from @BotFather |
| `DATABASE_URL` | yes | Supabase **POOLED** connection string |
| `TELEGRAM_USER_ID` | optional | your numeric id (used by the MCP server; otherwise falls back to the 1st user in `sources.yaml`) |
| `TWITTER_AUTH_TOKEN` | yes* | `auth_token` cookie from a throwaway X account |
| `TWITTER_CT0` | yes* | `ct0` cookie from the same session |
| `REDDIT_USER_AGENT` | optional | descriptive UA (has a default in `.env.example`) |
| `GITHUB_TOKEN` | optional | raises the Search API rate limit |
| `EXA_API_KEY` | optional | boosts the seed's semantic search |
| `CURATOR_MONTHLY_BUDGET_USD` | optional | spend cap (default `8`); curation pauses when exceeded |
| `CURATOR_MODEL` | optional | default `claude-haiku-4-5` |
| `EMBEDDING_MODEL` | optional | default `voyage-4-lite` |

\* Without the X cookies, Twitter ingestion is disabled; Reddit/GitHub keep working.

> The X cookies **expire**. When X ingestion breaks, re-extract
> `auth_token`/`ct0` from a logged-in session and update the vars — the next
> JobQueue cycle (≤ 30 min) picks them up, no redeploy needed.

## 4. Config in git (`config/sources.yaml`)

Railway builds **from git**, so `config/sources.yaml` must be
**committed to your private repo** for the bot to find the allowlist and the
sources in production (normal commit; or `git add -f` if you gitignored the
configs). It's **personal data**, not a secret — which is why the deploy repo
should be **private**. (Secrets stay only in the environment variables, never in
git.)

## 5. Cold-start (seed) — optional, once

Loads `config/seeds.yaml` as preloaded votes so that `/buscar`/recall already
work on day 1. It's usually simpler to run **locally**:

```bash
python -m src.seed
```

Needs `DATABASE_URL` + `VOYAGE_API_KEY` and the schema applied. It's idempotent
(re-running doesn't duplicate). On Railway you can do it as a one-off **"Run"**
with `python -m src.seed`; it doesn't become a service.

## 6. twitter-cli in the build

`twitter-cli` is needed at runtime (X ingestion) and is already in
`requirements.txt`, so Nixpacks installs it via `pip` — no extra step.
If a future version requires system packages, declare them in `railway.json`
under `build.nixpacksPlan.phases.setup.aptPkgs` (we already include `git`).

## 7. Validate (via the logs)

- **bot:** the logs show Telegram polling active (`getUpdates ... 200`); send
  `/start` to the bot.
- **pipeline (internal):** ~30 s after startup, the `pipeline` job shows up
  collecting (Reddit/GitHub/X) → embeddings → curation; it repeats **every 30 min**.
- **delivery:** the digest job runs **once a day**; use `/feed` to force it now.
- **database:** new posts appearing and, after 👍/👎 votes, `vote_counts` changing.
