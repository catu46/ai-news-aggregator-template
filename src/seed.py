"""Seed / cold-start: loads the user's taste examples BEFORE any real ingestion,
so the system already has signal on day 1.

For each user present in seeds.yaml:
  1. resolve the telegram_user_id via sources.yaml and create/fetch the user_id;
  2. turn each SeedExample into an IngestedPost with source_platform='seed'
     and a deterministic source_id ('seed-' + sha1(text)[:16]);
  3. upsert the post (dedup by the (source_platform, source_id) pair);
  4. embed the text and store the embedding (feeds RECALL and the centroid);
  5. record a pre-loaded vote: gold => +1 (👍), noise => -1 (👎),
     with origin='seed'.

Seeds do NOT go through the curator: the verdict is left NULL on purpose (they
are not "approved/rejected posts" — they are taste labels). That is why they feed
the recall archive and the affinity prior, but do not enter the delivery flow.

Note: the `posts.source_platform` CHECK in `db/schema.sql` ALREADY accepts 'seed'
(in addition to 'github' and 'manual'), so upserting seeds works directly — there
is no extra schema step.

Usage:  python -m src.seed
"""
from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass

from .common.config import SeedExample, UserSources, load_seeds, load_settings, load_sources
from .common.db import Database
from .common.embeddings import Embedder
from .common.models import IngestedPost


def _seed_source_id(text: str) -> str:
    """Deterministic source_id => re-running the seed does not duplicate posts."""
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
    return f"seed-{digest[:16]}"


@dataclass
class UserSeedResult:
    """Summary of what was seeded for a user."""

    user_key: str
    user_id: int
    gold_seeded: int = 0
    noise_seeded: int = 0
    skipped_dup: int = 0


async def _seed_user(
    db: Database,
    embedder: Embedder,
    user_key: str,
    src: UserSources,
    examples: list[SeedExample],
) -> UserSeedResult:
    """Seed all examples for a single user."""
    user_id = await db.get_or_create_user(src.telegram_user_id, src.display_name)
    result = UserSeedResult(user_key=user_key, user_id=user_id)

    for ex in examples:
        post = IngestedPost(
            source_platform="seed",  # valid value of IngestedPost's Literal and the schema CHECK
            source_id=_seed_source_id(ex.text),
            source_url=ex.url or "",
            raw_text=ex.text,
            author=None,
            published_at=None,
            metadata={"seed": True, "label": ex.label, "user_key": user_key},
        )

        post_id = await db.upsert_post(post)
        if post_id is None:
            # Already existed (re-run, or the same text repeated): embedding and vote
            # were already stored on the 1st pass. With no lookup method in the
            # shared interface, we treat this as an idempotent no-op.
            result.skipped_dup += 1
            continue

        # Embedding (feeds centroid + recall). embed_documents works in batches;
        # here it is 1 at a time to keep the post_id <-> vector relation trivial.
        [embedding] = await embedder.embed_documents([ex.text])
        await db.set_embedding(post_id, embedding, _model_of(embedder))

        # Pre-loaded vote: gold liked, noise not liked. origin='seed'.
        vote = 1 if ex.label == "gold" else -1
        await db.record_vote(user_id, post_id, vote, origin="seed")

        if ex.label == "gold":
            result.gold_seeded += 1
        else:
            result.noise_seeded += 1

    return result


def _model_of(embedder: Embedder) -> str:
    """Read the Embedder's model id without relying on a public attribute that
    doesn't exist.

    The Embedder keeps the model in `_model`; we use getattr so it won't break if
    the name changes in the future.
    """
    return getattr(embedder, "_model", "voyage-4-lite")


async def main() -> None:
    settings = load_settings()
    sources = load_sources()
    seeds = load_seeds()

    if not seeds:
        print("No seeds found (config/seeds.yaml empty or only placeholders). Nothing to do.")
        return

    # Index user_key -> UserSources to resolve the telegram_user_id of each seed.
    sources_by_key = {s.key: s for s in sources}

    db = Database(settings.database_url)
    await db.connect()
    embedder = Embedder(settings)

    results: list[UserSeedResult] = []
    skipped_keys: list[str] = []
    try:
        for user_key, examples in seeds.items():
            src = sources_by_key.get(user_key)
            if src is None:
                # Seed with no matching source: can't resolve the telegram_user_id.
                skipped_keys.append(user_key)
                print(
                    f"[skip] user_key '{user_key}' has seeds but is not in "
                    f"sources.yaml — can't resolve telegram_user_id."
                )
                continue
            result = await _seed_user(db, embedder, user_key, src, examples)
            results.append(result)
    finally:
        await db.close()

    # ----------------------------------------------------------------- summary
    print("\n=== Seed summary ===")
    if not results:
        print("No users seeded.")
    for r in results:
        print(
            f"  {r.user_key} (user_id={r.user_id}): "
            f"{r.gold_seeded} gold 👍, {r.noise_seeded} noise 👎"
            + (f", {r.skipped_dup} dup (already existed)" if r.skipped_dup else "")
        )
    if skipped_keys:
        print(f"  Skipped (no source in sources.yaml): {', '.join(skipped_keys)}")

    total_gold = sum(r.gold_seeded for r in results)
    total_noise = sum(r.noise_seeded for r in results)
    print(f"  Total: {total_gold} gold, {total_noise} noise across {len(results)} user(s).")


if __name__ == "__main__":
    asyncio.run(main())
