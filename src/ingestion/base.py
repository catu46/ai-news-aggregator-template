"""Common interface for ingestion sources.

Every source (Reddit, X, ...) implements `fetch()` and returns a batch of
normalized `IngestedPost`. The runner deduplicates by (platform, source_id)
in the database, so sources don't need to worry about duplicates.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..common.models import IngestedPost


class IngestionSource(ABC):
    name: str = "source"

    @abstractmethod
    async def fetch(self) -> list[IngestedPost]:
        """Collect recent posts and return them normalized."""
        raise NotImplementedError
