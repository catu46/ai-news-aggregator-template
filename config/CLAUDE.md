# config/ — bot configuration (sources + cold-start)

This folder holds the project's **hand-editable configuration**: *where* the bot pulls content from (`sources.yaml`) and *how it learns your taste on day 1* (`seeds.yaml`). Both files are organized **per user** (multi-tenant): the operator is the `owner` user, and you can add more people by creating new blocks. Each file has an `.example` version (placeholders only, safe to commit) and a "real" version (with your personal data). **The bot reads the real files (`sources.yaml` and `seeds.yaml`), not the `.example` ones.**

## Files

- **`sources.example.yaml`** → template of the monitored sources, per user. Structure: `users:` → one block per person (e.g. `owner`) with:
  - `telegram_user_id` (your numeric Telegram id — get it from `@userinfobot` or by sending `/start` to the bot) and `display_name`;
  - `reddit.subreddits` — list of followed subreddits (without the `r/`);
  - `x.accounts` — followed X accounts (without the `@`) — and `x.searches` — advanced searches using X operators (`from:`, `OR`, `min_faves:`, `lang:`, `-filter:replies`);
  - `github.queries` — list of topics/queries for GitHub's Search API (keywords + operators like `language:`, `topic:`).
  - You can add a 2nd person by uncommenting the `friend1` block at the end of the file. **Connection:** this is what defines the "capture radius" of each platform's collector; every item turns into a call to the Reddit/X/GitHub APIs.

- **`seeds.example.yaml`** → template of the cold-start examples, per user. Structure: `users:` → one block per person with two lists:
  - `gold` — examples of what you **want** to receive (a dense post/discussion); accepts `text:` (pasted) and an optional `url:`;
  - `noise` — examples of what the curator **should reject** (basic tutorial, hype, clickbait).
  - 3–5 of each is enough. **Connection:** it bootstraps two things at once — (1) the curator's *few-shot* (it already filters with your taste from the 1st message) and (2) the recall archive, entering as preloaded votes (`gold`=👍 / `noise`=👎, `origin='seed'`), so `/buscar` already works on day 1.

## How to use (onboarding)

1. Copy each `.example` to its real name:
   - `config/sources.example.yaml` → `config/sources.yaml`
   - `config/seeds.example.yaml` → `config/seeds.yaml`
2. Fill the real files with your values (subreddits, accounts, queries, gold/noise examples).
3. The real files **carry personal data** (your `telegram_user_id`, your interests). In a clean fork / public repo, **gitignore them** (`config/sources.yaml`, `config/seeds.yaml`) and keep only the `.example` files versioned.
4. **Deploy on Railway** (which deploys via git): the app needs to see the config at runtime, so in **your private repo** the real files **have to be in git** (or you inject the config via environment variables). In short: `.example` = public and safe; real = private, in git only if the repo is private, otherwise via variables.
