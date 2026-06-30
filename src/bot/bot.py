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
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

import numpy as np
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
from ..common.recall import semantic_recall
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
# POPULARITY/importance bonus in delivery: score += WEIGHT * log10(engagement).
# A big launch (e.g. GPT-5.6) comes with LOTS of engagement even when it doesn't
# match your affinity — this signal lifts it. All in log10 so a viral item
# doesn't steamroll affinity/focus; it MIXES with them (doesn't replace). Per
# platform:
#   github : STAR_WEIGHT  * log10(stars)            — e.g. 1k★ +0.75, 50k★ +1.17
#   twitter: TWEET_POP_WEIGHT * log10(likes + 3·RT) — a RT weighs more than a like
#   reddit : no engagement field in metadata (RSS) -> 0 (affinity+focus only)
STAR_WEIGHT = 0.25
TWEET_POP_WEIGHT = 0.35
# Recency: the THIRD ranking pillar (alongside relevance and popularity). Freshly
# launched items rise; decays by exponential half-life.
# score += RECENCY_WEIGHT * 0.5^(age_days / RECENCY_HALFLIFE_DAYS).
# e.g. (weight 1.0, half-life 4d): today +1.0, 4d +0.50, 8d +0.25, 16d +0.06.
# Calibrated to sit at the same level as popularity/affinity so a recent +
# relevant + popular item combines all three and rises (replaces nothing).
RECENCY_WEIGHT = 1.0
RECENCY_HALFLIFE_DAYS = 4.0

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
# Repeated-news dedup: a candidate within cosine distance DEDUP_MAX_DIST of
# something ALREADY DELIVERED (same story, different source_id, sometimes another
# source/day) is dropped — even if you liked it. Calibrated: same story <=0.18,
# distinct stories >=0.28; 0.22 sits in the gap. DEDUP_SINCE_DAYS = "already received" window.
DEDUP_MAX_DIST = 0.22
DEDUP_SINCE_DAYS = 90
# Auto-balancing: learns the new×relevant mix from YOUR votes. Only kicks in
# with enough signal and runs once a day (in the delivery job, NOT in /feed). Small
# step (EMA), so a manual adjustment via chat dominates for several days.
AUTO_BALANCE_MIN_VOTES = 6
AUTO_BALANCE_STEP = 0.15
AUTO_BALANCE_BOUNDS = (0.15, 0.60)
# Learning the POPULARITY/RECENCY weights per user (analogous to auto-balance, but
# over the score weights). A multiplier on the base weight, learned from votes: if
# you like popular/recent items more -> the weight rises. SEMANTIC (affinity) stays
# dominant — so the cap is modest (1.5), letting pop/recency only INFLUENCE.
WEIGHT_PREF_BOUNDS = (0.5, 1.5)   # min/max multiplier (1.0 = neutral)
WEIGHT_PREF_GAIN = 0.8            # how much the preference [-1,1] moves the target
WEIGHT_PREF_STEP = 0.30          # EMA step (gradual)
WEIGHT_PREF_MIN_VOTES = 6        # minimum signal to learn
# CAP on the non-semantic term (popularity + recency summed, with learned weights).
# Guarantees SEMANTIC (affinity) stays dominant: without it, both multipliers at
# max (1.5 each) would sum to ~3.4-3.8 for a fresh viral and overpower a good
# on-topic match (affinity ~1.9-2.5). The cap keeps the non-semantic below a strong
# on-topic affinity — preserving "a big launch rises" without letting the learning
# steamroll the topic. ⚠️ Re-derive if STAR_WEIGHT/TWEET_POP_WEIGHT/RECENCY_WEIGHT change.
POP_REC_CAP = 2.5
# Focus quota (A): how many bucket slots a /focus occupies. If you don't say a
# number, the default is HALF the cap — focus prioritizes the topic but does NOT
# monopolize (the remaining slots stay normal: freshness + affinity -> diversity returns).
FOCUS_DEFAULT_QUOTA_FRACTION = 0.5
# Digest size (B): per-day card cap per bucket, adjustable via chat and saved in
# settings. Safety bounds for the requested value.
DIGEST_SIZE_MIN = 1
DIGEST_SIZE_MAX = 40
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

        # Repeated-news dedup: drop candidates near something already delivered
        # (same story from another source/day) — over the whole pool, before
        # bucketing, so it covers repos AND news.
        try:
            delivered_embs = await db.delivered_embeddings(
                user_id, since_days=DEDUP_SINCE_DAYS
            )
            before = len(pending)
            pending = _dedup_pending(pending, delivered_embs)
            if before != len(pending):
                logger.info(
                    "dedup: %d -> %d candidates (user_id=%s)",
                    before, len(pending), user_id,
                )
        except Exception:  # pragma: no cover - dedup never breaks delivery
            logger.exception("dedup failed (user_id=%s); proceeding without it", user_id)

        for bucket_key, header, platforms, _cap in BUCKETS:
            if tune:  # learns (once a day) the new×relevant mix and the pop/recency weights
                await _auto_tune_balance(db, user_id, bucket_key, list(platforms))
                await _auto_tune_weights(db, user_id, bucket_key, list(platforms))
            recs = [r for r in pending if r["source_platform"] in platforms]
            if recs:
                cap = await _bucket_cap(db, user_id, bucket_key)  # adjustable cap (B)
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


def _weight_pref_target(pairs: list[tuple[float, int]]) -> float | None:
    """Target multiplier (1.0 = neutral) from (signal_value, vote) pairs.

    Measures whether you like items with a HIGH signal (engagement or recency)
    MORE: split the votes at the signal's median and compare the 👍 rate of the
    high vs low group. `pref` ∈ [-1, 1] (+ = likes the high signal). Returns None
    when it can't separate (too little signal), so the weight isn't moved blindly.
    """
    sig = [(v, vote) for v, vote in pairs if v > 0]  # only those carrying the signal
    if len(sig) < WEIGHT_PREF_MIN_VOTES:             # (e.g. a tweet w/ likes; reddit pop=0 is excluded)
        return None
    median = sorted(v for v, _ in sig)[len(sig) // 2]
    high = [vote for v, vote in sig if v > median]
    low = [vote for v, vote in sig if v <= median]
    if not high or not low:
        return None
    like_high = sum(1 for x in high if x == 1) / len(high)
    like_low = sum(1 for x in low if x == 1) / len(low)
    pref = like_high - like_low  # [-1, 1]
    lo, hi = WEIGHT_PREF_BOUNDS
    return max(lo, min(hi, 1.0 + WEIGHT_PREF_GAIN * pref))


async def _auto_tune_weights(
    db: Database, user_id: int, bucket_key: str, platforms: list[str]
) -> None:
    """Learns the POPULARITY and RECENCY multipliers of the bucket from YOUR votes:
    if you like high-engagement items more, `pop` rises; if you like recent ones
    more, `recency` rises. Semantic (affinity) stays dominant — the multipliers are
    bounded (`WEIGHT_PREF_BOUNDS`). Small step (EMA).

    Recency is measured AT VOTE TIME (was it fresh when you voted?), not now —
    otherwise every old vote would look like "a vote on something stale".
    """
    try:
        rows = await db.vote_meta_signal(user_id, platforms)
    except Exception:  # pragma: no cover
        return
    if len(rows) < WEIGHT_PREF_MIN_VOTES:
        return
    pop_pairs, rec_pairs = [], []
    for r in rows:
        pop_pairs.append((_popularity_boost(r), int(r["vote"])))
        pub, voted = r["published_at"], r["voted_at"]
        if pub is not None and voted is not None:
            age = max(0.0, (voted.timestamp() - pub.timestamp()) / 86400.0)
            rv = float(0.5 ** (age / RECENCY_HALFLIFE_DAYS))
        else:
            rv = 0.0
        rec_pairs.append((rv, int(r["vote"])))

    cur_pop, cur_rec = await db.get_weight_prefs(user_id, bucket_key)
    lo, hi = WEIGHT_PREF_BOUNDS
    t_pop = _weight_pref_target(pop_pairs)
    t_rec = _weight_pref_target(rec_pairs)
    new_pop = cur_pop if t_pop is None else max(lo, min(hi, cur_pop + WEIGHT_PREF_STEP * (t_pop - cur_pop)))
    new_rec = cur_rec if t_rec is None else max(lo, min(hi, cur_rec + WEIGHT_PREF_STEP * (t_rec - cur_rec)))
    if abs(new_pop - cur_pop) >= 0.01 or abs(new_rec - cur_rec) >= 0.01:
        await db.set_weight_prefs(user_id, bucket_key, new_pop, new_rec)
        logger.info(
            "auto-weights %s: pop %.2f->%.2f, rec %.2f->%.2f (%d votes)",
            bucket_key, cur_pop, new_pop, cur_rec, new_rec, len(rows),
        )


def _dedup_pending(recs: list, delivered_embs: list) -> list:
    """Drop candidates near-identical to something ALREADY DELIVERED (same story
    from another source/day) and to each other — keeping the first of each group
    (newest, since `recs` is already published_at-desc).

    Runs over the WHOLE pool before bucketing, so it covers repos AND news.
    Vectors are L2-normalized -> cosine = inner product; cuts when similarity to
    anything seen exceeds (1 - DEDUP_MAX_DIST). Calibrated on news (same story
    <=0.18, distinct >=0.28); 0.22 is conservative enough not to merge distinct repos.
    """
    seen = [np.asarray(e, dtype=float) for e in delivered_embs if e is not None]
    seen_mat = np.vstack(seen) if seen else None
    kept = []
    for rec in recs:
        emb = rec["embedding"]
        if emb is None:
            kept.append(rec)
            continue
        v = np.asarray(emb, dtype=float)
        if seen_mat is not None and float((seen_mat @ v).max()) >= 1.0 - DEDUP_MAX_DIST:
            continue  # near-duplicate of something delivered/chosen -> skip
        kept.append(rec)
        seen_mat = v[None, :] if seen_mat is None else np.vstack([seen_mat, v])
    return kept


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


def _popularity_boost(rec) -> float:
    """POPULARITY/importance bonus for a post (independent of the embedding).

    The owner's intuition: a hugely important launch (e.g. GPT-5.6) may NOT match
    their affinity, but engagement (likes/RTs/stars) is the signal that "this is
    big" — so it should rise. All in log10 so a viral item doesn't steamroll
    affinity/focus. See `STAR_WEIGHT`/`TWEET_POP_WEIGHT`.
      - github : stars
      - twitter: likes + 3·retweets (a RT is a stronger signal than a like)
      - reddit : no engagement field in metadata (RSS) -> 0
    """
    meta = rec["metadata"] or {}
    sp = rec["source_platform"]
    if sp == "github":
        stars = meta.get("stars")
        if stars:
            return STAR_WEIGHT * float(np.log10(max(int(stars), 1)))
    elif sp == "twitter":
        try:
            eng = int(meta.get("like_count") or 0) + 3 * int(meta.get("retweet_count") or 0)
        except (TypeError, ValueError):
            eng = 0
        if eng > 0:
            return TWEET_POP_WEIGHT * float(np.log10(eng))
    return 0.0


def _recency_boost(rec, now_ts: float) -> float:
    """How NEW the post is (0 = old or no date). The third pillar of the score,
    alongside relevance (affinity+focus) and popularity.

    Decays by exponential half-life (`RECENCY_HALFLIFE_DAYS`): a freshly launched
    item gets the full boost and fades smoothly. Depends only on `published_at`.
    """
    pub = _pub_ts(rec)
    if pub <= 0:
        return 0.0
    age_days = max(0.0, (now_ts - pub) / 86400.0)
    return RECENCY_WEIGHT * float(0.5 ** (age_days / RECENCY_HALFLIFE_DAYS))


async def _bucket_cap(db: Database, user_id: int, bucket_key: str) -> int:
    """Per-day card cap of the bucket: the user's override (B) or the BUCKETS default."""
    n = await db.get_digest_size(user_id, bucket_key)
    if n is None:
        n = _BUCKET_CAP.get(bucket_key, 1)
    return max(DIGEST_SIZE_MIN, min(int(n), DIGEST_SIZE_MAX))


def _focus_quota(focuses, cap: int) -> int:
    """How many bucket slots the /focus occupies (A). Uses the largest explicit
    quota among active focuses; if none has a number, the default (half the cap). 1..cap."""
    quotas = [int(f["quota"]) for f in focuses if f["quota"] is not None]
    q = max(quotas) if quotas else round(cap * FOCUS_DEFAULT_QUOTA_FRACTION)
    return max(1, min(q, cap))


async def _fresh_reserve(db: Database, user_id: int, bucket_key: str, slots: int) -> int:
    """Of the `slots` NORMAL slots, how many go to freshness (the rest to relevance).
    Uses the saved mix (/balance) if any, else the bucket's default reserve."""
    frac = await db.get_balance(user_id, bucket_key)
    if frac is None:
        return min(FRESH_SLOTS.get(bucket_key, 0), slots)
    return min(round(max(0.0, min(1.0, frac)) * slots), slots)


async def _deliver_bucket(
    app: Application, db: Database, telegram_user_id: int, user_id: int,
    bucket_key: str, header: str, platforms: list[str], cap: int, recs: list,
) -> int:
    """Ranks and delivers ONE bucket.

    THREE pillars add up in each card's score — relevance + popularity + recency:
      - relevance: your 👍/👎 affinity WITHIN this bucket + the active /focus;
      - popularity: engagement (stars on github, likes+RTs on X);
      - recency: how recently it was published (decays by half-life).
    A recent + relevant + popular item combines all three and rises to the top.
    The popularity/recency weights are LEARNED from your votes (bounded + capped so
    semantic stays dominant). Affinity here only RANKS (👍 rises, 👎 sinks).
    """
    likes, dislikes = await db.vote_counts(user_id, platforms=platforms)
    affinity_on = (likes + dislikes) >= MIN_VOTES_FOR_AFFINITY
    focuses = await db.active_focus(user_id, bucket_key)

    # The 3 pillars: relevance (affinity+focus) + popularity + recency.
    # pop_mult/rec_mult are LEARNED from your votes (1.0 = neutral): if you like
    # popular/recent items more, the weight rises — bounded so semantic dominates.
    pop_mult, rec_mult = await db.get_weight_prefs(user_id, bucket_key)
    now_ts = datetime.now(timezone.utc).timestamp()
    scored = []
    for rec in recs:
        score = 0.0
        emb = rec["embedding"]
        if emb is not None:
            # --- (1) RELEVANCE: affinity (👍/👎 of this bucket) only RANKS ---
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
            # --- (1) RELEVANCE: active direction (/focus) ---
            if focuses:
                score += _focus_boost(emb, focuses)
        # --- (2)+(3) POPULARITY + RECENCY, weighted by your learned taste ---
        # stars/likes+RTs (a big launch rises without matching affinity) and
        # freshness (decays by half-life). Summed with a CAP so semantic dominates.
        nonsem = (
            pop_mult * _popularity_boost(rec)
            + rec_mult * _recency_boost(rec, now_ts)
        )
        score += min(POP_REC_CAP, nonsem)
        scored.append((rec, score))

    scored.sort(key=lambda t: t[1], reverse=True)
    score_by_id = {rec["id"]: score for rec, score in scored}

    # Splits the bucket's slots into two portions:
    #   (1) FOCUS (A): occupies `fq` slots with the top by score (focus-weighted);
    #   (2) NORMAL: the rest of the cap, split freshness × relevance over whoever
    #       is left — this is what brings diversity/platform back (e.g. Reddit
    #       doesn't vanish when the focus is concentrated on X). With no focus,
    #       fq=0 and the whole bucket is the normal portion (the usual behavior).
    chosen, chosen_ids = [], set()
    if focuses:
        fq = _focus_quota(focuses, cap)
        for rec, _ in scored:                  # focus occupies fq slots (top score)
            if len(chosen) >= fq:
                break
            chosen.append(rec)
            chosen_ids.add(rec["id"])

    normal_slots = cap - len(chosen)
    if normal_slots > 0:
        leftovers = [(rec, s) for rec, s in scored if rec["id"] not in chosen_ids]
        fresh_quota = await _fresh_reserve(db, user_id, bucket_key, normal_slots)
        relevant_quota = normal_slots - fresh_quota
        taken_rel = 0
        for rec, _ in leftovers:               # relevance slots (by score)
            if taken_rel >= relevant_quota:
                break
            chosen.append(rec)
            chosen_ids.add(rec["id"])
            taken_rel += 1
        rest = [rec for rec, _ in leftovers if rec["id"] not in chosen_ids]
        rest.sort(key=_pub_ts, reverse=True)   # freshness slots: the newest
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
        "• just talk to me — steer (“for 3 days I want up to 6 news about RAG”), "
        "ask (“what was that news about agents that I liked?”), "
        "query the state (“what's in focus?”) or resize (“up to 20 news a day”)\n"
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
        # Two-stage search: broad vector recall -> rerank (relevance cut).
        matches = await semantic_recall(
            _db(context), _embedder(context), user_id, q_en,
            mode="archive", limit=SEARCH_LIMIT,
        )
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


async def _render_focus(db, user_id: int) -> str:
    """Text of the active focus per bucket (or a notice that there is none). Shared
    by the /focus command and the spoken state query (kind="status")."""
    lines = ["🎯 Active focus:"]
    any_focus = False
    for bucket_key, lbl in (("repos", "📦 repos"), ("news", "🗞️ news")):
        cap = await _bucket_cap(db, user_id, bucket_key)
        for r in await db.active_focus(user_id, bucket_key):
            any_focus = True
            q = _focus_quota([r], cap)  # effective quota (explicit or default)
            lines.append(f"• {lbl} → {r['topic']}  (up to {q} of {cap})")
    if not any_focus:
        return "No active focus. Tell me something like “for 3 days I want up to 6 repos about RAG”."
    lines.append("\n/focus clear to clear it.")
    return "\n".join(lines)


async def _render_mixture(db, user_id: int) -> str:
    """Text of the current new×relevant mix per bucket. Shared by the /mix command
    and the spoken state query (kind="status")."""
    label = {"repos": "📦 repos", "news": "🗞️ news"}
    lines = ["⚖️ Current mix (novelty / relevance):"]
    for bucket_key in ("repos", "news"):
        cap = await _bucket_cap(db, user_id, bucket_key)
        size_tag = "default" if await db.get_digest_size(user_id, bucket_key) is None else "adjusted"
        frac = await db.get_balance(user_id, bucket_key)
        if frac is None:  # no saved adjustment -> bucket's default reserve
            f = FRESH_SLOTS.get(bucket_key, 0) / max(1, _BUCKET_CAP.get(bucket_key, 1))
            tag = "default"
        else:
            f = float(frac)
            tag = "adjusted"
        pct = round(f * 100)
        lines.append(
            f"• {label[bucket_key]}: ~{pct}% novelty / {100 - pct}% relevance ({tag}) "
            f"· up to {cap}/day ({size_tag})"
        )
    lines.append(
        "\nThe bot auto-adjusts the mix from your votes (starting at ~6 votes in the bucket). "
        "To change by hand: just say it (e.g. “more novelty in the news”, “up to 20 news/day”). "
        "To reset the mix: “undo that”."
    )
    return "\n".join(lines)


async def cmd_focus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/focus — shows the active focus. /focus clear — clears it. /focus <text> — steers."""
    user = update.effective_user
    if not _is_allowed(context, user.id if user else None):
        return
    arg = " ".join(context.args).strip() if context.args else ""
    user_id = await _resolve_user_id(context, user.id)
    db = _db(context)
    # Any /focus is an explicit focus action -> cancel a pending quota Q&A
    # (otherwise a stray number afterwards would re-create a just-cleared focus).
    if context.user_data is not None:
        context.user_data.pop("pending_focus", None)

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
    await update.effective_message.reply_text(await _render_focus(db, user_id))


async def cmd_mix(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mix — shows each bucket's current new×relevant balance."""
    user = update.effective_user
    if not _is_allowed(context, user.id if user else None):
        return
    user_id = await _resolve_user_id(context, user.id)
    db = _db(context)
    await update.effective_message.reply_text(await _render_mixture(db, user_id))


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
    "• query the feed's state — e.g.: “what's in focus?”, “what's the mix "
    "now?”, “how's my feed?”\n"
    "• resize the digest — e.g.: “up to 20 news a day”, “8 repos a day”\n"
    "• or paste a link for me to save to the archive."
)


async def _handle_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE, text: str
) -> None:
    """Routes a text message: feed direction / vote recall / hint."""
    # C — focus Q&A: if a focus is awaiting "how many?", this message may be the
    # answer. If it's a number/"all", complete it; otherwise abandon the question
    # and proceed normally (don't hijack an unrelated message).
    pending = context.user_data.get("pending_focus") if context.user_data is not None else None
    if pending:
        db = _db(context)
        user_id = await _resolve_user_id(context, update.effective_user.id)
        q = _parse_quota(text, await _bucket_cap(db, user_id, pending["bucket"]))
        if q is not None:
            await _complete_pending_focus(update, context, pending, q)
            return
        context.user_data.pop("pending_focus", None)  # not an answer -> proceed

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
    if intent.kind == "capacity":
        await _apply_capacity(
            update, context, intent.capacity_bucket, intent.capacity_count
        )
        return
    if intent.kind == "status":
        await _do_status(update, context, intent.status_about)
        return
    await update.effective_message.reply_text(intent.reply or _CHAT_HINT)


async def _do_status(
    update: Update, context: ContextTypes.DEFAULT_TYPE, about: str
) -> None:
    """Answers "what's in focus? / what's the mix?" by reading the REAL feed state
    (focus table + settings->balance), without changing anything."""
    user_id = await _resolve_user_id(context, update.effective_user.id)
    db = _db(context)
    parts = []
    if about in ("focus", "both"):
        parts.append(await _render_focus(db, user_id))
    if about in ("balance", "both"):
        parts.append(await _render_mixture(db, user_id))
    if not parts:  # defensive fallback if an unexpected value comes in
        parts.append(await _render_focus(db, user_id))
    await update.effective_message.reply_text("\n\n".join(parts))


_BUCKET_LABEL = {"repos": "📦 repos", "news": "🗞️ news"}


def _parse_quota(text: str, cap: int) -> int | None:
    """Reads the quantity from a SHORT reply to "how many?" ("6", "up to 6", "a
    couple", "all", "half"). Returns 1..cap, or None.

    IMPORTANT: only matches a short quantity reply — NOT a new sentence that
    happens to contain a digit ("anything about GPT-4?", "send me 20 news a day").
    So if the user abandons the Q&A and sends something else, this returns None
    and the message follows normal routing (without hijacking the new intent)."""
    t = (text or "").strip().lower()
    if re.search(r"\b(all|everything|max|maximum)\b", t):
        return cap
    if re.search(r"\bhalf\b", t):
        return max(1, round(cap / 2))
    # exactly ONE number, optionally with "up to/about/~/=" before and
    # "cards/items/posts" after — and NO free text beyond that.
    m = re.fullmatch(
        r"(?:up to\s+|at[eé]\s+|about\s+|~\s*|=\s*)?(\d{1,3})\s*(?:cards?|items?|posts?)?",
        t,
    )
    if m:
        return max(1, min(int(m.group(1)), cap))
    return None


async def _apply_focus(
    update: Update, context: ContextTypes.DEFAULT_TYPE, directives
) -> None:
    """Applies /focus. Each direction occupies a QUOTA of the bucket's slots (A);
    if a SINGLE direction comes WITHOUT a number, the bot ASKS how many (C) and waits."""
    user_id = await _resolve_user_id(context, update.effective_user.id)
    embedder = _embedder(context)
    db = _db(context)

    # C — a single direction without a quota: ask "how many?" and store pending.
    if len(directives) == 1 and directives[0].quota <= 0:
        d = directives[0]
        cap = await _bucket_cap(db, user_id, d.bucket)
        if context.user_data is not None:
            context.user_data["pending_focus"] = {
                "bucket": d.bucket, "topic": d.topic, "days": d.days,
            }
        lbl = _BUCKET_LABEL.get(d.bucket, d.bucket)
        await update.effective_message.reply_text(
            f"Got it! Of the up-to-{cap} {lbl} cards, how many should be about "
            f"“{d.topic}”? Send a number (or “all”). The rest of the bucket stays normal."
        )
        return

    applied = []
    for d in directives:
        try:
            vec = await embedder.embed_query(d.topic)
            quota = d.quota if d.quota > 0 else None  # None = default (half the cap)
            await db.set_focus(user_id, d.bucket, d.topic, vec, d.days, quota=quota)
            applied.append(d)
        except Exception:  # pragma: no cover
            logger.exception("steering: failed to apply focus %s/%s", d.bucket, d.topic)
    if not applied:
        await update.effective_message.reply_text("I tried to adjust the focus but it errored out. 😬")
        return

    lines = ["🎯 Focus updated:"]
    for d in applied:
        cap = await _bucket_cap(db, user_id, d.bucket)
        q = _focus_quota([{"quota": d.quota if d.quota > 0 else None}], cap)
        lines.append(
            f"• {_BUCKET_LABEL.get(d.bucket, d.bucket)} → {d.topic}  "
            f"(up to {q} of {cap}, {d.days} day(s))"
        )
    lines.append("")
    lines.append(
        "I'll prioritize this in delivery (the rest of the bucket stays normal) and "
        "start fetching more on this topic.\n/focus shows what's active · /focus clear clears it."
    )
    await update.effective_message.reply_text("\n".join(lines))


async def _complete_pending_focus(
    update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict, quota: int
) -> None:
    """C — completes the focus that was awaiting its quantity (the Q&A reply)."""
    if context.user_data is not None:
        context.user_data.pop("pending_focus", None)
    user_id = await _resolve_user_id(context, update.effective_user.id)
    db = _db(context)
    embedder = _embedder(context)
    cap = await _bucket_cap(db, user_id, pending["bucket"])
    try:
        vec = await embedder.embed_query(pending["topic"])
        await db.set_focus(
            user_id, pending["bucket"], pending["topic"], vec, pending["days"],
            quota=quota,
        )
    except Exception:  # pragma: no cover
        logger.exception("steering: failed to complete pending focus")
        await update.effective_message.reply_text("I tried to adjust the focus but it errored out. 😬")
        return
    lbl = _BUCKET_LABEL.get(pending["bucket"], pending["bucket"])
    await update.effective_message.reply_text(
        f"🎯 Focus: {lbl} → {pending['topic']} — up to {quota} of {cap}. "
        f"The rest of the bucket stays normal (freshness + your taste)."
    )


async def _apply_capacity(
    update: Update, context: ContextTypes.DEFAULT_TYPE, bucket: str, count: int
) -> None:
    """B — changes a bucket's per-day card cap (or both). Saved in settings."""
    user_id = await _resolve_user_id(context, update.effective_user.id)
    db = _db(context)
    if not count or count <= 0:  # no number -> ask (can't guess +/-)
        await update.effective_message.reply_text(
            "How many cards per day do you want? E.g. “up to 20 news a day” or "
            "“8 repos a day”."
        )
        return
    n = max(DIGEST_SIZE_MIN, min(int(count), DIGEST_SIZE_MAX))
    buckets = ["repos", "news"] if bucket == "both" else [bucket]
    for b in buckets:
        await db.set_digest_size(user_id, b, n)
    names = " and ".join(_BUCKET_LABEL.get(b, b) for b in buckets)
    extra = "" if n == count else f" (capped at {n}, max {DIGEST_SIZE_MAX})"
    await update.effective_message.reply_text(
        f"📐 Digest size: {names} → up to {n} card(s)/day{extra}."
    )


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
    embedder = _embedder(context)
    try:
        if polarity in ("liked", "disliked"):
            vote = 1 if polarity == "liked" else -1
            rows = await semantic_recall(
                db, embedder, user_id, query, mode="voted", vote=vote, limit=SEARCH_LIMIT
            )
            mode = "voted"
        else:  # 'any' -> the whole curated archive (not just your votes)
            rows = await semantic_recall(
                db, embedder, user_id, query, mode="archive", limit=SEARCH_LIMIT
            )
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
