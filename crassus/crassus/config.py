"""Account credentials and runtime configuration.

Credentials come from an ignored local file or the environment -- never from
source control, and never from an audit record. `Account.__repr__` is
deliberately redacted so a password cannot reach a log by accident.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ACCOUNTS_FILE = REPO_ROOT / "accounts.json"
DEFAULT_LEDGER_DIR = REPO_ROOT / "logs"
DEFAULT_STATE_DIR = REPO_ROOT / "state"

BASE_URL = os.environ.get("CRASSUS_BASE_URL", "https://options.moopertonic.net")
SNAPSHOT_URL = os.environ.get(
    "CRASSUS_SNAPSHOT_URL",
    "https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev/intraday/latest.json",
)

# Operator key that marks an account as a bot on the Worker. Registering with
# this in an X-Bot-Registration-Key header puts the account in the `bot:` index
# behind the public /api/bots roster (the site's Automated tab); registering
# without it silently creates an ordinary human account that never appears
# there, which is why the runner treats a missing key as a startup error rather
# than a default.
BOT_REGISTRATION_KEY = os.environ.get("BOT_REGISTRATION_KEY")

# User-Agent for the reddit_sentiment_qqq strategy's scraper (crassus/sentiment.py).
# Reddit's public per-subreddit `new.json` listing needs no app registration or
# credentials -- this is just an honest identifier sent with each request, not
# a secret. Unset falls back to a generic default rather than blocking the
# strategy.
REDDIT_USER_AGENT = os.environ.get("REDDIT_USER_AGENT")

# User-Agent for the trump_whisperer_qqq strategy's feed reader
# (crassus/trump_sentiment.py). trumpstruth.org/feed is a public, unauthenticated
# RSS mirror -- no credentials to provision, same as REDDIT_USER_AGENT above.
TRUMP_FEED_USER_AGENT = os.environ.get("TRUMP_FEED_USER_AGENT")


@dataclass
class Account:
    """One bot account. An account *is* a login -- the server has no account_id,
    so identity is the username plus its own cookie jar."""

    alias: str
    username: str
    password: str = field(repr=False)
    strategy_id: str
    params: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:  # keep passwords out of tracebacks and logs
        return f"Account(alias={self.alias!r}, username={self.username!r}, strategy_id={self.strategy_id!r})"


def load_accounts(path: Path | None = None) -> list[Account]:
    """Load accounts from JSON.

    Resolution order: explicit path, then $CRASSUS_ACCOUNTS_FILE, then
    ./accounts.json. Each entry may set "password_env" instead of "password"
    to pull the secret from the environment.
    """
    path = path or Path(os.environ.get("CRASSUS_ACCOUNTS_FILE", DEFAULT_ACCOUNTS_FILE))
    if not path.exists():
        raise FileNotFoundError(
            f"No accounts file at {path}. Copy accounts.example.json to accounts.json "
            f"(it is gitignored) or set $CRASSUS_ACCOUNTS_FILE."
        )

    raw = json.loads(path.read_text())
    accounts = []
    for entry in raw["accounts"]:
        password = entry.get("password")
        if not password and entry.get("password_env"):
            password = os.environ.get(entry["password_env"])
            if not password:
                raise ValueError(
                    f"Account {entry['alias']}: ${entry['password_env']} is not set"
                )
        if not password:
            raise ValueError(f"Account {entry['alias']}: no password or password_env")
        accounts.append(
            Account(
                alias=entry["alias"],
                username=entry["username"],
                password=password,
                strategy_id=entry["strategy_id"],
                params=entry.get("params", {}),
            )
        )

    aliases = [a.alias for a in accounts]
    if len(set(aliases)) != len(aliases):
        raise ValueError(f"Duplicate account aliases: {aliases}")
    return accounts
