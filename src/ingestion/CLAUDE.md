# src/ingestion — Post collection layer

This folder is the project's **entry point**: each source (Reddit, GitHub, X) knows how
to fetch recent content "out there" and return it all in the same normalized format,
an `IngestedPost` (defined in `../common/models.py`). Every source implements the same
interface (`IngestionSource.fetch()`), so the rest of the system (the runner, the
curator) doesn't need to know where the post came from. Deduplication by
`(source_platform, source_id)` is the **database's** responsibility, not the sources' —
they can return duplicates freely. Each source also isolates its own failures (one
query/account breaking won't take down the others or the other sources).

Besides the fixed targets in `config/sources.yaml`, `pipeline.run_ingestion` **injects the
active `/focus` topics** straight into collection: **news** topics become searches on Reddit
(`searches` param) and X (added to its `searches`), and **repos** topics become queries on
GitHub. In other words, changing the focus in chat actually PULLS in new content, it doesn't
just reorder delivery.

## The contract: `IngestedPost`

Every `fetch()` returns a `list[IngestedPost]`. The relevant fields:
- `source_platform` — `"reddit"`, `"twitter"`, `"github"`, `"seed"` or `"manual"`.
- `source_id` — the platform's native id; it's the **dedup key** in the database.
- `source_url`, `raw_text` (title + body, this is what becomes the embedding and goes to the
  curator), `author`, `published_at` (datetime, whenever it can be extracted), `metadata`
  (free-form dict per platform).

## Files

- **`base.py`** → Defines the `IngestionSource` interface (abstract class). It has a `name`
  attribute and the abstract method `async fetch() -> list[IngestedPost]`. This is the only
  contract a source must fulfill. Every source inherits from it.

- **`__init__.py`** → Re-exports `IngestionSource`, `RedditSource`, `GitHubSource`, `XSource`.
  This is where the runner imports the sources from. When adding a new source, register it here.

- **`reddit_source.py`** (`RedditSource`) → Collects posts via Reddit's **public RSS/Atom
  feed**, **without authentication**, along two axes. (1) **Fixed subs:** the *new* feed of the
  subreddits from the constructor (`/r/<sub>/new/.rss`), using the *multireddit* trick
  (`sub1+sub2+...`) to grab all the subs in a single request (lower chance of rate-limiting).
  (2) **TOPIC search** (`searches` param — e.g. the `/focus` topics): for each topic, it hits
  `/search.rss` in **four variants** — `top` over the day, week, and month windows, plus `hot` —,
  pairing relevance by upvotes across multiple time scales with freshness. Each variant is a
  separate request and the **database dedups** the overlap (the RSS doesn't carry the score,
  since Reddit sorts server-side). It does a GET with `httpx` (async), parses the Atom with
  `feedparser`, and extracts the body text (which comes as HTML) with `BeautifulSoup`. Each feed
  is fetched in isolation (`_fetch_feed`): a feed that fails/throttles becomes an empty list and
  doesn't take down the rest. **Filters real posts only:** it keeps only entries whose link
  contains `/comments/` (search sometimes returns communities, not posts). Key points: Reddit's
  `.json` endpoint started returning 403 (Jun 2026), hence the `.rss`; the `source_id` comes from
  the Atom id (`t3_abc123` → `abc123`); the subreddit is discovered from each item's link (the
  multireddit feed mixes the subs together). *Documented caveat:* a datacenter IP (e.g. Railway)
  may get blocked — if that happens, you can swap in an authenticated backend behind the same
  interface. Constructor takes: `subreddits`, `user_agent`, `limit`, and `searches` (optional).
  If there are neither subs nor searches, `fetch()` returns empty.

- **`github_source.py`** (`GitHubSource`) → Fetches **trending repos by topic** via GitHub's
  **public Search API**. For each query/topic, it fetches repos *created recently*
  (`created:>=<date>`) with a minimum number of stars, sorted by stars (a proxy for "a new repo
  that's already taking off"). For each repo found, it also fetches the **README** as raw text
  (best-effort, truncated at 5000 chars) — so the embedding and the curator have real content.
  **Auth optional but recommended:** without `GITHUB_TOKEN` the README read limit is 60 req/h;
  with a token, 5000/h. A query's failure is isolated (it carries on with the others); local
  dedup by repo id within the same fetch. Constructor: `queries`, `token` (optional), and
  keyword-only `per_query`, `recent_days`, `min_stars`. Note that `source_platform` is `"github"`.

- **`x_source.py`** (`XSource`) → Collects from **X/Twitter** in free mode (cookie-based),
  calling the external `twitter` CLI (twitter-cli) via **subprocess** (`asyncio.create_subprocess_exec`),
  always with `--json`. Two modes: `user-posts <handle>` (accounts you follow) and
  `search "<query>"` (searches — e.g. the `/focus` topics). **Each search runs in TWO tabs:**
  `-t Latest` (newest) and `-t Top` (most engaged), pairing freshness and relevance; dedup by id
  (`seen`, via `_collect`) covers the overlap between the tabs and between searches and accounts.
  **Cookie-based auth:** the CLI reads `TWITTER_AUTH_TOKEN` and `TWITTER_CT0`; when passed to the
  constructor, they're injected into the subprocess `env` (to run headless on Railway); on the
  Mac, the CLI uses the logged-in browser's cookies. The `twitter` binary is resolved as
  PATH → venv bin → `~/.local/bin`. Robustness: `stdin=DEVNULL` (never hangs waiting for input),
  timeout, and every failure (timeout, non-JSON, returncode != 0, `ok=false`) becomes an empty
  list with a log, without taking down collection. Includes the quoted tweet in `raw_text` as
  context for the curator. Constructor: `accounts`, `searches`, `auth_token`, `ct0`, and
  keyword-only `per_account`, `per_search`, `timeout`. Note that `source_platform` is `"twitter"`
  (not `"x"`).

## How to add a new source

1. Create `<name>_source.py` with a class that **inherits from `IngestionSource`** (from
   `base.py`), defines `name`, and implements `async def fetch(self) -> list[IngestedPost]`.
2. Inside `fetch()`, build each post as an `IngestedPost` (imported from
   `../common/models.py`), filling in at least `source_platform`, `source_id`,
   `source_url`, and `raw_text`. Don't worry about duplicates (the database dedups) and
   **isolate your own failures** (don't let a network error take down the whole collection).
3. If it's a new platform, add the value to `source_platform` in the
   `Literal[...]` of `IngestedPost` in `../common/models.py`.
4. Re-export the class in `__init__.py` and register/instantiate the source where the runner
   builds the list of sources.
