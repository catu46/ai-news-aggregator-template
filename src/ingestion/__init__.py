"""Ingestion layer: sources that emit normalized IngestedPost objects."""
from .base import IngestionSource
from .reddit_source import RedditSource
from .github_source import GitHubSource
from .x_source import XSource

__all__ = ["IngestionSource", "RedditSource", "GitHubSource", "XSource"]
