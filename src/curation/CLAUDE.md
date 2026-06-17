# src/curation

This folder is the project's "judgment brain": it's where an LLM (Anthropic's
Haiku 4.5) decides what deserves to reach the feed and interprets what the owner
says in chat. These are two separate tasks. (1) **Curation**: given a post's
text, return a structured verdict (approve/reject + category + summary). (2)
**Steering**: given a chat message without a link, figure out whether the owner
wants to steer the feed, search the archive for content on a topic, or adjust
the fresh/relevant mix. All via Structured Outputs (the model is forced to
return an object that validates against a Pydantic schema), and the curator runs
with a monthly spend ceiling. The instruction "personas" are written in English
(the rubric and the model's prompts).

## Files

- **`curator.py`** -> the swappable curator (the "judge" of each post).
  - `Curator` (ABC): the stable interface. Every implementation has
    `async def classify(post_text, similarity_signal=None, interests=None) -> Verdict | None`.
    Returns `None` on failure (model refusal, `max_tokens` overflow, empty
    parse); in those cases the caller marks an error and moves on. `interests` is
    the optional list of topics with `/focus` active: the runner passes it through
    so the curator becomes **aware of the owner's interests** (see
    `ACTIVE USER INTERESTS` in the rubric).
  - `make_curator(settings, *, spend_guard=None) -> Curator`: the curator
    **factory**. Picks the provider via `settings.curator_provider`
    (`CURATOR_PROVIDER`): default `anthropic` -> `AnthropicCurator` (Haiku);
    `kimi` -> `KimiCurator` (Moonshot). The rest of the pipeline doesn't change —
    the `Curator` interface is the same.
  - `AnthropicCurator`: the default implementation. Uses Haiku 4.5
    (`settings.curator_model`) via `client.messages.parse(..., output_format=Verdict)`.
    The `RUBRIC` (imported from `prompt.py`) goes in a **static, cached** system
    block (`cache_control: ephemeral`) — the prefix is identical on every call,
    so prompt caching kicks in and gets cheaper from the 2nd request onward.
    `max_tokens` is small (the verdict is tiny). It NEVER passes `effort` (Haiku
    doesn't support it). It sanitizes `confidence` to the [0, 1] range. It
    exposes `model` (public, read by the runner via `curator_model_of()`).
  - `KimiCurator`: alternative implementation via Moonshot/Kimi (`settings.kimi_model`),
    selectable via `CURATOR_PROVIDER=kimi`. The API is OpenAI-compatible
    (`openai.AsyncOpenAI` with `base_url=settings.moonshot_base_url`; the SDK is
    imported lazily, only Kimi users need it). It requires
    `MOONSHOT_API_KEY` (a clear error if absent). Since Kimi has no
    `messages.parse`, it uses **JSON mode** (`response_format={"type": "json_object"}`)
    and **validates with Pydantic** (`Verdict.model_validate_json`); the system
    prefix is `RUBRIC + _KIMI_JSON_CONTRACT` (the contract describes the strict
    JSON expected, with `summary` in English). It runs with `temperature=1.0`
    (kimi-k2.6 rejects 0). An API/network failure, `finish_reason="length"`,
    empty content, or invalid JSON all become `None`. It reuses `RUBRIC`,
    `SpendGuard` and `BudgetExceeded`; it also exposes `model` for the runner.
  - `SpendGuard`: spend accounting. Accumulates the estimated USD in a small
    JSON at `~/.cache/ai-news-aggregator/spend.json`, keyed by month
    (`YYYY-MM`). Thread-safe (lock), with atomic writes (tmp + rename).
    `classify()` checks `is_over_budget()` BEFORE spending and, if over, raises
    `BudgetExceeded`; the spend is ALWAYS added after the call, even on failure
    (tokens were already billed). The runner can check `spent_this_month` and
    `is_over_budget()` before running. It is shared by both providers.
  - `estimate_cost_usd(usage)`: estimates the cost of an Anthropic response from
    `resp.usage`. It reads the fields defensively and charges input + cache
    (read and write) at the input price, and output at the output price. It's a
    **conservative** estimate (overestimating is safe: worst case we stop a
    little before the budget).
  - `estimate_kimi_cost_usd(usage)`: the equivalent for Kimi, reading the
    OpenAI-style `usage`. It uses `prompt_tokens_details.cached_tokens` when
    available (charges cache hit cheaper); otherwise, it charges all input at the
    cache miss price (conservative).
  - `BudgetExceeded(RuntimeError)`: raised when the month's spend reaches
    `settings.curator_monthly_budget_usd`.
  - **Swapping providers is the central design point**: the second provider
    (`KimiCurator`) already realizes the `DeepSeekCurator` sketch described in a
    note at the end of the file. Just another `Curator` impl with the same
    signature, returning a `Verdict`; the rest of the pipeline (runner, db)
    doesn't change — only the injected `Curator`, chosen by `make_curator`.
    `SpendGuard`/`BudgetExceeded` are reused; only the prices and the `usage`
    reading are provider-specific.

- **`prompt.py`** -> the curator's rubric and the user-message assembly.
  - `RUBRIC`: the static, cacheable system block. It is deliberately long
    (>= 4096 tokens, Haiku's cache floor) — assembled from `_CORE` (the
    "ruthless curator" persona, the 5 approval categories —
    `data_engineering`, `automation`, `autonomous_agents`,
    `advanced_frameworks`, `modern_architecture` — and the rejection reasons:
    `basic_tutorial`, `corporate_hype`, `clickbait`, `off_topic`, `none`) plus
    `_EXAMPLES` (16 worked boundary examples). The padding is NOT filler: the
    examples calibrate the classifier AND push the prefix above the cache floor.
    Golden rule: nothing dynamic in `RUBRIC` — any byte that changes invalidates
    the cache. The `Verdict`'s `summary` is ALWAYS in English (all other fields
    stay as specified). The rubric carries two override sections:
    - `ACTIVE USER INTERESTS`: when the user turn lists topics the owner
      explicitly asked for (via `/focus`), it **relaxes the bar** for genuinely
      and specifically on-topic items — it APPROVES even funding rounds,
      business/market news, and company moves that would normally be
      `corporate_hype`/`off_topic` (the owner WANTS this; it's signal, not
      noise). It still REJECTS empty hype/clickbait with no substance, and judges
      anything NOT related to a listed interest by the normal ruthless bar.
    - "source-aware bar" (`SOURCE-AWARE BAR`): posts from `github` get a more
      lenient criterion (the owner wants to discover trending repos);
      `reddit`/`twitter` (or no `SOURCE:` line) get the full ruthless bar.
    Strong approval signal = numbers/plans/postmortem/named mechanism; length is
    not depth; when in doubt, REJECT.
  - `build_user_message(raw_text, author, metadata, similarity_signal=None, interests=None)`:
    assembles the user turn (the **volatile** part, which comes AFTER the cached
    prefix). It appends the optional `similarity_signal` (a RAG/seeds hint,
    treated as advice, not an order), the `ACTIVE USER INTERESTS` line when there
    are `interests` (a list of topics, which triggers the bar relaxation in the
    rubric), the `author`, and `metadata` hints. It's what `classify()` calls for
    each post.
  - `_format_metadata_hints(metadata)`: extracts only the cheap, informative
    fields from the metadata (subreddit, score, title, url, like_count, etc.) as
    compact, deterministic JSON, without dumping the whole object.

- **`steering.py`** -> interprets chat messages from the feed's owner.
  - `Steerer.parse(message) -> ChatIntent | None`: classifies the message into an
    intent, also via Haiku + Structured Outputs (`output_format=ChatIntent`).
    It uses the same model family as the curator (`settings.curator_model`) and
    only runs when the owner sends text WITHOUT a link. It never takes down the
    handler: it catches exceptions and returns `None` on failure/refusal/empty
    message.
  - The five intents (`kind` in `ChatIntent`):
    - `"steer"` -> steer the feed for a while; fills `directives` (by bucket
      `repos`/`news`, a `topic` good for search in English, and `days`).
    - `"recall"` -> FIND archive content on a topic; fills
      `recall_query` (the topic, in English for technical topics) and `recall_polarity`.
      The intent covers both what he already voted on and a **GENERAL question
      about a topic** (e.g. "any news about AI venture capital?", "bring me new
      stuff about agents"). The `recall_polarity`:
      - `"any"` is the COMMON case — searches the **whole archive**, not just the
        votes. "New news about X" falls here.
      - `"liked"` ONLY when the owner explicitly says he LIKED / tapped 👍;
        `"disliked"` ONLY when he explicitly says he did NOT like / tapped 👎.
    - `"balance"` -> adjust the fresh vs. relevant mix; fills
      `balance_bucket` (`repos`/`news`/`both`), `balance_fresh` (fraction 0..1) and
      `balance_reset` (bool). It's only `balance` when there is an **explicit
      fresh-vs-relevant COMPARISON** (or a mention of "too old" / "mix" / "balance"):
      "I want MORE fresh than relevant", "stuff coming in is TOO OLD", "focus on
      what's RELEVANT to me". Asking for CONTENT without comparing is NOT balance —
      "new news", "bring me what's new", "I want what's new about X" are recall
      `any` (or `other`); a plain "New news!" (no comparison, no topic) does NOT
      become an adjustment. `balance_reset=true` when he wants to GO BACK TO
      DEFAULT / UNDO / RESET the mix ("revert", "reset the mix", "leave it at the
      default", "undo this", "cancel that adjustment"); in that case NO fraction
      is invented — the app clears the adjustment and returns to the default.
      `balance_fresh` maps the wording ("only fresh"≈0.9, "more fresh"≈0.6,
      "half-and-half"=0.5, "more relevant"≈0.25, "only the relevant"≈0.1).
    - `"status"` -> QUERY the current state of the feed, without changing anything
      ("what's in focus?", "which topics are active?", "how's my feed?", "what's
      the mix now?"); fills `status_about` (`focus` = direction/focus only,
      `balance` = the new×relevant mix only, `both` = a general question about the
      feed). Do NOT confuse with `steer` (which CHANGES the focus) nor `recall`
      (which searches CONTENT). The bot replies by reading the REAL state (the
      `focus` table + `settings->balance`).
    - `"other"` -> small talk, a question, a thank-you.
  - `reply` in `ChatIntent` is always in English and short: on `steer` it
    confirms what it understood; on `recall` it says it'll look it up; on
    `balance` it confirms the new mix; on `status` it stays empty/short (the app
    shows the real focus/mix); on `other` it gives one line of guidance.
    It **never** claims to have EXECUTED an action the model doesn't control — on
    `other` it ONLY guides/clarifies: it is FORBIDDEN to say "done", "reverted",
    "reset", "undone", "cancelled" (the app does the executing, not the model) —
    no false confirmations. A request to undo/reset the mix is `balance` (with
    `balance_reset=true`), not `other`. Unused fields stay at their defaults
    (`directives=[]`, `recall_query=""`, `recall_polarity="any"`,
    `balance_bucket="both"`, `balance_fresh=0.4`, `balance_reset=false`,
    `status_about="both"`). Timeframe hygiene: `days <= 0` becomes
    `DEFAULT_FOCUS_DAYS` (14) and is capped at `MAX_FOCUS_DAYS` (60).
  - `Steerer.translate_to_en(text) -> str`: translates a short search query to
    English (the language the archive is embedded in) BEFORE embedding, which
    improves recall. It uses `messages.create` (not Structured Outputs) with the
    same model. If it's already English, it returns it unchanged; on any failure,
    it returns the original (never takes down the search).

## How it connects to the rest

- `Verdict`, `ChatIntent` (and their fields) come from `..common.models`;
  `Settings` (the curator's provider and model, API keys, Moonshot's `base_url`,
  budget) comes from `..common.config`.
- The runner injects the curator via `make_curator(settings)` (Haiku by default,
  or Kimi if `CURATOR_PROVIDER=kimi`) to judge posts; `Steerer` is what the chat
  handler uses to understand the owner. `AnthropicCurator` and `Steerer` depend
  on the `anthropic` SDK's `messages.parse` with a Pydantic schema as
  `output_format` (`Steerer` also uses `messages.create` in `translate_to_en`);
  `KimiCurator` uses the `openai` SDK with JSON mode + Pydantic validation.
