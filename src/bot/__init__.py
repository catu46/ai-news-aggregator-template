"""Telegram bot: curated delivery + 👍/👎 votes + semantic search over your likes."""
from .bot import build_application, deliver_pending, main

__all__ = ["build_application", "deliver_pending", "main"]
