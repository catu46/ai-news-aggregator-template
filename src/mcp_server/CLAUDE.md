# src/mcp_server — MCP server that opens the archive to Claude

This folder holds an MCP server (FastMCP running over stdio) that exposes YOUR
curated archive as tools Claude can call. The idea is to ask straight from
Claude Code/Desktop ("what did I like about RAG?", "what's the active focus?")
without having to open Telegram. It's a THIN shell over the `Database` in
`src/common/db.py`: each tool just takes the vector of a query, calls the
corresponding database method, and formats the result as text. Each person runs
THEIR OWN instance (their Supabase, their `telegram_user_id`), so the data never
mixes — it's the same multi-tenant isolation as the rest of the project.

## Files

- **`server.py`** — the whole server. Creates `FastMCP("archive-ia")` and exposes 3
  tools. Details on how it wires into the rest:
  - **Lazy connection:** keeps `_db` and `_embedder` in module-level variables. The
    `_ensure()` helper connects to the database and creates the `Embedder` only on
    the first use of any tool, reusing the pool afterwards. It uses
    `load_settings()` / `load_sources()` from `src/common/config.py`, the `Database`
    from `src/common/db.py`, and the `Embedder` from `src/common/embeddings.py`.
  - **Who "you" are:** `_resolve_telegram_id()` reads `TELEGRAM_USER_ID` from `.env`;
    if it's missing, it takes the `telegram_user_id` of the 1st user in
    `config/sources.yaml`; if it finds none, it raises an error asking you to
    configure one. That telegram id is converted into the internal `user_id` via
    `db.get_or_create_user(...)`, and every search is filtered by that user.
  - **`_headline(...)`** — a formatting-only helper: takes the 1st non-empty line of
    a text and trims it to 160 chars, so each item becomes a short single line.
  - **The 3 tools (each one is thin over 1 `Database` method):**
    - `search_archive(query, limit=10)` → embeds the query and calls
      `db.search_pool(...)`. Semantic search over the archive (approved + what you
      saved/liked); marks liked items with ❤️.
    - `recall_votes(query, vote="any", limit=10)` → calls
      `db.recall_voted(...)`. Recalls what YOU voted on about a topic. `vote`:
      `"liked"` (👍, becomes `1`), `"disliked"` (👎, becomes `-1`) or `"any"` (any
      vote, becomes `None`).
    - `see_focus()` → calls `db.active_focus(...)` for the `repos` and `news` buckets
      and shows the active direction (`/focus`) of each one (📦 repos / 🗞️ news).
  - **`main()`** — starts the server with `mcp.run()` (stdio, the default mode for
    Claude Code/Desktop). It's what runs when you do `python -m src.mcp_server.server`.

- **`__init__.py`** — empty, just marks the folder as a Python package (so the
  `src.mcp_server.server` import works).

## How to plug it in

- **Via `.mcp.json`** (at the project root): there's already a server called
  `archive` that runs `.venv/bin/python -m src.mcp_server.server`. Any Claude
  Code opened at the project root loads it automatically.
- **Manually:** `claude mcp add archive -- .venv/bin/python -m src.mcp_server.server`.

In both cases, remember to have the project's envs configured (e.g. the
`DATABASE_URL` that `load_settings()` reads, and `TELEGRAM_USER_ID` or a user in
`config/sources.yaml`), otherwise the 1st tool call fails in `_ensure()`.
