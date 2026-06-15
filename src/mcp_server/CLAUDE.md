# src/mcp_server — MCP server that opens the archive to Claude

This folder has an MCP server (FastMCP running on stdio) that exposes YOUR
curated archive as tools Claude can call. The idea is to ask directly in Claude
Code/Desktop ("what did I like about RAG?", "what's the active focus?") without
having to open Telegram. It's a THIN shell on top of the `Database` from
`src/common/db.py`: each tool just gets the vector for a query, calls the
corresponding database method and formats the result as text. Each person runs
THEIR OWN instance (their Supabase, their `telegram_user_id`), so the data never
mixes — it's the same multi-tenant isolation as the rest of the project.

## Files

- **`server.py`** — the entire server. Creates `FastMCP("acervo-ia")` and exposes
  3 tools. Details on how it connects to the rest:
  - **Lazy connection:** keeps `_db` and `_embedder` in module variables. The
    `_ensure()` helper connects to the database and creates the `Embedder` only on
    the first use of any tool, reusing the pool afterwards. It uses
    `load_settings()` / `load_sources()` from `src/common/config.py`, the `Database`
    from `src/common/db.py` and the `Embedder` from `src/common/embeddings.py`.
  - **Who "you" are:** `_resolve_telegram_id()` reads `TELEGRAM_USER_ID` from
    `.env`; if it doesn't exist, it takes the `telegram_user_id` of the 1st user in
    `config/sources.yaml`; if it finds none, it raises an error asking you to
    configure it. This telegram id is converted into the internal `user_id` via
    `db.get_or_create_user(...)`, and all searches are filtered by that user.
  - **`_headline(...)`** — a formatting-only helper: takes the 1st non-empty line of
    a text and cuts it at 160 chars, so each item becomes a short line.
  - **The 3 tools (each one is thin over 1 method of `Database`):**
    - `buscar_acervo(consulta, limite=10)` → embeds the query and calls
      `db.search_pool(...)`. Semantic search in the archive (approved + what you
      saved/liked); marks with ❤️ what was liked.
    - `lembrar_votos(consulta, voto="any", limite=10)` → calls
      `db.recall_voted(...)`. Recalls what YOU voted on about a topic. `voto`:
      `"liked"` (👍, becomes `1`), `"disliked"` (👎, becomes `-1`) or `"any"` (any
      vote, becomes `None`).
    - `ver_foco()` → calls `db.active_focus(...)` for the `repos` and `news` buckets
      and shows the active direction (`/foco`) of each (📦 repos / 🗞️ news).
  - **`main()`** — starts the server with `mcp.run()` (stdio, the default mode for
    Claude Code/Desktop). It's what runs when you do `python -m src.mcp_server.server`.

- **`__init__.py`** — empty, it just marks the folder as a Python package (so the
  `src.mcp_server.server` import works).

## How to plug it in

- **Via `.mcp.json`** (at the project root): there's already a server called
  `acervo` that runs `.venv/bin/python -m src.mcp_server.server`. Any Claude Code
  opened at the project root loads this automatically.
- **Manually:** `claude mcp add acervo -- .venv/bin/python -m src.mcp_server.server`.

In both cases, remember to have the project's env vars configured (e.g. the
`DATABASE_URL` that `load_settings()` reads, and `TELEGRAM_USER_ID` or a user in
`config/sources.yaml`), otherwise the 1st tool call fails in `_ensure()`.
