"""Settings from environment variables (.env) and watchlist.yaml."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

REQUIRED = ("DART_API_KEY", "DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID", "DISCORD_APPROVER_USER_ID")


@dataclass(frozen=True)
class Settings:
    dart_key: str
    discord_token: str
    channel_id: int
    approver_id: int
    watchlist: dict[str, str] = field(default_factory=dict)
    kis_key: str = ""
    kis_secret: str = ""
    kis_env: str = "real"
    data_dir: str = "data"
    poll_minutes: int = 10
    review_timeout_s: int = 1800
    summary_max_chars: int = 12000
    llm_order: str = "local,groq,gemini"   # watchlist filings: any provider, in this order
    universe_llm_order: str = "local"      # the rest of the market: free-tier limits are too small for it


def load_dotenv(path: str = ".env", env: dict | None = None) -> None:
    """Minimal KEY=VALUE reader. Variables already set win."""
    env = os.environ if env is None else env
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_watchlist(path: str) -> dict[str, str]:
    # BaseLoader keeps every value as written: plain YAML reads 000660 as the octal number 432
    with open(path, encoding="utf-8") as f:
        data = yaml.load(f, Loader=yaml.BaseLoader) or {}
    stocks = data.get("stocks") or {}
    bad = [c for c in stocks if not (len(c) == 6 and c.isalnum())]
    if bad:
        raise ValueError(f"watchlist codes must be 6 characters, e.g. \"005930\": {bad}")
    return dict(stocks)


def load(env: dict | None = None, watchlist_path: str = "watchlist.yaml") -> Settings:
    env = os.environ if env is None else env
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        raise SystemExit(f"missing settings in .env: {', '.join(missing)}")
    if not os.path.isfile(watchlist_path):
        raise SystemExit(f"{watchlist_path} not found: cp watchlist.example.yaml {watchlist_path}, then list your stocks")
    return Settings(
        dart_key=env["DART_API_KEY"],
        discord_token=env["DISCORD_BOT_TOKEN"],
        channel_id=int(env["DISCORD_CHANNEL_ID"]),
        approver_id=int(env["DISCORD_APPROVER_USER_ID"]),
        watchlist=load_watchlist(watchlist_path),
        kis_key=env.get("KIS_APP_KEY", ""),
        kis_secret=env.get("KIS_APP_SECRET", ""),
        kis_env=env.get("KIS_ENV", "real"),
        data_dir=env.get("DATA_DIR", "data"),
        poll_minutes=int(env.get("POLL_MINUTES", "10")),
        review_timeout_s=int(env.get("REVIEW_TIMEOUT_S", "1800")),
        summary_max_chars=int(env.get("SUMMARY_MAX_CHARS", "12000")),
        llm_order=env.get("LLM_ORDER", "local,groq,gemini"),
        universe_llm_order=env.get("UNIVERSE_LLM_ORDER", "local"),
    )
