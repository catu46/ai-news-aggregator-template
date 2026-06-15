# src/curation

This folder is the project's "judgment brain": this is where an LLM (Anthropic's
Haiku 4.5) decides what deserves to reach the feed and interprets what the owner
says in the chat. There are two separate tasks. (1) **Curation**: given a post's
text, return a structured verdict (approve/reject + category + summary). (2)
**Steering**: given a chat message without a link, figure out whether the owner
wants to steer the feed, recall something they already voted on, or adjust the
fresh/relevant mix. All via Structured Outputs (the model is forced to return an
object that validates against a Pydantic schema), and the curator runs with a
monthly spend cap. The instruction "personas" are written in English (the rubric
and the model prompts); the code and the comments, in English.

## Files

- **`curator.py`** -> the swappable curator (the "judge" of each post).
  - `Curator` (ABC): the stable interface. Every implementation has
    `async def classify(post_text, similarity_signal=None) -> Verdict | None`.
    Returns `None` on failure (model refusal, `max_tokens` overflow, empty
    parse); in those cases the caller marks an error and moves on.
  - `AnthropicCurator`: the default implementation. Uses Haiku 4.5
    (`settings.curator_model`) via `client.messages.parse(..., output_format=Verdict)`.
    The `RUBRIC` (imported from `prompt.py`) goes into a **static and cached**
    system block (`cache_control: ephemeral`) — the prefix is identical on every
    call, so prompt caching kicks in and gets cheaper from the 2nd request
    onward. `max_tokens` is small (the verdict is tiny). NEVER passes `effort`
    (Haiku doesn't support it). Clamps `confidence` to the [0, 1] range.
  - `SpendGuard`: spend accounting. Accumulates the estimated USD in a small
    JSON at `~/.cache/ai-news-aggregator/spend.json`, keyed by month
    (`YYYY-MM`). Thread-safe (lock), with atomic writes (tmp + rename).
    `classify()` checks `is_over_budget()` BEFORE spending and, if over, raises
    `BudgetExceeded`; spend is ALWAYS added after the call, even on failure
    (tokens were already charged). The runner can check `spent_this_month` and
    `is_over_budget()` before running.
  - `estimate_cost_usd(usage)`: estimates the cost from `resp.usage`. Reads the
    fields defensively and charges input + cache (read and write) at the input
    price, and output at the output price. It is a **conservative** estimate
    (overestimating is safe: worst case we stop a little before the budget).
  - `BudgetExceeded(RuntimeError)`: raised when the month's spend reaches
    `settings.curator_monthly_budget_usd`.
  - **Swapping providers is the central design point**: there is a commented
    sketch of `DeepSeekCurator` at the end of the file. Just another impl of
    `Curator` with the same signature, returning a `Verdict`; the rest of the
    pipeline (runner, db) doesn't change — only the injected `Curator`.
    `SpendGuard`/`BudgetExceeded` are reused; only the prices and the `usage`
    reading would be specific.

- **`prompt.py`** -> the curator rubric and the user-message assembly.
  - `RUBRIC`: the static, cacheable system block. It is deliberately long
    (>= 4096 tokens, Haiku's cache floor) — assembled from `_CORE` (the
    "ruthless curator" persona, the 5 approval categories —
    `data_engineering`, `automation`, `autonomous_agents`,
    `advanced_frameworks`, `modern_architecture` — and the rejection reasons:
    `basic_tutorial`, `corporate_hype`, `clickbait`, `off_topic`, `none`) plus
    `_EXAMPLES` (16 worked boundary examples). The padding is NOT filler: the
    examples calibrate the classifier AND push the prefix above the cache floor.
    Golden rule: nothing dynamic in `RUBRIC` — any byte that changes invalidates
    the cache. It also includes a "source-aware bar": posts from `github` get a
    more lenient criterion; `reddit`/`twitter` get the full ruthless bar. Strong
    approval signal = numbers/plans/postmortem/named mechanism; length is not
    depth; when in doubt, REJECT.
  - `build_user_message(raw_text, author, metadata, similarity_signal=None)`:
    assembles the user turn (the **volatile** part, which comes AFTER the cached
    prefix). Attaches the optional `similarity_signal` (a RAG/seeds hint, treated
    as advice, not an order), the `author`, and `metadata` hints. It's what
    `AnthropicCurator.classify()` calls on each post.
  - `_format_metadata_hints(metadata)`: extracts only cheap, informative fields
    from the metadata (subreddit, score, title, url, like_count, etc.) as
    compact, deterministic JSON, without dumping the whole object.

- **`steering.py`** -> interprets chat messages from the feed owner.
  - `Steerer.parse(message) -> ChatIntent | None`: classifies the message into
    an intent, also via Haiku + Structured Outputs (`output_format=ChatIntent`).
    Uses the same model family as the curator (`settings.curator_model`) and only
    runs when the owner sends text WITHOUT a link. Never takes down the handler:
    catches exceptions and returns `None` on failure/refusal/empty message.
  - The four intents (`kind` in `ChatIntent`):
    - `"steer"` -> steer the feed for a while; fills `directives` (per
      `repos`/`news` bucket, a `topic` good for English search, and `days`).
    - `"recall"` -> recall something they ALREADY received and voted on; fills
      `recall_query` and `recall_polarity` (`liked`/`disliked`/`any`).
    - `"balance"` -> adjust the fresh vs. relevant mix; fills
      `balance_bucket` (`repos`/`news`/`both`) and `balance_fresh` (fraction 0..1).
    - `"other"` -> small talk, a question, a thank-you.
  - `reply` in `ChatIntent` always comes out short and in English. Timeframe
    hygiene: `days <= 0` becomes `DEFAULT_FOCUS_DAYS` (14) and is capped at
    `MAX_FOCUS_DAYS` (60).

## How it connects to the rest

- `Verdict`, `ChatIntent` (and their fields) come from `..common.models`;
  `Settings` (model, API key, budget) comes from `..common.config`.
- `AnthropicCurator` is what the runner injects to judge posts; `Steerer` is what
  the chat handler uses to understand the owner. Both depend on the `anthropic`
  SDK's `messages.parse` with a Pydantic schema as `output_format`.
