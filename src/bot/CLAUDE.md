# src/bot — the Telegram bot

This folder is the "face" of the project: the Telegram bot that talks to you. It runs always-on in long-polling (python-telegram-bot 22.8), locked to a set of known users (allowlist), and does three things — (1) delivers, once a day, a digest of AI news in two buckets (📦 GitHub repos and 🗞️ news from X+Reddit), ranked by your taste and the active direction; (2) learns from your 👍/👎 votes on the cards; and (3) understands your free-form text (steer the feed, recall what you voted on, adjust the mix) and saves links you paste. On top of that, the bot itself runs the ingest/embed/curate pipeline from time to time, so it doesn't need a separate cron. Almost everything real (database, embeddings, curation) lives in `../common`, `../curation` and `../pipeline` — this folder only orchestrates and talks to Telegram.

## Files

- **`bot.py`** — the entire bot module. It's the only logic file here. Details by area below.

- **`__init__.py`** — only re-exports `build_application`, `deliver_pending` and `main` from `bot.py`. It's the folder's facade; whoever imports the bot imports from here.

## How `bot.py` connects to the rest

### Shared state and authorization
- Everything expensive (a single instance of `Database`, `Embedder`, `AnthropicCurator` from `../curation`, `Steerer` from `../curation/steering`, and the `Settings`) is created once at startup and stored in `application.bot_data`, under the `KEY_*` keys (avoids loose strings). The `_db()` / `_embedder()` helpers pull it back.
- **Allowlist**: the bot only responds to `telegram_user_id`s listed in `config/sources.yaml` (loaded via `load_sources` from `../common/config`). `_is_allowed()` blocks anyone else (no id, e.g. a channel → denied). It's multi-user: each update is resolved to an internal `user_id` via `_resolve_user_id()` → `db.get_or_create_user()`, cached in `bot_data`.
- In `_post_init()` (runs after the loop starts) each allowlist user is registered in the database with their `display_name`.

### Daily delivery in 2 buckets (`deliver_pending` + `_deliver_bucket`)
- `deliver_pending()` fetches the approved-but-not-delivered posts (`db.approved_undelivered`) for each user and splits them into the buckets defined in the **`BUCKETS`** constant: each bucket has a key, a header, source platforms and a cap per digest. The bucket key (`repos` / `news`) is what matches the focus `bucket` — it's the link between the "direction" (/foco) and the right bucket.
- `_deliver_bucket()` ranks WITHIN the bucket by adding two signals to each card's score:
  - **affinity**: your 👍/👎 for that bucket (nearest neighbors via `db.nearest_votes`, restricted to the bucket's platforms — votes on repos don't touch news). Turns on starting from the first vote (`MIN_VOTES_FOR_AFFINITY`). Here affinity only RANKS; nothing is hidden.
  - **direction/focus** (`_focus_boost`): pushes toward the bucket's active topic, by cosine similarity (normalized vectors). Works even without any vote.
- **New × relevant balancing**: within the cap, a reserve of slots goes to the NEWEST (by `published_at`) and the rest to the most relevant (affinity+focus). The default reserve comes from **`FRESH_SLOTS`** per bucket; if you adjusted the mix via chat (/balance → `db.get_balance`), that wins. Whatever doesn't fit today stays a candidate in the next digests.
- Each card goes out via `_format_card()` (header adapted to github/twitter/reddit/manual) with the 👍/👎 buttons (`_vote_keyboard`), and the delivery is recorded (`db.record_delivery`) with the score.

### JobQueues (two periodic jobs)
Configured in `build_application` if `job_queue` exists (needs the `[job-queue]` extra):
- **`_job_deliver`** → runs `deliver_pending` **once a day** at a fixed time (`run_daily` at hour **`DIGEST_HOUR`** in timezone **`DIGEST_TZ`**; default 7am America/Sao_Paulo) — the "mini-newspaper". `/feed` delivers on demand at any time. It's in the daily job (not in `/feed`) that auto-balancing runs.
- **`_job_pipeline`** → runs the full pipeline (`run_ingestion` → `run_embedding` → `run_curation`, all from `../pipeline`) every **`PIPELINE_INTERVAL_SECONDS`** (30min). That's what makes a separate cron service unnecessary. A run's failures are logged and isolated, they don't bring the bot down.

### Chat router (`on_text` → `_handle_chat`)
Text without a command goes through `on_text`: if it has a URL, it goes to `_save_link`; otherwise, to `_handle_chat`, which uses the `Steerer` (`../curation/steering`) to classify the intent and dispatch:
- **steer** → `_apply_focus`: embeds each topic and writes it to `focus` (`db.set_focus`), replacing that bucket's previous direction, with a deadline in days.
- **recall** → `_do_recall`: recalls what YOU voted on about a topic (`db.recall_voted`), filtering by 👍/👎/any.
- **balance** → `_apply_balance`: adjusts the new×relevant mix of one bucket (or both) via `db.set_balance`.
- **other / no intent** → responds with the hint (`_CHAT_HINT`).

### Commands and voting
- **`/start`** (`cmd_start`) — welcome + registers the user; explains the commands.
- **`/feed`** (`cmd_feed`) — triggers `deliver_pending` on demand.
- **`/buscar <query>`** (`cmd_buscar`) — embeds the question (`embedder.embed_query`) and runs a semantic search over the curated archive (`db.search_pool`); marks ❤️ on what you liked.
- **`/foco`** (`cmd_foco`) — with no argument it shows the active focus; `/foco limpar` clears it (`db.clear_focus`); `/foco <text>` steers (falls into the same `_handle_chat`).
- **Paste a link** (`_save_link`) — active curation: reads the page as clean markdown via Jina Reader (`_fetch_readable`, base `JINA_BASE`, cut at `MANUAL_MAX_CHARS`), saves it as a `manual` post (dedup by the URL itself), embeds it and records a 👍 right away. It then shows up in `/buscar`.
- **👍/👎 buttons** (`on_vote`, `CallbackQueryHandler`) — reads the `callback_data` (`up:<id>` / `down:<id>` / `noop`), records the vote (`db.record_vote`) and swaps the buttons for an inert "✅ recorded".

### Bootstrap
- **`build_application`** builds the `Application`, populates `bot_data`, registers the handlers and jobs, and ties the database's `connect()/close()` to the `post_init`/`post_shutdown` hooks (if `connect_db=True`).
- **`main`** loads `settings` (`../common/config`), creates `Database` and `Embedder`, builds the app and calls `run_polling`. Run with `python -m src.bot.bot`.

## Key tuning constants (top of `bot.py`)
- **`BUCKETS`** — defines the buckets: `repos` (github, cap `REPOS_PER_DIGEST=5`) and `news` (reddit+twitter, cap `NEWS_PER_DIGEST=12`). The key links /foco to the bucket.
- **`FRESH_SLOTS`** — `{"repos": 2, "news": 4}`: slots reserved for the newest in each bucket (default, if there's no saved /balance).
- **`MIN_VOTES_FOR_AFFINITY`** = 1 — how many votes before affinity enters the ranking.
- **`DIGEST_HOUR`** / **`DIGEST_TZ`** (default 7am / America/Sao_Paulo) — fixed time of the daily delivery · **`PIPELINE_INTERVAL_SECONDS`** = 30min — pipeline interval.
- **`SEARCH_LIMIT`** = 10 — results for /buscar and recall.
- **`TEXT_PREVIEW_CHARS`** = 500 · **`MANUAL_MAX_CHARS`** = 8000 — text limits in the card and the saved link.
- **`JINA_BASE`** / **`URL_RE`** — page reader and URL detection in the "paste a link" feature.
