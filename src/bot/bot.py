"""Telegram bot (python-telegram-bot 22.8, long-polling).

Responsibilities of this module:
  - Lock the bot to a set of known telegram_user_ids (allowlist),
    loaded from config/sources.yaml. It is multi-user-ready: each update is
    resolved to the internal user_id via get_or_create_user.
  - Deliver approved, not-yet-delivered posts (deliver_pending), both
    via JobQueue (once a day, in 2 buckets) and on demand via /feed.
  - Record 👍/👎 votes coming from the inline buttons (CallbackQueryHandler).
  - Semantic search over the posts the user liked (/search).

Shared state (Database + Embedder + allowlist) lives in
application.bot_data, instantiated once at startup.

Run with:  python -m src.bot.bot
"""
from __future__ import annotations

import logging
import re
from datetime import time as dtime
from zoneinfo import ZoneInfo

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..common.config import Settings, load_settings, load_sources
from ..common.db import Database
from ..common.embeddings import Embedder
from ..common.models import IngestedPost
from ..curation.curator import make_curator
from ..curation.steering import Steerer
from ..pipeline import run_curation, run_embedding, run_ingestion

logger = logging.getLogger(__name__)

# How much of raw_text we show in each delivery card.
TEXT_PREVIEW_CHARS = 500
# Automatic digest: once a day at a FIXED TIME (DIGEST_HOUR/DIGEST_TZ) — a
# morning "mini-newspaper". /feed delivers on demand at any time.
# Interval of the pipeline job (ingest + embed + curate) running INSIDE the bot.
PIPELINE_INTERVAL_SECONDS = 30 * 60
# How many results semantic search returns.
SEARCH_LIMIT = 10
# Affinity: uses your 👍/👎 ONLY to RANK the feed (👎 sinks, 👍 rises).
# Nothing is hidden — all approved content ends up delivered, just in order.
MIN_VOTES_FOR_AFFINITY = 1        # turns ranking on starting from the 1st vote

# Daily digest in 2 buckets: (header, source platforms, cap per digest).
REPOS_PER_DIGEST = 5
NEWS_PER_DIGEST = 12
# (key, header, source platforms, cap per digest). The key matches
# focus.bucket — it's what links the "direction" (/focus) to the right bucket.
BUCKETS = [
    ("repos", "📦 TRENDING REPOS", ("github",), REPOS_PER_DIGEST),
    ("news", "🗞️ WHAT PEOPLE ARE TALKING ABOUT", ("reddit", "twitter"), NEWS_PER_DIGEST),
]
# Balances NEW vs RELEVANT: within each bucket's cap, this reserve goes
# to the NEWEST; the rest goes to the most relevant (affinity + focus). This way
# fresh content is never buried, and "old but relevant" also shows up.
FRESH_SLOTS = {"repos": 2, "news": 4}
# Bucket -> cap map (derived from BUCKETS), to compute the default freshness fraction.
_BUCKET_CAP = {key: cap for key, _h, _p, cap in BUCKETS}
# Freshness: never delivers anything published more than N days ago (keeps undated posts).
DELIVERY_MAX_AGE_DAYS = 30
# Auto-balancing: learns the new×relevant mix from YOUR votes. Only kicks in
# with enough signal and runs once a day (in the delivery job, NOT in /feed). Small
# step (EMA), so a manual adjustment via chat dominates for several days.
AUTO_BALANCE_MIN_VOTES = 6
AUTO_BALANCE_STEP = 0.15
AUTO_BALANCE_BOUNDS = (0.15, 0.60)
# Page reader (clean markdown, no auth) for the "paste a link" feature.
JINA_BASE = "https://r.jina.ai/"
URL_RE = re.compile(r"https?://[^\s]+")
# Char limit stored/embedded from a manually saved link.
MANUAL_MAX_CHARS = 8000

# Keys used in application.bot_data (avoids loose strings throughout the code).
KEY_DB = "db"
KEY_EMBEDDER = "embedder"
KEY_ALLOWED = "allowed_user_ids"          # set[int] of telegram_user_ids
KEY_USER_MAP = "tg_to_internal_user_id"   # dict[int, int] cache tg -> user_id
KEY_CURATOR = "curator"
KEY_SETTINGS = "settings"
KEY_STEERER = "steerer"


# --------------------------------------------------------------------------
# State / authorization helpers
# --------------------------------------------------------------------------
def _db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data[KEY_DB]


def _embedder(context: ContextTypes.DEFAULT_TYPE) -> Embedder:
    return context.application.bot_data[KEY_EMBEDDER]


def _is_allowed(context: ContextTypes.DEFAULT_TYPE, telegram_user_id: int | None) -> bool:
    """Locks the bot to the allowlist. No id (e.g. a channel) -> denied."""
    if telegram_user_id is None:
        return False
    allowed: set[int] = context.application.bot_data[KEY_ALLOWED]
    return telegram_user_id in allowed


async def _resolve_user_id(context: ContextTypes.DEFAULT_TYPE, telegram_user_id: int) -> int:
    """Resolves telegram_user_id -> internal user_id, cached in bot_data."""
    cache: dict[int, int] = context.application.bot_data[KEY_USER_MAP]
    cached = cache.get(telegram_user_id)
    if cached is not None:
        return cached
    user_id = await _db(context).get_or_create_user(telegram_user_id)
    cache[telegram_user_id] = user_id
    return user_id


def _truncate(text: str, limit: int = TEXT_PREVIEW_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _first_line(text: str) -> str:
    """First non-empty line — used in the summary of search results."""
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return (text or "").strip()


def _vote_keyboard(post_id: int) -> InlineKeyboardMarkup:
    """Two 👍/👎 buttons. callback_data 'up:<id>' / 'down:<id>' (<< 64 bytes)."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👍", callback_data=f"up:{post_id}"),
                InlineKeyboardButton("👎", callback_data=f"down:{post_id}"),
            ]
        ]
    )


def _registered_keyboard() -> InlineKeyboardMarkup:
    """Single, inert button shown after the vote is recorded."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ recorded", callback_data="noop")]]
    )


def _format_card(rec) -> str:
    """Delivery card, with a header tailored to the source (repo / tweet / reddit)."""
    plat = rec["source_platform"]
    meta = rec["metadata"] or {}
    summary = (rec["summary"] or "").strip() or _truncate(rec["raw_text"])
    if plat == "github":
        head = f"📦 {meta.get('full_name') or rec['author'] or '?'}  ·  ⭐ {meta.get('stars', '?')}"
    elif plat == "twitter":
        head = f"🐦 @{rec['author'] or '?'}"
    elif plat == "reddit":
        sub = meta.get("subreddit")
        head = f"👽 r/{sub}" if sub else f"📰 {rec['author'] or '?'}"
    else:
        head = f"📰 {rec['author'] or '?'}"
    parts = [head, "", summary]
    if rec["source_url"]:
        parts += ["", rec["source_url"]]
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Delivery of pending items
# --------------------------------------------------------------------------
async def deliver_pending(app: Application, tune: bool = False) -> int:
    """Delivers a digest in 2 buckets (📦 repos / 🗞️ news) per user.

    Each bucket is ranked by affinity WITHIN the bucket itself — your votes
    on repos don't affect news and vice versa — with a cap per digest. It never
    delivers anything published more than DELIVERY_MAX_AGE_DAYS days ago.

    `tune=True` (daily job) lets auto-balancing learn from votes before
    delivering; in `/feed` it stays False (doesn't shuffle the mix on every request).
    Returns the total number of cards sent.
    """
    db: Database = app.bot_data[KEY_DB]
    allowed: set[int] = app.bot_data[KEY_ALLOWED]
    cache: dict[int, int] = app.bot_data[KEY_USER_MAP]

    sent_total = 0
    for telegram_user_id in allowed:
        user_id = cache.get(telegram_user_id)
        if user_id is None:
            user_id = await db.get_or_create_user(telegram_user_id)
            cache[telegram_user_id] = user_id

        try:
            pending = await db.approved_undelivered(
                user_id, limit=300, max_age_days=DELIVERY_MAX_AGE_DAYS
            )
        except Exception:  # pragma: no cover - operational resilience
            logger.exception("Failed to fetch pending items for user_id=%s", user_id)
            continue

        for bucket_key, header, platforms, cap in BUCKETS:
            if tune:  # learns this bucket's new×relevant mix (once a day)
                await _auto_tune_balance(db, user_id, bucket_key, list(platforms))
            recs = [r for r in pending if r["source_platform"] in platforms]
            if recs:
                sent_total += await _deliver_bucket(
                    app, db, telegram_user_id, user_id,
                    bucket_key, header, list(platforms), cap, recs,
                )

    return sent_total


async def _auto_tune_balance(
    db: Database, user_id: int, bucket_key: str, platforms: list[str]
) -> None:
    """Learns the bucket's novelty fraction from YOUR votes.

    Cards with LOW affinity score got in via the FRESHNESS slot; if you
    like those, you enjoy discovering -> raise novelty; if you reject them, lower it.
    Small step (EMA), so a manual adjustment via chat still dominates for several days.
    Only acts with enough signal (>= AUTO_BALANCE_MIN_VOTES votes in the bucket) and
    when there's score separation (otherwise you can't tell novelty from
    relevance apart).
    """
    try:
        rows = await db.balance_signal(user_id, platforms)
    except Exception:  # pragma: no cover
        return
    if len(rows) < AUTO_BALANCE_MIN_VOTES:
        return
    scores = sorted(float(r["score"]) for r in rows)
    median = scores[len(scores) // 2]
    low = [r for r in rows if float(r["score"]) <= median]
    high = [r for r in rows if float(r["score"]) > median]
    if not low or not high:
        return
    like_low = sum(1 for r in low if r["vote"] == 1) / len(low)
    like_high = sum(1 for r in high if r["vote"] == 1) / len(high)
    pref = like_low - like_high                 # [-1,1]: + = likes novelty
    lo, hi = AUTO_BALANCE_BOUNDS
    target = max(lo, min(hi, 0.35 + 0.20 * pref))
    default_frac = FRESH_SLOTS.get(bucket_key, 0) / max(1, _BUCKET_CAP.get(bucket_key, 1))
    cur = await db.get_balance(user_id, bucket_key)
    cur = default_frac if cur is None else float(cur)
    if not (lo <= cur <= hi):
        return  # you set an extreme by hand -> respect it, don't auto-adjust
    new = max(lo, min(hi, cur + AUTO_BALANCE_STEP * (target - cur)))
    if abs(new - cur) >= 0.01:
        await db.set_balance(user_id, bucket_key, new)
        logger.info(
            "auto-balance %s: %.2f -> %.2f (target %.2f, %d votes)",
            bucket_key, cur, new, target, len(rows),
        )


def _pub_ts(rec) -> float:
    """Publication timestamp for ordering by freshness (0 when absent)."""
    pa = rec["published_at"]
    return pa.timestamp() if pa is not None else 0.0


def _focus_boost(emb, focuses) -> float:
    """How much the active direction (/focus) pulls this post up (0 = nothing).

    Vectors are L2-normalized -> cosine similarity = inner product.
    Works EVEN without votes: the direction re-ranks the feed on the spot.
    """
    boost = 0.0
    for f in focuses:
        femb = f["embedding"]
        if femb is None:
            continue
        sim = float(emb @ femb)  # cosine (normalized vectors)
        boost += float(f["weight"]) * max(0.0, sim)
    return boost


async def _deliver_bucket(
    app: Application, db: Database, telegram_user_id: int, user_id: int,
    bucket_key: str, header: str, platforms: list[str], cap: int, recs: list,
) -> int:
    """Ranks and delivers ONE bucket.

    Two signals add up in each card's score:
      - affinity: your 👍/👎 WITHIN this bucket (restricted to `platforms`);
      - direction (/focus): this bucket's active topic re-ranks toward it.
    Affinity here only RANKS (👍 rises, 👎 sinks); nothing is hidden.
    """
    likes, dislikes = await db.vote_counts(user_id, platforms=platforms)
    affinity_on = (likes + dislikes) >= MIN_VOTES_FOR_AFFINITY
    focuses = await db.active_focus(user_id, bucket_key)

    scored = []
    for rec in recs:
        score = 0.0
        emb = rec["embedding"]
        if emb is not None:
            # --- affinity (👍/👎 of this bucket): only RANKS, never hides ---
            if affinity_on:
                try:
                    neighbors = await db.nearest_votes(
                        user_id, emb, k=5, platforms=platforms
                    )
                except Exception:  # pragma: no cover
                    neighbors = []
                # nearby 👍 neighbors -> score +, nearby 👎 neighbors -> score -
                score += sum(
                    n["vote"] * max(0.0, 1.0 - float(n["dist"])) for n in neighbors
                )
            # --- active direction (/focus) ---
            if focuses:
                score += _focus_boost(emb, focuses)
        scored.append((rec, score))

    scored.sort(key=lambda t: t[1], reverse=True)
    score_by_id = {rec["id"]: score for rec, score in scored}

    # Splits the bucket's slots: most go to relevance (affinity + focus) and
    # a reserve goes to the NEWEST not yet chosen. The rest (relevant but
    # not delivered today) stays a candidate in the next digests.
    if focuses:  # with FOCO active: no freshness reserve -> everything by relevance
        fresh_quota = 0   # otherwise a new off-topic post jumps the /focus queue
    else:
        frac = await db.get_balance(user_id, bucket_key)
        if frac is None:  # no saved preference -> bucket's default reserve
            fresh_quota = min(FRESH_SLOTS.get(bucket_key, 0), cap)
        else:             # you adjusted the mix via chat (/balance)
            fresh_quota = min(round(max(0.0, min(1.0, frac)) * cap), cap)
    relevant_quota = cap - fresh_quota
    chosen, chosen_ids = [], set()
    for rec, _ in scored:                      # relevance slots
        if len(chosen) >= relevant_quota:
            break
        chosen.append(rec)
        chosen_ids.add(rec["id"])
    rest = [rec for rec, _ in scored if rec["id"] not in chosen_ids]
    rest.sort(key=_pub_ts, reverse=True)       # freshness slots: the newest
    for rec in rest:
        if len(chosen) >= cap:
            break
        chosen.append(rec)
        chosen_ids.add(rec["id"])

    # The header gets a note when there's an active direction in this bucket.
    if focuses:
        topics = ", ".join(f["topic"] for f in focuses[:3])
        header = f"{header}\n🎯 active focus: {topics}"

    sent = 0
    header_sent = False
    for rec in chosen:
        post_id = rec["id"]
        if not header_sent:
            try:
                await app.bot.send_message(chat_id=telegram_user_id, text=header)
            except Exception:  # pragma: no cover
                pass
            header_sent = True
        try:
            msg = await app.bot.send_message(
                chat_id=telegram_user_id,
                text=_format_card(rec),
                reply_markup=_vote_keyboard(post_id),
                parse_mode=None,
                link_preview_options=LinkPreviewOptions(is_disabled=False),
            )
        except Exception:  # pragma: no cover
            logger.exception(
                "Failed to send post_id=%s to tg=%s", post_id, telegram_user_id
            )
            continue
        await db.record_delivery(
            user_id=user_id, post_id=post_id,
            telegram_message_id=msg.message_id,
            affinity_score=score_by_id.get(post_id),
        )
        sent += 1
    return sent


async def _job_deliver(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback: runs deliver_pending periodically."""
    try:
        n = await deliver_pending(context.application, tune=True)
        if n:
            logger.info("Delivery job sent %d message(s).", n)
    except Exception:  # pragma: no cover
        logger.exception("Delivery job failed.")


async def _job_pipeline(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback: runs the pipeline (ingest -> embed -> curate) in the bot.

    This way the always-on bot does EVERYTHING — no need for a separate cron service
    on Railway. Failures of a run are isolated (logged, they don't take down the bot).
    """
    app = context.application
    try:
        await run_ingestion(app.bot_data[KEY_DB])
        await run_embedding(
            app.bot_data[KEY_DB],
            app.bot_data[KEY_EMBEDDER],
            app.bot_data[KEY_SETTINGS],
        )
        await run_curation(app.bot_data[KEY_DB], app.bot_data[KEY_CURATOR])
        logger.info("Pipeline job: cycle completed.")
    except Exception:  # pragma: no cover
        logger.exception("Pipeline job failed.")


# --------------------------------------------------------------------------
# Command handlers
# --------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _is_allowed(context, user.id if user else None):
        return
    await _resolve_user_id(context, user.id)  # ensures the user is registered
    await update.effective_message.reply_text(
        "Hi! Once a day I'll send you two buckets of AI news:\n"
        "📦 TRENDING REPOS (GitHub) and 🗞️ WHAT PEOPLE ARE TALKING ABOUT (X + Reddit).\n\n"
        "Commands:\n"
        "• /feed — fetch whatever is new right now\n"
        "• /run — run a cycle now (ingest + curate + deliver)\n"
        "• /search <query> — semantic search over your archive (❤️ = liked)\n"
        "• /focus — view/clear the feed's current direction\n"
        "• /mix — view the current new×relevant balance\n"
        "• just talk to me — steer (“for 3 days I want repos about RAG”) "
        "or ask (“what was that news about agents that I liked?”)\n"
        "• paste a link — I'll read the page and save it to your archive\n\n"
        "Use 👍/👎 on the cards: each bucket learns your taste separately."
    )


async def cmd_feed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fires deliver_pending on demand (delivers to ALL authorized users)."""
    user = update.effective_user
    if not _is_allowed(context, user.id if user else None):
        return
    await _resolve_user_id(context, user.id)
    n = await deliver_pending(context.application)
    if n == 0:
        await update.effective_message.reply_text("Nothing new for now. 🙂")


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/run: runs a FULL cycle now (ingest → embed → curate → deliver)."""
    user = update.effective_user
    if not _is_allowed(context, user.id if user else None):
        return
    bd = context.application.bot_data
    if bd.get("pipeline_running"):
        await update.effective_message.reply_text("⏳ A cycle is already running, hold on…")
        return
    bd["pipeline_running"] = True
    await update.effective_message.reply_text(
        "🏃 Running now: ingest → embed → curate → deliver…"
    )
    try:
        db = _db(context)
        n_new = await run_ingestion(db)
        await run_embedding(db, _embedder(context), bd[KEY_SETTINGS])
        n_cur = await run_curation(db, bd[KEY_CURATOR])
        n_sent = await deliver_pending(context.application)
    except Exception:  # pragma: no cover
        logger.exception("/run failed")
        await update.effective_message.reply_text("The cycle errored out. 😬 (check the logs)")
        return
    finally:
        bd["pipeline_running"] = False
    tail = "See above 👆" if n_sent else "Nothing new approved to deliver right now."
    await update.effective_message.reply_text(
        f"✅ Cycle completed: {n_new} ingested, {n_cur} curated, "
        f"{n_sent} delivered. {tail}"
    )


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/search <query>: embed the question + search the curated archive (❤️=liked)."""
    user = update.effective_user
    if not _is_allowed(context, user.id if user else None):
        return

    query_text = " ".join(context.args).strip() if context.args else ""
    if not query_text:
        await update.effective_message.reply_text(
            "Usage: /search <query>\nE.g.: /search autonomous agents with tools"
        )
        return

    user_id = await _resolve_user_id(context, user.id)
    try:
        # The archive is embedded in English -> translate the query before searching.
        # (Chat recall already arrives in English, from the Steerer's parser.)
        q_en = await context.application.bot_data[KEY_STEERER].translate_to_en(query_text)
        vec = await _embedder(context).embed_query(q_en)
        matches = await _db(context).search_pool(user_id, vec, limit=SEARCH_LIMIT)
    except Exception:  # pragma: no cover
        logger.exception("Search failed for tg=%s", user.id)
        await update.effective_message.reply_text("The search went wrong. Try again?")
        return

    if not matches:
        await update.effective_message.reply_text(
            "I didn't find anything in your curated archive for that query."
        )
        return

    lines = [f"🔎 Results for: {query_text}  (❤️ = you liked)", ""]
    for i, rec in enumerate(matches, start=1):
        headline = _first_line(rec["raw_text"])[:120]
        mark = " ❤️" if rec["liked"] else ""
        url = rec["source_url"] or ""
        lines.append(f"{i}.{mark} {headline}")
        if url:
            lines.append(f"   {url}")
    await update.effective_message.reply_text(
        "\n".join(lines),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def cmd_focus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/focus — shows the active focus. /focus clear — clears it. /focus <text> — steers."""
    user = update.effective_user
    if not _is_allowed(context, user.id if user else None):
        return
    arg = " ".join(context.args).strip() if context.args else ""
    user_id = await _resolve_user_id(context, user.id)
    db = _db(context)

    if arg.lower() in ("clear", "off", "reset"):
        n = await db.clear_focus(user_id)
        await update.effective_message.reply_text(
            f"🎯 Focus cleared ({n} removed)." if n else "There was no active focus."
        )
        return
    if arg:  # /focus <text> also steers (same path as natural chat)
        await _handle_chat(update, context, arg)
        return

    # No argument: show what's active.
    lines = ["🎯 Active focus:"]
    any_focus = False
    for bucket_key, lbl in (("repos", "📦 repos"), ("news", "🗞️ news")):
        for r in await db.active_focus(user_id, bucket_key):
            any_focus = True
            lines.append(f"• {lbl} → {r['topic']}")
    if not any_focus:
        await update.effective_message.reply_text(
            "No active focus. Tell me something like “for 3 days I want repos about RAG”."
        )
        return
    lines.append("\n/focus clear to clear it.")
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_mix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mix — shows each bucket's current new×relevant balance."""
    user = update.effective_user
    if not _is_allowed(context, user.id if user else None):
        return
    user_id = await _resolve_user_id(context, user.id)
    db = _db(context)
    label = {"repos": "📦 repos", "news": "🗞️ news"}
    lines = ["⚖️ Current mix (novelty / relevance):"]
    for bucket_key in ("repos", "news"):
        frac = await db.get_balance(user_id, bucket_key)
        if frac is None:  # no saved adjustment -> bucket's default reserve
            f = FRESH_SLOTS.get(bucket_key, 0) / max(1, _BUCKET_CAP.get(bucket_key, 1))
            tag = "default"
        else:
            f = float(frac)
            tag = "adjusted"
        pct = round(f * 100)
        lines.append(
            f"• {label[bucket_key]}: ~{pct}% novelty / {100 - pct}% relevance ({tag})"
        )
    lines.append(
        "\nThe bot auto-adjusts from your votes (starting at ~6 votes in the bucket). "
        "To change it by hand: just say it (e.g. “more novelty in the news”). To reset: “undo that”."
    )
    await update.effective_message.reply_text("\n".join(lines))


# --------------------------------------------------------------------------
# Save a pasted link (ACTIVE curation — without relying on 👍/👎)
# --------------------------------------------------------------------------
async def _fetch_readable(url: str) -> tuple[str, str | None]:
    """Reads a URL as clean markdown via Jina Reader. Returns (text, title)."""
    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        headers={"User-Agent": "ai-news-aggregator"},
    ) as client:
        resp = await client.get(JINA_BASE + url)
        resp.raise_for_status()
        text = resp.text
    title = None
    for line in text.splitlines()[:5]:
        if line.lower().startswith("title:"):
            title = line.split(":", 1)[1].strip() or None
            break
    return text, title


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Text WITHOUT a command -> routes: a link is saved to the archive; otherwise it's
    interpreted as 'feed direction' (/focus in natural language)."""
    user = update.effective_user
    if not _is_allowed(context, user.id if user else None):
        return
    text = update.effective_message.text or ""
    match = URL_RE.search(text)
    if match:
        await _save_link(update, context, match.group(0).rstrip(").,]}>\"'"))
    else:
        await _handle_chat(update, context, text)


_CHAT_HINT = (
    "Here's how I can help:\n"
    "• steer the feed — e.g.: “for 3 days I want repos about RAG and news "
    "about AI regulation”\n"
    "• recall what you voted on — e.g.: “what was that news about agents "
    "that I liked?”\n"
    "• or paste a link for me to save to the archive."
)


async def _handle_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    """Routes a text message: feed direction / vote recall / hint."""
    parser: Steerer = context.application.bot_data[KEY_STEERER]
    intent = await parser.parse(text)
    if intent is None:
        await update.effective_message.reply_text(_CHAT_HINT)
        return
    if intent.kind == "recall" and intent.recall_query.strip():
        await _do_recall(update, context, intent.recall_query, intent.recall_polarity)
        return
    if intent.kind == "balance":
        await _apply_balance(
            update, context, intent.balance_bucket,
            intent.balance_fresh, intent.balance_reset,
        )
        return
    if intent.kind == "steer" and intent.directives:
        await _apply_focus(update, context, intent.directives)
        return
    await update.effective_message.reply_text(intent.reply or _CHAT_HINT)


async def _apply_focus(
    update: Update, context: ContextTypes.DEFAULT_TYPE, directives
) -> None:
    """Embeds each topic and writes to `focus` (replaces the bucket's previous direction)."""
    user_id = await _resolve_user_id(context, update.effective_user.id)
    embedder = _embedder(context)
    db = _db(context)
    applied = []
    for d in directives:
        try:
            vec = await embedder.embed_query(d.topic)
            await db.set_focus(user_id, d.bucket, d.topic, vec, d.days)
            applied.append(d)
        except Exception:  # pragma: no cover
            logger.exception("steering: failed to apply focus %s/%s", d.bucket, d.topic)
    if not applied:
        await update.effective_message.reply_text("I tried to adjust the focus but it errored out. 😬")
        return

    label = {"repos": "📦 repos", "news": "🗞️ news"}
    lines = ["🎯 Focus updated:"]
    for d in applied:
        lines.append(f"• {label.get(d.bucket, d.bucket)} → {d.topic}  ({d.days} day(s))")
    lines.append("")
    lines.append(
        "I'll prioritize this in delivery and start fetching more on this topic.\n"
        "/focus shows what's active · /focus clear clears it."
    )
    await update.effective_message.reply_text("\n".join(lines))


async def _apply_balance(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    bucket: str, fresh: float, reset: bool = False,
) -> None:
    """Adjusts (or RESETS) the NEW vs RELEVANT mix of one (or both) bucket(s)."""
    user_id = await _resolve_user_id(context, update.effective_user.id)
    db = _db(context)
    buckets = ("repos", "news") if bucket == "both" else (bucket,)
    label = {"repos": "📦 repos", "news": "🗞️ news"}
    alvo = " and ".join(label.get(b, b) for b in buckets)
    if reset:  # "undo that"/"reset": clears the saved adjustment -> default
        for b in buckets:
            await db.clear_balance(user_id, b)
        await update.effective_message.reply_text(
            f"⚖️ Mix for {alvo} back to default."
        )
        return
    fresh = max(0.0, min(1.0, float(fresh)))
    for b in buckets:
        await db.set_balance(user_id, b, fresh)
    pct = round(fresh * 100)
    await update.effective_message.reply_text(
        f"⚖️ Mix adjusted for {alvo}: ~{pct}% novelty / {100 - pct}% relevance.\n"
        "Change it whenever you want, just say so."
    )


async def _do_recall(
    update: Update, context: ContextTypes.DEFAULT_TYPE, query: str, polarity: str
) -> None:
    """A generic question ('any') searches the WHOLE archive; 'liked'/'disliked'
    recalls only what you voted on. (The query already comes in English from the parser.)"""
    user_id = await _resolve_user_id(context, update.effective_user.id)
    db = _db(context)
    try:
        vec = await _embedder(context).embed_query(query)
        if polarity in ("liked", "disliked"):
            vote = 1 if polarity == "liked" else -1
            rows = await db.recall_voted(user_id, vec, vote=vote, limit=SEARCH_LIMIT)
            mode = "voted"
        else:  # 'any' -> the whole curated archive (not just your votes)
            rows = await db.search_pool(user_id, vec, limit=SEARCH_LIMIT)
            mode = "archive"
    except Exception:  # pragma: no cover
        logger.exception("recall failed for tg=%s", update.effective_user.id)
        await update.effective_message.reply_text("The search went wrong. Try again?")
        return

    if not rows:
        if mode == "voted":
            scope = "you liked" if polarity == "liked" else "you disliked"
            msg = f"I didn't find anything {scope} about “{query}”."
        else:
            msg = f"I didn't find anything in your archive about “{query}”."
        await update.effective_message.reply_text(msg)
        return

    if mode == "voted":
        title = "❤️ You liked" if polarity == "liked" else "👎 You disliked"
        lines = [f"{title} — about “{query}”:", ""]
        for i, r in enumerate(rows, start=1):
            mark = "❤️" if r["vote"] == 1 else "👎"
            head = _first_line(r["raw_text"])[:120]
            lines.append(f"{i}. {mark} {head}")
            if r["source_url"]:
                lines.append(f"   {r['source_url']}")
    else:  # archive (whole archive; ❤️ marks what you liked)
        lines = [f"🔎 In your archive — about “{query}” (❤️ = you liked):", ""]
        for i, r in enumerate(rows, start=1):
            mark = " ❤️" if r["liked"] else ""
            head = _first_line(r["raw_text"])[:120]
            lines.append(f"{i}.{mark} {head}")
            if r["source_url"]:
                lines.append(f"   {r['source_url']}")
    await update.effective_message.reply_text(
        "\n".join(lines),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def _save_link(
    update: Update, context: ContextTypes.DEFAULT_TYPE, url: str
) -> None:
    """Reads a URL via Jina, embeds it and stores it as a 'manual' post + 👍.

    Active curation: you save something without waiting for a card. The item enters the
    archive (origin='manual', vote +1) and starts showing up in /search.
    """
    user_id = await _resolve_user_id(context, update.effective_user.id)
    await update.effective_message.reply_text("🔗 Reading and saving the link…")

    try:
        content, title = await _fetch_readable(url)
    except Exception:  # pragma: no cover
        logger.exception("Failed to read link %s", url)
        await update.effective_message.reply_text("I couldn't read that link. 😕")
        return

    content = (content or "").strip()[:MANUAL_MAX_CHARS]
    if not content:
        await update.effective_message.reply_text("The link came back empty. 🤔")
        return

    db = _db(context)
    embedder = _embedder(context)
    post = IngestedPost(
        source_platform="manual",
        source_id=url,                 # dedup: re-pasting the same link doesn't duplicate
        source_url=url,
        raw_text=content,
        metadata={"via": "telegram_link", "title": title},
    )
    try:
        post_id = await db.upsert_post(post)
        if post_id is None:
            await update.effective_message.reply_text("That link was already in your archive. 👍")
            return
        vectors = await embedder.embed_documents([content])
        await db.set_embedding(post_id, vectors[0], embedder.model)
        await db.record_vote(user_id, post_id, vote=1, origin="manual")
    except Exception:  # pragma: no cover
        logger.exception("Failed to save link %s", url)
        await update.effective_message.reply_text("I tried to save it but it errored out. 😬")
        return

    await update.effective_message.reply_text(
        f"✅ Saved to your archive: {title or url}\nIt already shows up in /search."
    )


# --------------------------------------------------------------------------
# Vote handler (inline buttons)
# --------------------------------------------------------------------------
async def on_vote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles clicks on the 👍/👎 buttons and on 'noop' (✅ recorded)."""
    query = update.callback_query
    if query is None:
        return

    from_user = query.from_user
    if not _is_allowed(context, from_user.id if from_user else None):
        # Answer the callback so the spinner doesn't keep turning, but ignore the action.
        await query.answer("Not authorized.", show_alert=False)
        return

    data = query.data or ""

    # 'noop': click on the already-recorded button. Just confirm and exit.
    if data == "noop":
        await query.answer("Already recorded ✅")
        return

    # Expected: 'up:<post_id>' or 'down:<post_id>'.
    try:
        action, raw_id = data.split(":", 1)
        post_id = int(raw_id)
        vote = {"up": 1, "down": -1}[action]
    except (ValueError, KeyError):
        await query.answer("Invalid action.", show_alert=False)
        return

    await query.answer()  # dismisses the button spinner immediately

    user_id = await _resolve_user_id(context, from_user.id)
    message_id = query.message.message_id if query.message else None
    await _db(context).record_vote(
        user_id=user_id,
        post_id=post_id,
        vote=vote,
        origin="telegram",
        telegram_message_id=message_id,
    )

    # Replaces the 👍/👎 with a single inert button, confirming the record.
    try:
        await query.edit_message_reply_markup(reply_markup=_registered_keyboard())
    except BadRequest:
        # Old/identical/inaccessible message — vote was already recorded, fine to ignore.
        logger.debug("Couldn't edit the keyboard for post_id=%s", post_id)


# --------------------------------------------------------------------------
# Application construction / bootstrap
# --------------------------------------------------------------------------
async def _post_init(app: Application) -> None:
    """Runs after the loop starts: registers each allowlist user in the DB."""
    db: Database = app.bot_data[KEY_DB]
    allowed: set[int] = app.bot_data[KEY_ALLOWED]
    cache: dict[int, int] = app.bot_data[KEY_USER_MAP]

    sources = load_sources()
    display_by_tg = {s.telegram_user_id: s.display_name for s in sources}

    for telegram_user_id in allowed:
        user_id = await db.get_or_create_user(
            telegram_user_id, display_by_tg.get(telegram_user_id)
        )
        cache[telegram_user_id] = user_id

    logger.info("Bot locked to %d user(s): %s", len(allowed), sorted(allowed))


def build_application(
    settings: Settings,
    db: Database,
    embedder: Embedder,
    *,
    connect_db: bool = True,
) -> Application:
    """Assembles the Application with handlers, delivery job and shared state.

    The allowlist is derived from sources.yaml; registering each user in the DB
    happens in post_init (already inside the loop managed by run_polling).

    connect_db=True (default): post_init connects the pool and post_shutdown
    closes it — this way main() doesn't need to manage the loop manually. Pass False if
    the caller already controls connect()/close() externally.
    """
    sources = load_sources()
    allowed = {s.telegram_user_id for s in sources}

    async def _on_startup(application: Application) -> None:
        if connect_db:
            await db.connect()
        await _post_init(application)

    async def _on_shutdown(_: Application) -> None:
        if connect_db:
            await db.close()

    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(_on_startup)
        .post_shutdown(_on_shutdown)
        .build()
    )

    # Shared state: a single instance of Database and Embedder.
    app.bot_data[KEY_DB] = db
    app.bot_data[KEY_EMBEDDER] = embedder
    app.bot_data[KEY_ALLOWED] = allowed
    app.bot_data[KEY_USER_MAP] = {}
    app.bot_data[KEY_CURATOR] = make_curator(settings)
    app.bot_data[KEY_SETTINGS] = settings
    app.bot_data[KEY_STEERER] = Steerer(settings)

    # Handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("feed", cmd_feed))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("focus", cmd_focus))
    app.add_handler(CommandHandler("mix", cmd_mix))
    app.add_handler(CallbackQueryHandler(on_vote))  # covers up:/down:/noop
    # Text without a command: link -> save to archive; otherwise -> feed direction (/focus).
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Periodic delivery via JobQueue (requires python-telegram-bot[job-queue]).
    if app.job_queue is not None:
        try:
            tz = ZoneInfo(settings.digest_tz)
        except Exception:  # pragma: no cover - invalid tz -> UTC
            tz = ZoneInfo("UTC")
        app.job_queue.run_daily(
            _job_deliver,
            time=dtime(hour=settings.digest_hour, minute=0, tzinfo=tz),
            name="deliver_pending",
        )
        app.job_queue.run_repeating(
            _job_pipeline,
            interval=PIPELINE_INTERVAL_SECONDS,
            first=30,  # first ingestion shortly after startup
            name="pipeline",
        )
    else:  # pragma: no cover
        logger.warning(
            "JobQueue unavailable (install python-telegram-bot[job-queue]); "
            "automatic delivery disabled — use /feed."
        )

    return app


def main() -> None:
    """Assembles the Application and runs long-polling.

    run_polling manages the asyncio loop's lifecycle internally; the DB's
    connect()/close() is done in the post_init/post_shutdown hooks
    configured in build_application(connect_db=True).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = load_settings()
    db = Database(settings.database_url)
    embedder = Embedder(settings)

    app = build_application(settings, db, embedder, connect_db=True)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
