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
DATA_ROOT = Path(os.environ.get("CRASSUS_DATA_ROOT", str(REPO_ROOT)))
DEFAULT_LEDGER_DIR = DATA_ROOT / "logs"
DEFAULT_STATE_DIR = DATA_ROOT / "state"

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


BROKERS = frozenset({"worker", "tastytrade"})


@dataclass
class Account:
    """One bot account. An account *is* a login -- the server has no account_id,
    so identity is the username plus its own cookie jar."""

    alias: str
    username: str
    password: str = field(repr=False)
    strategy_id: str
    params: dict[str, Any] = field(default_factory=dict)
    paper_margin_limit: float = 1.0
    paper_financing_rate: float = 0.0
    # "worker" (paper trading on the Worker, the default) or "tastytrade"
    # (real brokerage orders via crassus/tastytrade_broker.py; credentials and
    # the owner's live approval come only from CRASSUS_TASTY_* env vars).
    broker: str = "worker"

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


def _entries_for(
    path: Path | None,
    catalog_path: Path,
    override_path: Path,
) -> list[dict[str, Any]]:
    """Resolve the exact catalog/override entries the runner would load."""
    configured_path = path or (
        Path(os.environ["CRASSUS_ACCOUNTS_FILE"])
        if os.environ.get("CRASSUS_ACCOUNTS_FILE")
        else None
    )
    if configured_path is not None:
        if not configured_path.exists():
            raise FileNotFoundError(f"No accounts file at {configured_path}.")
        return _read_entries(configured_path)
    if not catalog_path.exists():
        raise FileNotFoundError(f"No bot catalog at {catalog_path}.")
    catalog = _read_entries(catalog_path)
    overrides = _read_entries(override_path) if override_path.exists() else []
    return _merge_entries(catalog, overrides)


def account_gaps(
    path: Path | None = None,
    *,
    catalog_path: Path = DEFAULT_ACCOUNTS_CATALOG,
    override_path: Path = DEFAULT_ACCOUNTS_OVERRIDE,
    environ: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """List active accounts whose credentials are not provisioned.

    Unlike ``load_accounts`` this reports every gap at once, so an operator
    sees all missing Railway variables before a restart loop repeats the first
    one. It never returns or logs a secret value.
    """
    environment = os.environ if environ is None else environ
    gaps: list[dict[str, str]] = []
    for entry in _entries_for(path, catalog_path, override_path):
        if entry.get("active", True) is False:
            continue
        if entry.get("broker", "worker") == "tastytrade":
            # Brokerage-routed: no Worker password. Report which owner-set
            # CRASSUS_TASTY_* variables are absent (names only, never values).
            required = [
                "CRASSUS_TASTY_MODE",
                "CRASSUS_TASTY_ACCOUNT_NUMBER",
                "CRASSUS_TASTY_CLIENT_SECRET",
                "CRASSUS_TASTY_REFRESH_TOKEN",
            ]
            if environment.get("CRASSUS_TASTY_MODE") == "live":
                required.append("CRASSUS_TASTY_LIVE_APPROVAL")
            for env_name in required:
                if not environment.get(env_name):
                    gaps.append({
                        "alias": str(entry.get("alias", "")),
                        "username": str(entry.get("username", "")),
                        "missing": env_name,
                    })
            continue
        if entry.get("password"):
            continue
        env_name = entry.get("password_env")
        if not env_name:
            gaps.append({
                "alias": str(entry.get("alias", "")),
                "username": str(entry.get("username", "")),
                "missing": "no password or password_env",
            })
        elif not environment.get(env_name):
            gaps.append({
                "alias": str(entry.get("alias", "")),
                "username": str(entry.get("username", "")),
                "missing": env_name,
            })
    return gaps


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

    An entry may also set `"active": false` to stay in the tracked catalog
    -- so it still satisfies the "every registered strategy has a
    catalog-backed display card" CI gate (`verify_invariants.py`) and is
    documented in place -- without being provisioned as a live account: its
    password/password_env is never resolved, so it cannot abort startup for
    a credential nobody has created yet. This is the release/activation
    split MOO-161's fixed-window catalog entries need: added to the catalog
    the moment the code lands, but only turned into a running bot once
    Railway actually has its `CRASSUS_PW_*` variable and an operator flips
    it on (either by editing this field or by overriding it back to active
    in the ignored `accounts.json`, e.g. `{"username": ..., "active": true}`
    alongside its password). An entry with no `"active"` key is active by
    default, matching every catalog entry from before this field existed.
    """
    entries = _entries_for(path, catalog_path, override_path)

    if not entries:
        raise FileNotFoundError(
            "No bot accounts are configured. Add one to accounts.example.json "
            "or provide $CRASSUS_ACCOUNTS_FILE."
        )

    accounts = []
    missing: list[str] = []
    for entry in entries:
        if entry.get("active", True) is False:
            continue
        broker = entry.get("broker", "worker")
        if broker not in BROKERS:
            raise ValueError(f"Account {entry['alias']}: broker must be one of {sorted(BROKERS)}, got {broker!r}")
        if broker == "tastytrade":
            if entry.get("password") or entry.get("password_env"):
                raise ValueError(
                    f"Account {entry['alias']}: a tastytrade-routed account takes no Worker password; "
                    "its credentials come only from CRASSUS_TASTY_* environment variables"
                )
            accounts.append(
                Account(
                    alias=entry["alias"],
                    username=entry["username"],
                    password="",
                    strategy_id=entry["strategy_id"],
                    params=entry.get("params", {}),
                    broker=broker,
                )
            )
            continue
        password = entry.get("password")
        if not password and entry.get("password_env"):
            password = os.environ.get(entry["password_env"])
            if not password:
                missing.append(f"{entry['alias']}→${entry['password_env']}")
        if not password:
            if not entry.get("password_env"):
                missing.append(f"{entry['alias']}→no password or password_env")
            continue
        margin_limit = float(entry.get("paper_margin_limit", 1.0))
        financing_rate = float(entry.get("paper_financing_rate", 0.0))
        if not 1.0 <= margin_limit <= 4.0:
            raise ValueError(f"Account {entry['alias']}: paper_margin_limit must be 1..4")
        if not 0.0 <= financing_rate <= 1.0:
            raise ValueError(f"Account {entry['alias']}: paper_financing_rate must be 0..1")
        accounts.append(
            Account(
                alias=entry["alias"],
                username=entry["username"],
                password=password,
                strategy_id=entry["strategy_id"],
                params=entry.get("params", {}),
                paper_margin_limit=margin_limit,
                paper_financing_rate=financing_rate,
            )
        )

    if missing:
        raise ValueError(
            "Missing bot credential configuration: " + "; ".join(missing) +
            ". Provision every listed Railway variable (or mark the account "
            "active:false) before starting the runner; it will not start with "
            "incomplete credentials."
        )

    aliases = [a.alias for a in accounts]
    if len(set(aliases)) != len(aliases):
        raise ValueError(f"Duplicate account aliases: {aliases}")
    usernames = [a.username for a in accounts]
    if len(set(usernames)) != len(usernames):
        raise ValueError(f"Duplicate account usernames: {usernames}")
    return accounts
