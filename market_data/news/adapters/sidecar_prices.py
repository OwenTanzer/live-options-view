"""Read-only adapter: collector provenance sidecars -> underlying_quote price records.

Input: the live collector's `intraday/YYYYMMDD/snapshot_<digits>.provenance.json`
objects (schema `snapshot_provenance.v1`, written by market_data/live_market/market.py).
Output: `underlying_quote` records in the prices.py contract, for the news/price join.

Only LAST-TRADE selections become records. A sidecar's `underlying` block is
the collector's selected spot; it is a last trade only when the collector had
no two-sided quote (`price_basis == "last_trade"`, `selection_rule ==
"last_trade"`). Midpoints (`bid_ask_midpoint`, `bid_ask_midpoint_unstamped`),
remembered spots and unavailable spots are excluded and counted by reason:
a midpoint has no provider time (`provider_observed_at` is null on both sides),
so it cannot carry a `quote_ts` without a contract change, and none is made here.

Each record keeps three times separately and never derives one from another:

  quote_ts              provider event time of the trade  (underlying.provider_observed_at,
                        which must equal contributing.last_observed_at)
  collector_received_at collector receipt of that trade   (underlying.collector_received_at,
                        which must equal contributing.last_ts); prices.py makes this the
                        record's available_at
  archive_available_at  when the sidecar reached the archive (archive_uploaded_at)

A sidecar missing any of the three, or whose times are out of order, is
rejected; nothing is filled in. `session` is derived from the exchange calendar
(market_data.live_market.price_sessions: NYSE trading days, explicit early-close
table, 04:00/09:30/16:00/20:00 ET) at the provider time; a trade outside an
equity session is rejected.

Access is read-only and limited to `intraday/YYYYMMDD/` prefixes: the bucket
source exposes list and get only, and refuses any other prefix.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

SIDECAR_SCHEMA = "snapshot_provenance.v1"
UNDERLYING_SCHEMA = "underlying_price.v1"
INSTRUMENT_ID = "etf.qqq"
SYMBOL = "QQQ"
SOURCE = "live_market_collector:dxlink"
SIDECAR_SUFFIX = ".provenance.json"
PREFIX_RE = re.compile(r"^intraday/[0-9]{8}/$")
KEY_RE = re.compile(r"^intraday/([0-9]{8})/snapshot_([0-9]+)\.provenance\.json$")
# price_sessions statuses -> prices.py SESSIONS
SESSION_MAP = {"premarket": "pre", "regular": "regular", "afterhours": "post"}
# what a collector-side `session` value may say about the derived session
SOURCE_SESSION_COMPAT = {"regular": {"regular"}, "pre": {"pre"}, "premarket": {"pre"},
                         "post": {"post"}, "afterhours": {"post"}, "extended": {"pre", "post"}}
HEADER_LABEL = ("LIVE collector sidecars: QQQ last-trade selections only "
                "(midpoints, remembered and unavailable spots excluded)")


class SidecarAccessError(ValueError):
    pass


@dataclass
class AdapterResult:
    records: list[dict] = field(default_factory=list)
    rejected: Counter = field(default_factory=Counter)
    rejected_keys: dict[str, list[str]] = field(default_factory=dict)
    sidecars_read: int = 0

    def reject(self, key: str, reason: str) -> None:
        self.rejected[reason] += 1
        self.rejected_keys.setdefault(reason, []).append(key)

    def header(self, source: str) -> dict:
        return {"record_kind": "fixture_header", "synthetic": False, "label": HEADER_LABEL,
                "instrument_id": INSTRUMENT_ID, "source": source, "adapter": "sidecar_prices/v1",
                "sidecars_read": self.sidecars_read, "records": len(self.records),
                "rejected": dict(sorted(self.rejected.items()))}


# -- times -----------------------------------------------------------------------------------

def _aware(value) -> datetime | None:
    """An offset-carrying ISO time as UTC; naive or unparseable -> None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None or t.utcoffset() is None:
        return None
    return t.astimezone(timezone.utc)


def _z(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# -- calendar --------------------------------------------------------------------------------

class SessionDeriver:
    """Session at an instant from the collector's own equity calendar.

    `calendar_for(et)` returns a price_sessions.EquityCalendar covering et's
    date (default: build_equity_calendar, i.e. pandas_market_calendars NYSE);
    calendars are cached per ET date. Tests pass a fixed calendar.
    """

    def __init__(self, calendar_for: Callable | None = None):
        from market_data.live_market import price_sessions as ps
        self._ps = ps
        self._calendar_for = calendar_for or (lambda et: ps.build_equity_calendar(et))
        self._cache: dict[date, object] = {}

    def __call__(self, t: datetime) -> tuple[str | None, str | None]:
        """(session, None) or (None, rejection reason)."""
        et = t.astimezone(self._ps.ET)
        day = et.date()
        if day not in self._cache:
            try:
                self._cache[day] = self._calendar_for(et)
            except Exception:  # calendar lookup failure: fail closed for this record
                self._cache[day] = None
        state = self._ps.equity_session(et, self._cache[day])
        if state.status == "unknown":
            return None, "calendar_unavailable"
        session = SESSION_MAP.get(state.status)
        if session is None:
            return None, "outside_exchange_session"
        return session, None


# -- mapping ---------------------------------------------------------------------------------

def map_sidecar(key: str, sidecar, derive_session: Callable) -> tuple[dict | None, str | None]:
    """One sidecar -> (record, None) or (None, rejection reason). Never fills a missing time."""
    m = KEY_RE.match(key)
    if not m:
        return None, "key_not_a_sidecar"
    if not isinstance(sidecar, dict) or sidecar.get("schema_version") != SIDECAR_SCHEMA:
        return None, "schema_not_snapshot_provenance_v1"
    snap = sidecar.get("snapshot_key")
    if not isinstance(snap, str) or not snap.endswith(".csv") or snap[:-4] + SIDECAR_SUFFIX != key:
        return None, "snapshot_key_mismatch"
    if sidecar.get("symbol") != SYMBOL:
        return None, "symbol_not_qqq"
    u = sidecar.get("underlying")
    if not isinstance(u, dict) or u.get("schema_version") != UNDERLYING_SCHEMA:
        return None, "underlying_schema_missing"
    basis = u.get("price_basis")
    if u.get("price") is None or basis is None:
        return None, "underlying_unavailable"
    if basis != "last_trade":
        return None, f"excluded_basis:{basis}"
    if u.get("selection_rule") != "last_trade":
        return None, "selection_rule_not_last_trade"
    price = u.get("price")
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
        return None, "price_not_finite_positive"
    c = u.get("contributing") if isinstance(u.get("contributing"), dict) else {}

    provider_raw, receipt_raw = u.get("provider_observed_at"), u.get("collector_received_at")
    archive_raw = sidecar.get("archive_uploaded_at")
    if not provider_raw:
        return None, "missing_provider_time"
    if not receipt_raw:
        return None, "missing_collector_receipt"
    if not archive_raw:
        return None, "missing_archive_upload_time"
    provider, receipt, archive = _aware(provider_raw), _aware(receipt_raw), _aware(archive_raw)
    if provider is None or receipt is None or archive is None:
        return None, "time_unparseable_or_without_offset"
    # The selected value's times must be the last trade's own contributing times.
    if _aware(c.get("last_observed_at")) != provider:
        return None, "provider_time_not_last_trade"
    if _aware(c.get("last_ts")) != receipt:
        return None, "receipt_not_last_trade"
    if receipt < provider:
        return None, "receipt_before_provider_time"
    if archive < receipt:
        return None, "archive_upload_before_receipt"

    session, why = derive_session(provider)
    if why:
        return None, why
    source_session = u.get("session")
    if source_session is not None and session not in SOURCE_SESSION_COMPAT.get(source_session, set()):
        return None, "session_conflict"

    day, digits = m.group(1), m.group(2)
    return {
        "record_id": f"sc-{day}-{digits}",
        "record_kind": "underlying_quote",
        "instrument_id": INSTRUMENT_ID,
        "granularity": "tick",
        "price": float(price),
        "price_basis": "last_trade",
        "session": session,
        "session_basis": "exchange_calendar_at_provider_time",
        "source_session": source_session,
        "quote_ts": _z(provider),
        "collector_received_at": _z(receipt),
        "available_at": _z(receipt),          # prices.py: a quote's available_at is its receipt
        "archive_available_at": _z(archive),  # kept separately; never replaces available_at
        "source": SOURCE,
        "source_symbol": SYMBOL,
        "source_object": key,
        "snapshot_key": snap,
        "synthetic": False,
    }, None


def adapt(items: Iterable[tuple[str, bytes]], derive_session: Callable) -> AdapterResult:
    """(key, raw bytes) pairs -> records sorted by (available_at, quote_ts, record_id) + rejections."""
    out, seen = AdapterResult(), set()
    for key, raw in items:
        out.sidecars_read += 1
        try:
            sidecar = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            out.reject(key, "not_json")
            continue
        rec, why = map_sidecar(key, sidecar, derive_session)
        if why:
            out.reject(key, why)
            continue
        if rec["record_id"] in seen:
            out.reject(key, "duplicate_record_id")
            continue
        seen.add(rec["record_id"])
        out.records.append(rec)
    out.records.sort(key=lambda r: (r["available_at"], r["quote_ts"], r["record_id"]))
    return out


def write_jsonl(result: AdapterResult, path, source: str) -> None:
    lines = [json.dumps(result.header(source), sort_keys=True)]
    lines += [json.dumps(r, sort_keys=True) for r in result.records]
    Path(path).write_bytes(("\n".join(lines) + "\n").encode("utf-8"))  # LF on every platform


# -- sources (read-only) ---------------------------------------------------------------------

def check_prefix(prefix: str) -> str:
    if not isinstance(prefix, str) or not PREFIX_RE.match(prefix):
        raise SidecarAccessError(f"refused prefix {prefix!r}: only intraday/YYYYMMDD/ may be listed or read")
    return prefix


def local_sidecars(root) -> Iterator[tuple[str, bytes]]:
    """Sidecars under a local mirror laid out as <root>/intraday/YYYYMMDD/*.provenance.json."""
    root = Path(root)
    for p in sorted(root.glob("intraday/*/*" + SIDECAR_SUFFIX)):
        yield p.relative_to(root).as_posix(), p.read_bytes()


class BucketSidecars:
    """List + get only, under intraday/YYYYMMDD/. No write, delete or other prefix exists here."""

    def __init__(self, client, bucket: str):
        self._client, self._bucket = client, bucket

    def keys(self, prefix: str) -> list[str]:
        check_prefix(prefix)
        keys = []
        for page in self._client.get_paginator("list_objects_v2").paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                k = obj["Key"]
                if k.startswith(prefix) and k.endswith(SIDECAR_SUFFIX):
                    keys.append(k)
        return sorted(keys)

    def get(self, key: str) -> bytes:
        m = KEY_RE.match(key)
        if not m:
            raise SidecarAccessError(f"refused key {key!r}: not an intraday sidecar")
        check_prefix(f"intraday/{m.group(1)}/")
        return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()

    def sidecars(self, day: str) -> Iterator[tuple[str, bytes]]:
        for k in self.keys(f"intraday/{day}/"):
            yield k, self.get(k)

    @classmethod
    def from_env(cls, env=os.environ) -> "BucketSidecars":
        """PRICE_SIDECAR_R2_{ACCOUNT_ID,ACCESS_KEY_ID,SECRET_ACCESS_KEY,BUCKET_NAME}: read/list on intraday/ only."""
        names = ("ACCOUNT_ID", "ACCESS_KEY_ID", "SECRET_ACCESS_KEY", "BUCKET_NAME")
        missing = [n for n in names if not env.get("PRICE_SIDECAR_R2_" + n)]
        if missing:
            raise SidecarAccessError("missing " + ", ".join("PRICE_SIDECAR_R2_" + n for n in missing))
        import boto3
        client = boto3.client("s3", endpoint_url=f"https://{env['PRICE_SIDECAR_R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                              aws_access_key_id=env["PRICE_SIDECAR_R2_ACCESS_KEY_ID"],
                              aws_secret_access_key=env["PRICE_SIDECAR_R2_SECRET_ACCESS_KEY"], region_name="auto")
        return cls(client, env["PRICE_SIDECAR_R2_BUCKET_NAME"])
