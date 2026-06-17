"""Loads .env + config/sources.yaml + config/seeds.yaml into typed objects."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


def _env(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    val = os.getenv(name, default)
    if required and not val:
        raise RuntimeError(f"Required environment variable missing: {name}")
    return val


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str
    curator_model: str
    curator_monthly_budget_usd: float
    voyage_api_key: str
    embedding_model: str
    rerank_model: str              # Voyage reranker for the 2-stage search
    reddit_user_agent: str
    twitter_auth_token: str | None
    twitter_ct0: str | None
    telegram_bot_token: str
    database_url: str
    exa_api_key: str | None
    github_token: str | None
    curator_provider: str          # 'anthropic' (default) | 'kimi'
    moonshot_api_key: str | None    # if curator_provider='kimi'
    moonshot_base_url: str
    kimi_model: str
    digest_hour: int               # local hour of the daily delivery (0-23)
    digest_tz: str                 # IANA timezone for the delivery hour


def load_settings() -> Settings:
    return Settings(
        anthropic_api_key=_env("ANTHROPIC_API_KEY", required=True),
        curator_model=_env("CURATOR_MODEL", "claude-haiku-4-5"),
        curator_monthly_budget_usd=float(_env("CURATOR_MONTHLY_BUDGET_USD", "8")),
        voyage_api_key=_env("VOYAGE_API_KEY", required=True),
        embedding_model=_env("EMBEDDING_MODEL", "voyage-4-lite"),
        rerank_model=_env("RERANK_MODEL", "rerank-2.5"),
        reddit_user_agent=_env(
            "REDDIT_USER_AGENT",
            "ai-news-aggregator/1.0 (personal, non-commercial)",
        ),
        twitter_auth_token=_env("TWITTER_AUTH_TOKEN"),
        twitter_ct0=_env("TWITTER_CT0"),
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN", required=True),
        database_url=_env("DATABASE_URL", required=True),
        exa_api_key=_env("EXA_API_KEY"),
        github_token=_env("GITHUB_TOKEN"),
        curator_provider=_env("CURATOR_PROVIDER", "anthropic"),
        moonshot_api_key=_env("MOONSHOT_API_KEY"),
        moonshot_base_url=_env("MOONSHOT_BASE_URL", "https://api.moonshot.ai/v1"),
        kimi_model=_env("KIMI_MODEL", "kimi-k2.6"),
        digest_hour=int(_env("DIGEST_HOUR", "7")),
        digest_tz=_env("DIGEST_TZ", "America/Sao_Paulo"),
    )


# --------------------------------------------------------------------------
# sources.yaml  (sources per user)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class UserSources:
    key: str
    telegram_user_id: int
    display_name: str | None
    subreddits: list[str] = field(default_factory=list)
    x_accounts: list[str] = field(default_factory=list)
    x_searches: list[str] = field(default_factory=list)
    github_queries: list[str] = field(default_factory=list)


def load_sources(path: Path | None = None) -> list[UserSources]:
    path = path or (CONFIG_DIR / "sources.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: list[UserSources] = []
    for key, u in (data.get("users") or {}).items():
        reddit = u.get("reddit") or {}
        x = u.get("x") or {}
        github = u.get("github") or {}
        out.append(
            UserSources(
                key=key,
                telegram_user_id=int(u["telegram_user_id"]),
                display_name=u.get("display_name"),
                subreddits=list(reddit.get("subreddits") or []),
                x_accounts=list(x.get("accounts") or []),
                x_searches=list(x.get("searches") or []),
                github_queries=list(github.get("queries") or []),
            )
        )
    return out


# --------------------------------------------------------------------------
# seeds.yaml  (cold-start examples per user)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SeedExample:
    text: str
    label: str  # 'gold' | 'noise'
    url: str | None = None


def load_seeds(path: Path | None = None) -> dict[str, list[SeedExample]]:
    """Returns {user_key: [SeedExample, ...]}. Links to telegram_user_id via sources."""
    path = path or (CONFIG_DIR / "seeds.yaml")
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, list[SeedExample]] = {}
    for key, u in (data.get("users") or {}).items():
        examples: list[SeedExample] = []
        for label in ("gold", "noise"):
            for item in (u.get(label) or []):
                text = (item.get("text") or "").strip()
                if not text or text.startswith("<"):  # ignore <...> placeholders
                    continue
                examples.append(SeedExample(text=text, label=label, url=item.get("url")))
        if examples:
            out[key] = examples
    return out
