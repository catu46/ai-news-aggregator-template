"""Voyage AI wrapper (voyage-4-lite). L2-normalized vectors, 1024-dim."""
from __future__ import annotations

import asyncio

import voyageai

from .config import Settings

_EMBED_BATCH = 32  # texts per Voyage call (respects request limits)


class Embedder:
    def __init__(self, settings: Settings) -> None:
        self._client = voyageai.Client(api_key=settings.voyage_api_key)
        self._model = settings.embedding_model
        self.model = settings.embedding_model  # public (e.g. bot when saving a link)
        self._rerank_model = settings.rerank_model

    async def rerank(
        self, query: str, documents: list[str], top_k: int | None = None
    ) -> list[tuple[int, float]]:
        """Reorder `documents` by REAL relevance to `query` (Voyage cross-encoder).

        Cosine distance alone doesn't separate well in a single-domain archive
        (everything AI is close to everything); the reranker reads query+text
        TOGETHER and gives a 0..1 relevance score that actually separates.
        Returns a list of (index_in_documents, score) sorted by score desc; `[]`
        if empty. This is the 2nd stage of the search (see recall.py)."""
        if not documents:
            return []

        def _call() -> list[tuple[int, float]]:
            res = self._client.rerank(
                query, documents, model=self._rerank_model, top_k=top_k
            )
            return [(r.index, float(r.relevance_score)) for r in res.results]

        return await asyncio.to_thread(_call)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embeddings for posts (input_type='document')."""
        return await self._embed(texts, "document")

    async def embed_query(self, text: str) -> list[float]:
        """Embedding for a search query (input_type='query')."""
        out = await self._embed([text], "query")
        return out[0]

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
