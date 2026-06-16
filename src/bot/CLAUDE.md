# src/bot — the Telegram bot

This folder is the "face" of the project: the Telegram bot that talks to you. It runs always-on in long-polling (python-telegram-bot 22.8), locked to a set of known users (allowlist), and does three things — (1) delivers once a day a digest ("mini-newspaper") of AI news in two buckets (📦 GitHub repos and 🗞️ X+Reddit news), ranked by your taste and the active steering, ignoring what's too old; (2) learns from your 👍/👎 votes on the cards — including learning the new×relevant mix on its own; and (3) understands your free text (steer the feed, recall what you voted on, adjust the mix) and saves links you paste. On top of that, the bot itself runs the ingest/embed/curation pipeline from time to time (and on demand via `/run`), so it doesn't need a separate cron. Almost everything real (database, embeddings, curation) lives in `../common`, `../curation` and `../pipeline` — this folder only orchestrates and talks to Telegram.

## Files

- **`bot.py`** — the entire bot module. It's the only logic file here. Per-area details below.

- **`__init__.py`** — only re-exports `build_application`, `deliver_pending` and `main` from `bot.py`. It's the folder's facade; whoever imports the bot imports from here.

## How `bot.py` connects to the rest

### Shared state and authorization
- Everything expensive (a single instance of `Database`, `Embedder`, `AnthropicCurator` from `../curation`, `Steerer` from `../curation/steering`, and the `Settings`) is created once at startup and stored in `application.bot_data`, under the `KEY_*` keys (avoids loose strings). The `_db()` / `_embedder()` helpers pull it back.
- **Allowlist**: the bot only responds to `telegram_user_id`s listed in `config/sources.yaml` (loaded via `load_sources` from `../common/config`). `_is_allowed()` blocks anyone else (no id, e.g. channel → denied). It's multi-user: each update is resolved to an internal `user_id` via `_resolve_user_id()` → `db.get_or_create_user()`, with a cache in `bot_data`.
- In `_post_init()` (runs after the loop starts) each allowlist user is registered in the database with their `display_name`.

### Daily delivery in 2 buckets (`deliver_pending` + `_deliver_bucket`)
- `deliver_pending(app, tune=False)` fetches the approved-but-not-delivered posts (`db.approved_undelivered`, `limit=300`) of each user and splits them into the buckets defined in the **`BUCKETS`** constant: each bucket has a key, header, source platforms and a cap per digest. The bucket key (`repos` / `news`) is what matches the focus `bucket` — it's the link between the "steering" (/focus) and the right bucket.
- **Age cutoff**: nothing published more than **`DELIVERY_MAX_AGE_DAYS`** (30) days ago is delivered — `max_age_days` goes straight into `db.approved_undelivered` (dateless posts are kept). It's what guarantees the digest is "new".
- `tune=True` (only in the daily job) lets auto-balancing learn from your votes BEFORE delivering; in `/feed` and `/run` it stays `False`, so as not to reshuffle the mix on every request.
- `_deliver_bucket()` ranks WITHIN the bucket by adding two signals to each card's score:
  - **affinity**: your 👍/👎 from that bucket (nearest neighbors via `db.nearest_votes`, restricted to the bucket's platforms — votes on repos don't touch news). Kicks in from the first vote (`MIN_VOTES_FOR_AFFINITY`). Here affinity only RANKS; nothing is hidden.
  - **steering/focus** (`_focus_boost`): pushes toward the bucket's active topic, by cosine similarity (normalized vectors). Works even with no votes at all.
- **New × relevant balancing**: within the cap, a reserve of slots goes to the NEWEST (by `published_at`, via `_pub_ts`) and the rest to the most relevant (affinity+focus). The default reserve comes from **`FRESH_SLOTS`** per bucket; if you adjusted the mix via chat (/balance → `db.get_balance`, fraction × cap), that wins. **With /focus active, the freshness reserve becomes 0** (`fresh_quota = 0`) — everything by relevance to the topic, so a new off-topic post doesn't jump the focus queue. Whatever doesn't fit today stays a candidate for the next digests.
- When there's active focus in the bucket, the header gets a line "🎯 active focus: …" with the topics.
- Each card goes out via `_format_card()` (header adapted to github/twitter/reddit/manual) with the 👍/👎 buttons (`_vote_keyboard`), and the delivery is recorded (`db.record_delivery`) with the `affinity_score`.

### Auto-balancing (`_auto_tune_balance`)
- Learns on its own each bucket's novelty fraction from YOUR votes — runs **only in the daily job** (`tune=True`), never in `/feed`/`/run`.
- Uses `db.balance_signal`: only acts with enough signal (**`AUTO_BALANCE_MIN_VOTES`** = 6 votes in the bucket) and when there is score separation (splits the votes by the median into `low`/`high`; if one side ends up empty, it gives up). If you like the low-affinity ones (which got in through the freshness slot), it concludes you enjoy discovery and raises novelty; if you reject them, it lowers it.
- Moves with a **small step (EMA, `AUTO_BALANCE_STEP` = 0.15)** toward a target derived from the preference, within **`AUTO_BALANCE_BOUNDS`** (0.15–0.60) — so a manual adjustment via chat (/balance) still dominates for several days. **Respects manual adjustment**: if the current fraction is outside these bounds (you set an extreme by hand), it doesn't auto-adjust. It only writes (`db.set_balance`) if the step changes the fraction by ≥ 0.01.

### JobQueues (two periodic jobs)
Configured in `build_application` if `job_queue` exists (needs the `[job-queue]` extra; without it only the warning is logged and automatic delivery is off — use `/feed`):
- **`_job_deliver`** → runs `deliver_pending(tune=True)` **once/day** at a fixed time (`run_daily` at hour **`settings.digest_hour`** in timezone **`settings.digest_tz`**; default 7am America/Sao_Paulo, invalid tz falls back to UTC) — the "mini-newspaper". It replaced the old 24h interval. `/feed` delivers on demand at any time. It's in the daily job (not in `/feed`/`/run`) that auto-balancing runs.
- **`_job_pipeline`** → runs the full pipeline (`run_ingestion` → `run_embedding` → `run_curation`, all from `../pipeline`) every **`PIPELINE_INTERVAL_SECONDS`** (30min), with `first=30` (first ingestion ~30s after startup). This is what removes the need for a separate cron service. Failures of a run are logged and isolated, they don't crash the bot.

### Chat router (`on_text` → `_handle_chat`)
Text without a command goes through `on_text`: if it has a URL (`URL_RE`), it goes to `_save_link`; otherwise, to `_handle_chat`, which uses the `Steerer` (`../curation/steering`, `parser.parse`) to classify the intent and dispatch:
- **steer** → `_apply_focus`: embeds each topic (`embed_query`) and writes to `focus` (`db.set_focus`), replacing that bucket's previous steering, with a deadline in days.
- **recall** → `_do_recall`: recalls what YOU voted on about a topic (`db.recall_voted`), filtering by 👍/👎/any. The `recall_query` already arrives in ENGLISH from the Steerer parser, so it's embedded directly (no extra translation).
- **balance** → `_apply_balance`: adjusts the new×relevant mix of one bucket (or both, `bucket == "both"`) via `db.set_balance`.
- **other / no intent** (`parse` returns `None`) → replies with the hint (`_CHAT_HINT`), or with `intent.reply` when present.

### Commands and voting
- **`/start`** (`cmd_start`) — welcome + registers the user; explains the commands.
- **`/feed`** (`cmd_feed`) — triggers `deliver_pending` on demand (without `tune`); if nothing new, replies "Nothing new for now. 🙂".
- **`/run`** (`cmd_run`) — runs a **FULL cycle on demand**: `run_ingestion` → `run_embedding` → `run_curation` → `deliver_pending`, and replies with a summary (ingested/curated/delivered). Protected by a **`pipeline_running` lock** in `bot_data`: if a cycle is already running, it replies "⏳ A cycle is already running…" and exits; the lock is always released in `finally`. Errors are logged and turned into a friendly reply.
- **`/search <query>`** (`cmd_search`) — semantic search over the curated archive. Since the archive is embedded in ENGLISH, it first **translates the query PT→EN** (`steerer.translate_to_en`), then embeds (`embedder.embed_query`) and searches (`db.search_pool`); marks ❤️ on what you liked. Without an argument, shows the usage.
- **`/focus`** (`cmd_focus`) — without an argument shows the active focus (per bucket); `/focus clear|off|reset` clears it (`db.clear_focus`); `/focus <text>` steers (falls into the same `_handle_chat`).
- **Pasting a link** (`_save_link`) — active curation: reads the page as clean markdown via Jina Reader (`_fetch_readable`, base `JINA_BASE`, cut at `MANUAL_MAX_CHARS`), saves it as a `manual` post (dedup by the URL itself), embeds it and immediately records a 👍. It starts showing up in `/search`.
- **👍/👎 buttons** (`on_vote`, `CallbackQueryHandler`) — reads the `callback_data` (`up:<id>` / `down:<id>` / `noop`), records the vote (`db.record_vote`) and swaps the buttons for an inert "✅ recorded".

### Bootstrap
- **`build_application`** builds the `Application`, populates `bot_data`, registers the handlers and jobs, and ties the database `connect()/close()` to the `post_init`/`post_shutdown` hooks (if `connect_db=True`).
- **`main`** loads `settings` (`../common/config`), creates `Database` and `Embedder`, builds the app and calls `run_polling`. Run with `python -m src.bot.bot`.

## Key tuning constants (top of `bot.py`)
- **`BUCKETS`** — defines the buckets: `repos` (github, cap `REPOS_PER_DIGEST=5`) and `news` (reddit+twitter, cap `NEWS_PER_DIGEST=12`). The key links /focus to the bucket. **`_BUCKET_CAP`** is the bucket→cap map derived from it (used for the default freshness fraction).
- **`FRESH_SLOTS`** — `{"repos": 2, "news": 4}`: slots reserved for the newest in each bucket (default, if there's no saved /balance).
- **`DELIVERY_MAX_AGE_DAYS`** = 30 — doesn't deliver anything published more days ago than this (dateless posts pass).
- **`AUTO_BALANCE_MIN_VOTES`** = 6 · **`AUTO_BALANCE_STEP`** = 0.15 (EMA) · **`AUTO_BALANCE_BOUNDS`** = (0.15, 0.60) — minimum votes, step and bounds of auto-balancing (only in the daily job).
- **`MIN_VOTES_FOR_AFFINITY`** = 1 — from how many votes affinity enters the ranking.
- The digest's fixed time comes from **`settings.digest_hour`** / **`settings.digest_tz`** (default 7am / America/Sao_Paulo) · **`PIPELINE_INTERVAL_SECONDS`** = 30min — the pipeline interval.
- **`SEARCH_LIMIT`** = 10 — results for /search and recall.
- **`TEXT_PREVIEW_CHARS`** = 500 · **`MANUAL_MAX_CHARS`** = 8000 — text limits on the card and on the saved link.
- **`JINA_BASE`** / **`URL_RE`** — page reader and URL detection in the "paste a link" feature.
- **`KEY_*`** — keys of `application.bot_data` (`KEY_DB`, `KEY_EMBEDDER`, `KEY_ALLOWED`, `KEY_USER_MAP`, `KEY_CURATOR`, `KEY_SETTINGS`, `KEY_STEERER`); besides those, `/run` uses the transient `pipeline_running` flag.
