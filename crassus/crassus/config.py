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

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ACCOUNTS_FILE = REPO_ROOT / "accounts.json"
DEFAULT_LEDGER_DIR = REPO_ROOT / "logs"
DEFAULT_STATE_DIR = REPO_ROOT / "state"

BASE_URL = os.environ.get("CRASSUS_BASE_URL", "https://options.moopertonic.net")
SNAPSHOT_URL = os.environ.get(
    "CRASSUS_SNAPSHOT_URL",
    "https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev/intraday/latest.json",
)

# Reddit API credentials for the reddit_sentiment_qqq strategy (crassus/sentiment.py).
# A read-only PRAW "script" app -- see https://www.reddit.com/prefs/apps/ -- never
# from source control. Unset means that strategy declines to trade instead of crashing.
REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT = os.environ.get("REDDIT_USER_AGENT")


@dataclass
class Account:
    """One bot account. An account *is* a login -- the server has no account_id,
    so identity is the username plus its own cookie jar."""

    alias: str
    username: str
    password: str = field(repr=False)
    strategy_id: str

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
            )
        )

    aliases = [a.alias for a in accounts]
    if len(set(aliases)) != len(aliases):
        raise ValueError(f"Duplicate account aliases: {aliases}")
    return accounts
