# src/curation

This folder is the project's "judgment brain": it's where an LLM (Anthropic's
Haiku 4.5) decides what deserves to reach the feed and interprets what the owner
says in the chat. There are two separate tasks. (1) **Curation**: given a post's
text, return a structured verdict (approve/reject + category + summary). (2)
**Steering**: given a chat message without a link, figure out whether the owner
wants to steer the feed, recall something they already voted on, or adjust the
fresh/relevant mix. All via Structured Outputs (the model is forced to return an
object that validates against a Pydantic schema), and the curator runs with a
monthly spend ceiling. The instruction "personas" are written in English (rubric
and model prompts); the code and comments are in English.

## Files

- **`curator.py`** -> the swappable curator (the "judge" of each post).
  - `Curator` (ABC): the stable interface. Every implementation has
    `async def classify(post_text, similarity_signal=None, interests=None) -> Verdict | None`.
    Returns `None` on failure (model refusal, `max_tokens` overrun, empty
    parse); in those cases the caller marks an error and moves on. `interests` is
    the optional list of topics with `/focus` active: the runner forwards it so
    the curator becomes **aware of the owner's interests** (see `ACTIVE USER
    INTERESTS` in the rubric).
  - `make_curator(settings, *, spend_guard=None) -> Curator`: the curator
    **factory**. Picks the provider via `settings.curator_provider`
    (`CURATOR_PROVIDER`): default `anthropic` -> `AnthropicCurator` (Haiku);
    `kimi` -> `KimiCurator` (Moonshot). The rest of the pipeline doesn't change —
    the `Curator` interface is the same.
  - `AnthropicCurator`: the default implementation. Uses Haiku 4.5
    (`settings.curator_model`) via `client.messages.parse(..., output_format=Verdict)`.
    The `RUBRIC` (imported from `prompt.py`) goes into a **static and cached**
    system block (`cache_control: ephemeral`) — the prefix is identical on every
    call, so prompt caching kicks in and gets cheaper from the 2nd request
    onward. `max_tokens` is small (the verdict is tiny). NEVER passes `effort`
    (Haiku doesn't support it). Clamps `confidence` to the [0, 1] range. Exposes
    `model` (public, read by the runner via `curator_model_of()`).
  - `KimiCurator`: alternative implementation via Moonshot/Kimi (`settings.kimi_model`),
    selectable via `CURATOR_PROVIDER=kimi`. The API is OpenAI-compatible
    (`openai.AsyncOpenAI` with `base_url=settings.moonshot_base_url`; SDK
    imported lazily, only Kimi users need it). Requires `MOONSHOT_API_KEY`
    (clear error if missing). Since Kimi doesn't have `messages.parse`, it uses
    **JSON mode** (`response_format={"type": "json_object"}`) and **validates
    with Pydantic** (`Verdict.model_validate_json`); the system prefix is
    `RUBRIC + _KIMI_JSON_CONTRACT` (the contract describes the strict JSON
    expected, with `summary` in English). Runs with `temperature=1.0` (kimi-k2.6
    rejects 0). API/network failure, `finish_reason="length"`, empty content or
    invalid JSON become `None`. Reuses `RUBRIC`, `SpendGuard` and
    `BudgetExceeded`; also exposes `model` for the runner.
  - `SpendGuard`: spend accounting. Accumulates the estimated USD in a small JSON
    at `~/.cache/ai-news-aggregator/spend.json`, keyed by month
    (`YYYY-MM`). Thread-safe (lock), with atomic write (tmp + rename).
    `classify()` checks `is_over_budget()` BEFORE spending and, if over, raises
    `BudgetExceeded`; the spend is ALWAYS added after the call, even on failure
    (tokens were already charged). The runner can check `spent_this_month` and
    `is_over_budget()` before running. It is shared by both providers.
  - `estimate_cost_usd(usage)`: estimates the cost of an Anthropic response from
    `resp.usage`. Reads the fields defensively and charges input + cache
    (read and write) at the input price, and output at the output price. It is a
    **conservative** estimate (overestimating is safe: in the worst case we stop
    a bit short of the budget).
  - `estimate_kimi_cost_usd(usage)`: the equivalent for Kimi, reading the
    OpenAI-style `usage`. Uses `prompt_tokens_details.cached_tokens` when
    available (charges the cheaper cache hit); otherwise, charges all input at
    the cache miss price (conservative).
  - `BudgetExceeded(RuntimeError)`: raised when the month's spend reaches
    `settings.curator_monthly_budget_usd`.
  - **Swapping providers is the central point of the design**: the second
    provider (`KimiCurator`) already realizes the `DeepSeekCurator` sketch
    described in a note at the end of the file. Just another impl of `Curator`
    with the same signature, returning a `Verdict`; the rest of the pipeline
    (runner, db) doesn't change — only the injected `Curator`, chosen by
    `make_curator`. `SpendGuard`/`BudgetExceeded` are reused; only the prices and
    the `usage` reading are specific.

- **`prompt.py`** -> the curator's rubric and the user-message assembly.
  - `RUBRIC`: the static, cacheable system block. It is deliberately long
    (>= 4096 tokens, Haiku's cache floor) — built from `_CORE` (the "ruthless
    curator" persona, the 5 approve categories —
    `data_engineering`, `automation`, `autonomous_agents`,
    `advanced_frameworks`, `modern_architecture` — and the reject reasons:
    `basic_tutorial`, `corporate_hype`, `clickbait`, `off_topic`, `none`) plus
    `_EXAMPLES` (16 worked boundary examples). The padding is NOT filler: the
    examples calibrate the classifier AND push the prefix above the cache floor.
    Golden rule: nothing dynamic in `RUBRIC` — any byte that changes invalidates
    the cache. The `Verdict`'s `summary` ALWAYS comes out in English (all other
    fields stay as specified). The rubric carries two override sections:
    - `ACTIVE USER INTERESTS`: when the user turn lists topics the owner
      explicitly asked for (via `/focus`), **relax the bar** for genuinely and
      specifically on-topic items — APPROVE even funding rounds,
      business/market news and company moves that would normally be
      `corporate_hype`/`off_topic` (the owner WANTS this; it's signal, not
      noise). Still REJECTS empty hype/clickbait with no substance, and judges
      anything NOT related to a listed interest by the normal ruthless bar.
    - "source-aware bar" (`SOURCE-AWARE BAR`): posts from `github` get a more
      lenient criterion (the owner wants to discover trending repos);
      `reddit`/`twitter` (or no `SOURCE:` line) get the full ruthless bar.
    Strong approve signal = numbers/plans/postmortem/named mechanism;
    length is not depth; when in doubt, REJECT.
  - `build_user_message(raw_text, author, metadata, similarity_signal=None, interests=None)`:
    builds the user turn (the **volatile** part, which comes AFTER the cached
    prefix). Appends the optional `similarity_signal` (a hint from RAG/seeds,
    treated as advice, not an order), the `ACTIVE USER INTERESTS` line when there
    are `interests` (a list of topics, which triggers the bar relaxation in the
    rubric), the `author` and `metadata` hints. It's what `classify()` calls for
    each post.
  - `_format_metadata_hints(metadata)`: extracts only cheap, informative fields
    from the metadata (subreddit, score, title, url, like_count, etc.) as
    compact, deterministic JSON, without dumping the whole object.

- **`steering.py`** -> interprets chat messages from the feed owner.
  - `Steerer.parse(message) -> ChatIntent | None`: classifies the message into an
    intent, also via Haiku + Structured Outputs (`output_format=ChatIntent`).
    Uses the same model family as the curator (`settings.curator_model`) and only
    runs when the owner sends text WITHOUT a link. Never brings down the handler:
    catches exceptions and returns `None` on failure/refusal/empty message.
  - The four intents (`kind` in `ChatIntent`):
    - `"steer"` -> steer the feed for a while; fills `directives` (per
      `repos`/`news` bucket, a `topic` good for search in English, and `days`).
    - `"recall"` -> recall something he ALREADY received and voted on; fills
      `recall_query` and `recall_polarity` (`liked`/`disliked`/`any`).
    - `"balance"` -> adjust the fresh vs. relevant mix; fills
      `balance_bucket` (`repos`/`news`/`both`) and `balance_fresh` (fraction 0..1).
    - `"other"` -> small talk, a question, a thank-you.
  - `reply` in `ChatIntent` always comes out in English and short. Timeframe
    hygiene: `days <= 0` becomes `DEFAULT_FOCUS_DAYS` (14) and is capped at
    `MAX_FOCUS_DAYS` (60).
  - `Steerer.translate_to_en(text) -> str`: translates a short search query
    to English (the language the archive is embedded in) BEFORE embedding, which
    improves recall. Uses `messages.create` (not Structured Outputs) with the
    same model. If it's already English, returns it unchanged; on any failure,
    returns the original (never brings down the search).

## How it connects to the rest

- `Verdict`, `ChatIntent` (and their fields) come from `..common.models`;
  `Settings` (curator provider and model, API keys, Moonshot's `base_url`,
  budget) comes from `..common.config`.
- The runner injects the curator via `make_curator(settings)` (Haiku by default,
  or Kimi if `CURATOR_PROVIDER=kimi`) to judge posts; `Steerer` is what the
  chat handler uses to understand the owner. `AnthropicCurator` and `Steerer`
  depend on the `anthropic` SDK's `messages.parse` with a Pydantic schema as
  `output_format` (`Steerer` also uses `messages.create` in `translate_to_en`);
  `KimiCurator` uses the `openai` SDK with JSON mode + Pydantic validation.
