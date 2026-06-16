# src/bot — the Telegram bot

This folder is the project's "face": the Telegram bot that talks to you. It runs always-on in long-polling (python-telegram-bot 22.8), locked to a set of known users (allowlist), and does three things — (1) delivers, once a day, a digest ("mini-newspaper") of AI news in two buckets (📦 GitHub repos and 🗞️ X+Reddit news), ranked by your taste and the active direction, ignoring anything too old; (2) learns from your 👍/👎 votes on the cards — including learning the new×relevant mix on its own; and (3) understands your free text (steer the feed, recall what you voted on, adjust the mix) and saves links you paste. On top of that, the bot itself runs the ingest/embed/curation pipeline from time to time (and on demand via `/run`), so it needs no separate cron. Almost everything substantial (database, embeddings, curation) lives in `../common`, `../curation` and `../pipeline` — this folder only orchestrates and talks to Telegram.

## Files

- **`bot.py`** — the entire bot module. It's the only logic file here. Per-area details below.

- **`__init__.py`** — just re-exports `build_application`, `deliver_pending` and `main` from `bot.py`. It's the folder's facade; whoever imports the bot imports from here.

## How `bot.py` connects to the rest

### Shared state and authorization
- Everything expensive (a single instance of `Database`, `Embedder`, `AnthropicCurator` from `../curation`, `Steerer` from `../curation/steering`, and the `Settings`) is created once at startup and stored in `application.bot_data`, under the `KEY_*` keys (avoids loose strings). The `_db()` / `_embedder()` helpers pull it back out.
- **Allowlist**: the bot only responds to `telegram_user_id`s listed in `config/sources.yaml` (loaded via `load_sources` from `../common/config`). `_is_allowed()` blocks anyone else (no id, e.g. a channel → denied). It's multi-user: each update is resolved to an internal `user_id` via `_resolve_user_id()` → `db.get_or_create_user()`, cached in `bot_data`.
- In `_post_init()` (runs after the loop starts) each allowlist user is registered in the database with their `display_name`.

### Daily delivery in 2 buckets (`deliver_pending` + `_deliver_bucket`)
- `deliver_pending(app, tune=False)` fetches each user's approved-but-not-delivered posts (`db.approved_undelivered`, `limit=300`) and splits them into the buckets defined in the **`BUCKETS`** constant: each bucket has a key, header, source platforms and a cap per digest. The bucket key (`repos` / `news`) is what matches the focus's `bucket` — it's the link between the "direction" (/focus) and the right bucket.
- **Age cutoff**: nothing published more than **`DELIVERY_MAX_AGE_DAYS`** (30) days ago is delivered — `max_age_days` goes straight into `db.approved_undelivered` (undated posts are kept). It's what guarantees the digest is "news".
- `tune=True` (only in the daily job) lets auto-balancing learn from your votes BEFORE delivering; in `/feed` and `/run` it stays `False`, so as not to shuffle the mix on every request.
- `_deliver_bucket()` ranks WITHIN the bucket by adding two signals into each card's score:
  - **affinity**: your 👍/👎 for that bucket (nearest neighbors via `db.nearest_votes`, restricted to the bucket's platforms — votes on repos don't touch news). Kicks in from the first vote (`MIN_VOTES_FOR_AFFINITY`). Here affinity only RANKS; nothing is hidden.
  - **direction/focus** (`_focus_boost`): pushes toward the bucket's active topic, by cosine similarity (normalized vectors). Works even without any votes.
- **New × relevant balancing**: within the cap, a reserve of slots goes to the NEWEST (by `published_at`, via `_pub_ts`) and the rest to the most relevant (affinity+focus). The default reserve comes from **`FRESH_SLOTS`** per bucket; if you adjusted the mix via chat (/balance → `db.get_balance`, fraction × cap), that wins. **With /focus active, the freshness reserve becomes 0** (`fresh_quota = 0`) — everything by relevance to the topic, so a new off-topic post doesn't jump the focus queue. Whatever doesn't fit today stays a candidate in the next digests.
- When there's an active focus in the bucket, the header gets a "🎯 active focus: …" line with the topics.
- Each card goes out via `_format_card()` (header adapted to github/twitter/reddit/manual) with the 👍/👎 buttons (`_vote_keyboard`), and the delivery is recorded (`db.record_delivery`) with the `affinity_score`.

### Auto-balancing (`_auto_tune_balance`)
- Learns each bucket's novelty fraction on its own from YOUR votes — runs **only in the daily job** (`tune=True`), never in `/feed`/`/run`.
- Uses `db.balance_signal`: only acts with enough signal (**`AUTO_BALANCE_MIN_VOTES`** = 6 votes in the bucket) and when there's score separation (splits votes by the median into `low`/`high`; if either side ends up empty, it gives up). If you like the low-affinity ones (which got in via the freshness slot), it concludes you enjoy discovering and raises novelty; if you reject them, it lowers it.
- Moves with a **small step (EMA, `AUTO_BALANCE_STEP` = 0.15)** toward a target derived from the preference, within **`AUTO_BALANCE_BOUNDS`** (0.15–0.60) — so a manual adjustment via chat (/balance) still dominates for several days. **Respects manual adjustment**: if the current fraction is outside those bounds (you set an extreme by hand), it doesn't auto-adjust. It only writes (`db.set_balance`) if the step changes the fraction by ≥ 0.01.

### JobQueues (two periodic jobs)
Configured in `build_application` if the `job_queue` exists (needs the `[job-queue]` extra; without it only the warning is logged and automatic delivery is off — use `/feed`):
- **`_job_deliver`** → runs `deliver_pending(tune=True)` **once a day** at a fixed time (`run_daily` at hour **`settings.digest_hour`** in timezone **`settings.digest_tz`**; default 7am America/Sao_Paulo, invalid tz falls back to UTC) — the "mini-newspaper". It replaced the old 24h interval. `/feed` delivers on demand at any time. It's in the daily job (not in `/feed`/`/run`) that auto-balancing runs.
- **`_job_pipeline`** → runs the full pipeline (`run_ingestion` → `run_embedding` → `run_curation`, all from `../pipeline`) every **`PIPELINE_INTERVAL_SECONDS`** (30min), with `first=30` (first ingestion ~30s after startup). This is what avoids needing a separate cron service. Failures of a run are logged and isolated, they don't take down the bot.

### Chat router (`on_text` → `_handle_chat`)
Text without a command passes through `on_text`: if it has a URL (`URL_RE`), it goes to `_save_link`; otherwise to `_handle_chat`, which uses the `Steerer` (`../curation/steering`, `parser.parse`) to classify the intent and dispatch:
- **steer** → `_apply_focus`: embeds each topic (`embed_query`) and writes to `focus` (`db.set_focus`), replacing that bucket's previous direction, with a deadline in days.
- **recall** → `_do_recall`: **routes by polarity** (`intent.recall_polarity`). With **`"any"`** (a generic question) it searches the entire curated archive (`db.search_pool`, `archive` mode), marking ❤️ on what you liked; with **`"liked"`/`"disliked"`** it recalls ONLY what you voted on (`db.recall_voted` with `vote=1`/`-1`, `voted` mode). The `recall_query` already arrives in ENGLISH from the Steerer's parser, so it's embedded directly (no extra translation). The "not found" messages and the list headers change depending on the mode.
- **balance** → `_apply_balance`: adjusts (or **RESETS**) a bucket's new×relevant mix (or both, `bucket == "both"`) via `db.set_balance`. When the parser sends `balance_reset` (e.g. "undo that"), instead of writing a fraction it calls `db.clear_balance` on each bucket — back to default — and confirms with "back to default" (the confirmation is only actually given on the reset path; the normal path confirms the adjusted percentage).
- **other / no intent** (`parse` returns `None`) → replies with the hint (`_CHAT_HINT`), or with `intent.reply` when there is one.

### Commands and voting
The commands registered in `build_application` are: **`/start`**, **`/feed`**, **`/run`**, **`/search`**, **`/focus`** and **`/mix`** (plus the vote's `CallbackQueryHandler` and the `MessageHandler` for text without a command).

- **`/start`** (`cmd_start`) — welcome + registers the user; explains the commands (lists `/feed`, `/run`, `/search`, `/focus`, `/mix`, plus talking freely and pasting a link).
- **`/feed`** (`cmd_feed`) — fires `deliver_pending` on demand (without `tune`); if nothing new, replies "Nothing new for now. 🙂".
- **`/run`** (`cmd_run`) — runs a **FULL cycle on demand**: `run_ingestion` → `run_embedding` → `run_curation` → `deliver_pending`, and replies with a summary (ingested/curated/delivered). Protected by a **`pipeline_running` lock** in `bot_data`: if a cycle is already running, it replies "⏳ A cycle is already running…" and exits; the lock is always released in `finally`. Errors are logged and turned into a friendly reply.
- **`/search <query>`** (`cmd_search`) — semantic search over the curated archive. Since the archive is embedded in ENGLISH, it first **translates the query PT→EN** (`steerer.translate_to_en`), then embeds (`embedder.embed_query`) and searches (`db.search_pool`); marks ❤️ on what you liked. Without an argument, it shows the usage.
- **`/focus`** (`cmd_focus`) — without an argument shows the active focus (per bucket); `/focus clear|off|reset` clears it (`db.clear_focus`); `/focus <text>` steers (falls into the same `_handle_chat`).
- **`/mix`** (`cmd_mix`) — shows each bucket's current new×relevant balance (`repos` / `news`). For each bucket it reads `db.get_balance`: if there's no saved adjustment (`None`), it uses the bucket's default reserve (`FRESH_SLOTS / _BUCKET_CAP`) and marks the tag **`default`**; if there is, it uses the saved fraction and marks **`adjusted`**. It prints "~N% novelty / (100−N)% relevance" per bucket, plus a note explaining that the bot auto-adjusts from ~6 votes in the bucket and how to change/reset it by hand via chat.
- **Pasting a link** (`_save_link`) — active curation: reads the page as clean markdown via Jina Reader (`_fetch_readable`, base `JINA_BASE`, cut at `MANUAL_MAX_CHARS`), saves it as a `manual` post (dedup by the URL itself), embeds it and already records a 👍. It starts showing up in `/search`.
- **👍/👎 buttons** (`on_vote`, `CallbackQueryHandler`) — reads the `callback_data` (`up:<id>` / `down:<id>` / `noop`), records the vote (`db.record_vote`) and swaps the buttons for an inert "✅ recorded".

### Bootstrap
- **`build_application`** assembles the `Application`, populates `bot_data`, registers the handlers and jobs, and ties the database's `connect()/close()` to the `post_init`/`post_shutdown` hooks (if `connect_db=True`).
- **`main`** loads `settings` (`../common/config`), creates `Database` and `Embedder`, assembles the app and calls `run_polling`. Run with `python -m src.bot.bot`.

## Key tuning constants (top of `bot.py`)
- **`BUCKETS`** — defines the buckets: `repos` (github, cap `REPOS_PER_DIGEST=5`) and `news` (reddit+twitter, cap `NEWS_PER_DIGEST=12`). The key links /focus to the bucket. **`_BUCKET_CAP`** is the bucket→cap map derived from it (used for the default freshness fraction).
- **`FRESH_SLOTS`** — `{"repos": 2, "news": 4}`: slots reserved for the newest in each bucket (default, if there's no saved /balance).
- **`DELIVERY_MAX_AGE_DAYS`** = 30 — never delivers anything published more days ago than this (undated posts pass).
- **`AUTO_BALANCE_MIN_VOTES`** = 6 · **`AUTO_BALANCE_STEP`** = 0.15 (EMA) · **`AUTO_BALANCE_BOUNDS`** = (0.15, 0.60) — minimum votes, step and bounds of auto-balancing (daily job only).
- **`MIN_VOTES_FOR_AFFINITY`** = 1 — from how many votes affinity enters the ranking.
- The digest's fixed hour comes from **`settings.digest_hour`** / **`settings.digest_tz`** (default 7am / America/Sao_Paulo) · **`PIPELINE_INTERVAL_SECONDS`** = 30min — the pipeline interval.
- **`SEARCH_LIMIT`** = 10 — results for /search and recall.
- **`TEXT_PREVIEW_CHARS`** = 500 · **`MANUAL_MAX_CHARS`** = 8000 — text limits in the card and in the saved link.
- **`JINA_BASE`** / **`URL_RE`** — page reader and URL detection in the "paste a link" feature.
- **`KEY_*`** — `application.bot_data` keys (`KEY_DB`, `KEY_EMBEDDER`, `KEY_ALLOWED`, `KEY_USER_MAP`, `KEY_CURATOR`, `KEY_SETTINGS`, `KEY_STEERER`); besides those, `/run` uses the transient `pipeline_running` flag.
