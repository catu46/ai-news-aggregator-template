"""Voyage AI wrapper. L2-normalized vectors, 1024-dim.

Documents use `embedding_model` (default voyage-4-lite, cheap, runs at volume).
The search QUESTION can use a different `query_embedding_model` (potentially
larger, e.g. voyage-4-large) because the voyage-4 family (nano/lite/voyage-4/
large) shares the SAME vector space across sizes — Voyage calls these
"interchangeable" embeddings between family members
(https://blog.voyageai.com/2026/01/15/voyage-4/). This lets you spend a bit
more only on the few query calls (more context/quality) without re-embedding
the whole archive. See `Settings.query_embedding_model`.
"""
from __future__ import annotations

import asyncio

import numpy as np
import voyageai

from .config import Settings

_EMBED_BATCH = 32  # texts per Voyage call (respects request limits)
_QUERY_DIM = 1024  # pinned to the schema's size (posts.embedding vector(1024))


class Embedder:
    def __init__(self, settings: Settings) -> None:
        self._client = voyageai.Client(api_key=settings.voyage_api_key)
        self._model = settings.embedding_model
        self.model = settings.embedding_model  # public (e.g. bot when saving a link)
        self._query_model = settings.query_embedding_model
        self._rerank_model = settings.rerank_model
        self._rerank_profile = settings.rerank_profile

    async def rerank(
        self, query: str, documents: list[str], top_k: int | None = None,
        *, use_profile: bool = False,
    ) -> list[tuple[int, float]]:
        """Reorder `documents` by REAL relevance to `query` (Voyage cross-encoder).

        Cosine distance alone doesn't separate well in a single-domain archive
        (everything AI is close to everything); the reranker reads query+text
        TOGETHER and gives a 0..1 relevance score that actually separates.
        Returns a list of (index_in_documents, score) sorted by score desc; `[]`
        if empty. This is the 2nd stage of the search (see recall.py).

        `use_profile=True` embeds the `rerank_profile` (your taste, see Settings)
        as an instruction in the query — should ONLY be passed by the archive
        search path (recall.py). Defaults to False so the profile doesn't leak
        into other future uses of the reranker that aren't "find what I like in
        my archive" (e.g. a neutral rerank of something else entirely).
        """
        if not documents:
            return []

        q = self._compose_query(query) if use_profile else query

        def _call() -> list[tuple[int, float]]:
            res = self._client.rerank(
                q, documents, model=self._rerank_model, top_k=top_k
            )
            return [(r.index, float(r.relevance_score)) for r in res.results]

        return await asyncio.to_thread(_call)

    def _compose_query(self, query: str) -> str:
        """Embeds `rerank_profile` into the query, in rerank-2.5's syntax
        (instruction-following).

        The official docs (https://docs.voyageai.com/docs/reranker) confirm
        that rerank-2.5 accepts "optional instructions [that] can be appended
        or prepended to the query to better guide the relevance", but do NOT
        document a special delimiter/format — so we use a clear, readable
        prefix ("Instruction: ...\\nQuery: ...").
        """
        if not self._rerank_profile:
            return query
        return f"Instruction: {self._rerank_profile}\nQuery: {query}"

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embeddings for posts (input_type='document')."""
        return await self._embed(texts, "document")

    async def embed_query(self, text: str) -> list[float]:
        """Embedding for a search query (input_type='query').

        Uses `query_embedding_model` (Settings), which can be LARGER than the
        documents' model — same vector space within the voyage-4 family (see
        the module docstring). `output_dimension=1024` pins the output to the
        schema's size even if the larger model's default is different
        (voyage-4-large accepts 256/512/1024/2048 via Matryoshka). Voyage's
        docs don't explicitly guarantee that Matryoshka-truncated output comes
        back re-normalized; we re-normalize by L2 here as a cheap safeguard
        (numpy is already a dependency) — idempotent if it's already normalized.
        """
        def _call() -> list[float]:
            return self._client.embed(
                [text], model=self._query_model, input_type="query",
                output_dimension=_QUERY_DIM,
            ).embeddings[0]

        vec = await asyncio.to_thread(_call)
        arr = np.asarray(vec, dtype=np.float64)
        norm = np.linalg.norm(arr)
        if norm > 0:
            arr = arr / norm
        return arr.tolist()

    async def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        # The Voyage client is synchronous; we run it in a thread so we don't block the loop.
        # Batched so we don't blow past the token limit per request.
        out: list[list[float]] = []
        for i in range(0, len(texts), _EMBED_BATCH):
            chunk = texts[i : i + _EMBED_BATCH]

            def _call(chunk: list[str] = chunk) -> list[list[float]]:
                return self._client.embed(
                    chunk, model=self._model, input_type=input_type
                ).embeddings

            out.extend(await asyncio.to_thread(_call))
        return out
