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
# The checked-in catalog is the deployable roster. It intentionally contains
# only public bot metadata and environment-variable names, never passwords.
# An ignored accounts.json may override credentials/parameters for local use,
# but it must not replace the catalog: otherwise an old local file silently
# hides every bot added by a later PR.
DEFAULT_ACCOUNTS_CATALOG = REPO_ROOT / "accounts.example.json"
DEFAULT_ACCOUNTS_OVERRIDE = REPO_ROOT / "accounts.json"
DEFAULT_LEDGER_DIR = REPO_ROOT / "logs"
DEFAULT_STATE_DIR = REPO_ROOT / "state"

BASE_URL = os.environ.get("CRASSUS_BASE_URL", "https://options.moopertonic.net")

# Override/kill-switch/freeze/ledger-mirror channel (see crassus/policy.py,
# crassus/overrides_client.py). Same Worker as BASE_URL by default -- a
# separate env var only so a test/mock deployment can point the override
# channel elsewhere without also redirecting live trading traffic.
CRASSUS_AI_OVERRIDES_URL = os.environ.get("CRASSUS_AI_OVERRIDES_URL", BASE_URL)
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


def _read_entries(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text())
    entries = raw.get("accounts")
    if not isinstance(entries, list):
        raise ValueError(f"{path}: top-level 'accounts' must be a list")
    return entries


def _merge_entries(
    catalog: list[dict[str, Any]], overrides: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Overlay a private local file without letting it hide catalog bots.

    Matching is by username, the server-side account identity. Override-only
    accounts are appended so operators can still run private experiments.
    """
    by_username: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for source, entries in (("catalog", catalog), ("override", overrides)):
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("username"), str):
                raise ValueError(f"Account in {source} has no string username: {entry!r}")
            username = entry["username"]
            if source == "catalog":
                if username in by_username:
                    raise ValueError(f"Duplicate account username in catalog: {username!r}")
                by_username[username] = dict(entry)
                order.append(username)
            elif username in by_username:
                by_username[username].update(entry)
            else:
                by_username[username] = dict(entry)
                order.append(username)
    return [by_username[username] for username in order]


def load_accounts(
    path: Path | None = None,
    *,
    catalog_path: Path = DEFAULT_ACCOUNTS_CATALOG,
    override_path: Path = DEFAULT_ACCOUNTS_OVERRIDE,
) -> list[Account]:
    """Load accounts from JSON.

    An explicit path or $CRASSUS_ACCOUNTS_FILE is loaded exactly. Otherwise the
    checked-in accounts.example.json catalog is loaded, with an ignored local
    accounts.json overlaid by username when present. This keeps credentials
    private while ensuring a stale override cannot make newly merged bots
    disappear from the runner and therefore from the Automated tab.

    Each entry may set "password_env" instead of "password" to pull the secret
    from the environment.
    """
    configured_path = path or (
        Path(os.environ["CRASSUS_ACCOUNTS_FILE"])
        if os.environ.get("CRASSUS_ACCOUNTS_FILE")
        else None
    )
    if configured_path is not None:
        if not configured_path.exists():
            raise FileNotFoundError(f"No accounts file at {configured_path}.")
        entries = _read_entries(configured_path)
    else:
        if not catalog_path.exists():
            raise FileNotFoundError(f"No bot catalog at {catalog_path}.")
        catalog = _read_entries(catalog_path)
        overrides = _read_entries(override_path) if override_path.exists() else []
        entries = _merge_entries(catalog, overrides)

    if not entries:
        raise FileNotFoundError(
            "No bot accounts are configured. Add one to accounts.example.json "
            "or provide $CRASSUS_ACCOUNTS_FILE."
        )

    accounts = []
    for entry in entries:
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
    usernames = [a.username for a in accounts]
    if len(set(usernames)) != len(usernames):
        raise ValueError(f"Duplicate account usernames: {usernames}")
    return accounts
