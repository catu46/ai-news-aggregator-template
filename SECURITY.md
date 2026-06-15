# Security Policy

## Secrets management

This project **never** version-controls credentials. All secrets must live
exclusively in one of these two places:

- A local `.env` file, which is listed in `.gitignore` and is **never**
  committed under any circumstances.
- Host environment variables (for example, on Railway or a VPS),
  injected at runtime.

Do not include secrets in source code, logs, commit messages, issues,
screenshots, or any file tracked by Git. If a secret is exposed
accidentally, consider it compromised and **rotate the key immediately**.

## Sensitive keys

The following variables are considered sensitive and follow the rules above
(local `.env` or host env vars only — **never** committed):

- `ANTHROPIC_API_KEY` — access to the Anthropic API (LLM curator).
- `VOYAGE_API_KEY` — access to the Voyage embeddings API.
- `TELEGRAM_BOT_TOKEN` — Telegram bot token.
- `DATABASE_URL` — database connection string (includes credentials).
- `TWITTER_AUTH_TOKEN` / `CT0` — X/Twitter authentication cookies.
- `GITHUB_TOKEN` — GitHub access token.
- `EXA_API_KEY` — optional Exa key (semantic search).

## Configuration data vs secrets

`config/sources.yaml` and `config/seeds.yaml` are **not** secrets — they carry
**personal** data (your `telegram_user_id`, your interest profile), not
credentials. That's why they're handled differently from `.env`:

- **Clean forks/clones that will become public:** the recommendation is to add
  `config/sources.yaml` and `config/seeds.yaml` to `.gitignore`, to avoid
  accidentally committing personal data to a public repository.
- **Private deploy repository:** the Railway deploy is done **via git** — the
  service builds from whatever is committed. For the bot to find these
  configs in production, `config/sources.yaml` (and `seeds.yaml`, if you run the
  seed there) **needs to be committed** in your **private** deploy repository:
  a normal commit, or `git add -f` if you've gitignored them. Alternatively,
  provide the configuration through environment variables instead of a file.

In other words: we do **not** claim that `sources.yaml`/`seeds.yaml` "stay in
`.gitignore` and are never committed." In a **private** deploy repo they
typically **are** committed on purpose; what you avoid is exposing them in a
**public** repo. Only the **secrets** (`.env`) are the ones that are never
committed, under any circumstance.

## How to report a security issue

If you find a vulnerability or suspect credential exposure,
**do not open a public issue**. Report it privately through **GitHub
Security Advisories** (the *Security → Report a vulnerability* tab of the
repository), including:

- A description of the problem and its potential impact.
- Steps to reproduce, if applicable.
- Any information relevant to the fix.

We'll do our best to respond as quickly as possible and keep you
informed about the progress of the fix.
