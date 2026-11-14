"""Per-underlying session state, restore, and VWAP/RVOL persistence.

WHY this module: one underlying's collection state (volume deltas, VWAP
accumulator, RVOL baseline, momentum window, last-known price strip) must
never leak into another underlying's -- that is the whole point of #121,
and the module that owns the state is where the rule lives. restore_state
reseeds it from R2 after a restart; the VWAP/RVOL helpers persist it back.
The shared _last_prices strip is single-sourced here: the entry re-exports
this module's dict object, so tests mutating collector._last_prices and
the price/steo/snapshot loops all see the same strip.
"""

import io
import json
import re
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from . import market_signals as ms
from .chain import _dxlink_symbol, _occ_root
from .config import (
    CHAIN_UNDERLYINGS,
    ET,
    R2_BUCKET,
    RVOL_BASELINE_KEY,
    RVOL_BUCKET_MINUTES,
    RVOL_LOOKBACK_DAYS,
    RVOL_MIN_DAYS_REQUIRED,
    SNAPSHOT_SECS,
    TICKER,
    log,
)

# -- upload counters ----------------------------------------------------------

class Counters:
    def __init__(self):
        self._lock = threading.Lock()
        self.prices_ok   = 0
        self.snapshot_ok = 0
        self.csv_ok      = 0
        self.failures    = 0
        self.last_price_time    = None
        self.last_snapshot_time = None

    def inc_prices(self, ts: str):
        with self._lock:
            self.prices_ok += 1
            self.last_price_time = ts

    def inc_snapshot(self, ts: str):
        with self._lock:
            self.snapshot_ok += 1
            self.last_snapshot_time = ts

    def inc_csv(self):
        with self._lock:
            self.csv_ok += 1

    def inc_failure(self):
        with self._lock:
            self.failures += 1

    def get(self) -> dict:
        with self._lock:
            return {
                "prices_ok":          self.prices_ok,
                "snapshot_ok":        self.snapshot_ok,
                "csv_ok":             self.csv_ok,
                "failures":           self.failures,
                "last_price_time":    self.last_price_time,
                "last_snapshot_time": self.last_snapshot_time,
            }


# -- snapshot cadence tracker -------------------------------------------------

class SnapshotTracker:
    def __init__(self):
        self._lock = threading.Lock()
        self.seq                             = 0
        self.expected_next: Optional[datetime] = None
        self.missed                          = 0

    def record(self):
        with self._lock:
            self.seq += 1
            self.expected_next = datetime.now(timezone.utc) + timedelta(seconds=SNAPSHOT_SECS)

    def check_missed(self):
        with self._lock:
            if (self.expected_next is not None and
                    datetime.now(timezone.utc) > self.expected_next + timedelta(seconds=60)):
                self.missed += 1
                log.warning(f"missed snapshot (expected by {self.expected_next.isoformat()})")
                self.expected_next = None

    def get(self) -> dict:
        with self._lock:
            return {
                "snapshot_sequence":          self.seq,
                "expected_next_snapshot_time": (self.expected_next.isoformat()
                                                if self.expected_next else None),
                "missed_snapshot_count":      self.missed,
            }



# -- per-underlying session state -------------------------------------------------

def _fmt_oi(v: int) -> str:
    if v == 0:    return ""
    if v < 1000:  return str(v)
    if v < 10000: return f"{v/1000:.1f}K"
    return f"{v//1000}K"


_last_prices: dict[str, dict] = {}  # label -> {price, quote_ts}; timestamp never refreshed by fallback


class UnderlyingSession:
    """All per-underlying collection state (#121), so SPY and QQQ can never
    share a volume delta, VWAP sum, RVOL bucket, momentum window or archive key.

    QQQ (TICKER) keeps every pre-SPY R2 key; other underlyings publish under
    intraday/<symbol>/ and baselines/<symbol>_rvol_buckets.json, so no
    existing QQQ consumer of intraday/latest.json or intraday/YYYYMMDD/ can
    ever read SPY data.
    """

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.archive_root = "intraday" if symbol == TICKER else f"intraday/{symbol.lower()}"
        self.latest_key = f"{self.archive_root}/latest.json"
        self.rvol_baseline_key = (RVOL_BASELINE_KEY if symbol == TICKER
                                  else f"baselines/{symbol.lower()}_rvol_buckets.json")
        self.prev_vol: dict[str, int] = {}     # persists across calls to compute per-minute delta
        self.last_spot: list = [None, None]     # [price|None, observed_at_iso|None] -- yfinance/CSV fallback
        self.subscription_coverage: dict | None = None  # SPY's fixed startup strike window
        self.first_snapshot_written = False     # guard so first.csv is only written once per session
        self.session_date: date | None = None
        self.vwap_state: "ms.VwapState" = ms.VwapState()  # session-scoped VWAP accumulator, see market_signals.py
        self.rvol_baseline: dict = {}           # loaded once per session from rvol_baseline_key
        self.rvol_today: dict[str, int] = {}    # bucket_label -> latest session cum_volume in that bucket
        # Rolling window of recent spot observations for the display-only
        # momentum reading (see market_signals.py's compute_time_series_momentum).
        # Unlike vwap_state, this needs no restart-recovery persistence: it's a
        # bounded real-time window that self-heals within
        # ~MOMENTUM_LOOKBACK_MINUTES of any restart.
        self.momentum_history: list = []        # list[ms.SpotPoint]

    def day_prefix(self, date_str: str) -> str:
        return f"{self.archive_root}/{date_str}/"


QQQ_SESSION = UnderlyingSession(TICKER)
UNDERLYING_SESSIONS: dict[str, UnderlyingSession] = {
    sym: (QQQ_SESSION if sym == TICKER else UnderlyingSession(sym)) for sym in CHAIN_UNDERLYINGS
}


# -- archive listing ----------------------------------------------------------
#
# One minute snapshot writes two objects under its day prefix (the CSV and its
# `snapshot_provenance.v1` sidecar), so a single session already runs to ~1,800
# keys for QQQ -- well past S3's 1,000-key list page. `list_objects_v2` returns
# keys in ascending order and stops at the page boundary, so an unpaginated
# call sees the *oldest* page; taking the last CSV of that page restores from a
# mid-morning snapshot while reporting success. Page to the end instead.

LIST_PAGE_LIMIT = 50                     # 50,000 keys; a day prefix is orders below this
_SNAPSHOT_NAME_RE = re.compile(r"snapshot_(\d{4}|\d{6}|\d{12})\.csv$")


def _list_prefix(s3, prefix: str, max_pages: int = LIST_PAGE_LIMIT) -> list[str]:
    """Every key under `prefix`, following continuation tokens to the end.

    Raises rather than returning a partial listing: a truncated prefix is
    indistinguishable from a complete one at the call site, and restoring from
    the wrong snapshot is worse than not restoring at all -- the same "prefer a
    clean miss over corrupt state" rule this module already applies to CSV
    contents. The caller logs the miss and starts a fresh session.
    """
    keys: list[str] = []
    token = None
    for _ in range(max_pages):
        request = {"Bucket": R2_BUCKET, "Prefix": prefix}
        if token:
            request["ContinuationToken"] = token
        response = s3.list_objects_v2(**request)
        keys.extend(item["Key"] for item in response.get("Contents", []) if "Key" in item)
        if not response.get("IsTruncated"):
            return keys
        token = response.get("NextContinuationToken")
        if not token:
            raise RuntimeError(f"archive listing truncated without a continuation token: {prefix}")
    raise RuntimeError(f"archive listing exceeded {max_pages} pages: {prefix}")


def _snapshot_digits(name: str) -> Optional[str]:
    """The 12-digit HHMMSSffffff digits of one snapshot basename, or None.

    Older archives wrote fewer digits (`snapshot_1200.csv`), and the reader in
    crassus/crassus/market.py accepts 4, 6 or 12. Right-padding to 12 makes
    every accepted width comparable on one scale and derivable back to a
    capture time: `1200` becomes `120000000000`, not something that sorts
    before `0930...` as plain string ordering of mixed widths would.
    """
    match = _SNAPSHOT_NAME_RE.fullmatch(name)
    return match.group(1).ljust(12, "0") if match else None


def _snapshot_order(name: str) -> int:
    """Order one `snapshot_<digits>.csv` basename by capture time."""
    digits = _snapshot_digits(name)
    return int(digits) if digits is not None else -1


def _latest_snapshot_key(keys: list[str], prefix: str) -> Optional[str]:
    """The newest timestamped snapshot CSV under `prefix`, or None.

    Only `snapshot_<digits>.csv` qualifies. `first.csv`, the provenance
    sidecars, `vwap_state.json`, `momentum_log.jsonl` and anything else that
    happens to live in the prefix are never a restore source, so a stray CSV
    cannot displace the real latest snapshot. `prefix` carries the underlying
    and the session date, so a key from another symbol's or another day's
    archive cannot match.
    """
    candidates = [
        key for key in keys
        if key.startswith(prefix) and _snapshot_digits(key[len(prefix):]) is not None
    ]
    return max(candidates, key=lambda key: _snapshot_order(key[len(prefix):])) if candidates else None


def restore_state(s3, today: date, us: "UnderlyingSession | None" = None) -> None:
    """Seed one underlying's prev_vol/last_spot (and, for QQQ only, the shared
    _last_prices strip) from its most recent R2 snapshot.

    Called at session start so a redeploy doesn't blank VolDelta for one beat
    or lose price context.
    """
    us = us or QQQ_SESSION
    if us.session_date != today:
        # This object survives across collection days in the same process.
        # Start each underlying's new archive day with fresh per-day state
        # before recovery. QQQ was previously excluded, so a long-lived process
        # kept QQQ's first-snapshot guard set and wrote intraday/YYYYMMDD/first.csv
        # only on the first day after a restart.
        us.prev_vol = {}
        us.last_spot = [None, None]
        us.first_snapshot_written = False
        us.vwap_state = ms.VwapState()
        us.rvol_today = {}
        us.momentum_history = []
        us.subscription_coverage = None
        us.session_date = today
    date_str = today.strftime("%Y%m%d")
    try:
        prefix = us.day_prefix(date_str)
        listed = _list_prefix(s3, prefix)
        latest_key = _latest_snapshot_key(listed, prefix)
        if latest_key is None:
            log.info(
                f"restore_state: no {us.symbol} snapshot CSV under {prefix} "
                f"({len(listed)} objects listed), starting fresh"
            )
            return

        us.first_snapshot_written = True  # prior snapshots exist; first.csv already written

        body = s3.get_object(Bucket=R2_BUCKET, Key=latest_key)["Body"].read().decode()
        df = pd.read_csv(io.StringIO(body))

        # Restore prev_vol from Volume column keyed by OptionSymbol
        own_rows = df[df["OptionSymbol"].map(
            lambda sym: isinstance(sym, str) and _occ_root(sym) == us.symbol
        )] if "OptionSymbol" in df.columns else df.iloc[0:0]
        if "Volume" in own_rows.columns:
            for _, row in own_rows.iterrows():
                sym = row.get("OptionSymbol")
                vol = row.get("Volume")
                if sym and pd.notna(vol):
                    # Convert OCC symbol back to dxFeed format used as state key
                    try:
                        dx_sym = _dxlink_symbol(str(sym))
                        us.prev_vol[dx_sym] = int(vol)
                    except Exception:
                        pass

        # Restore last_spot, paired with the timestamp encoded in the
        # snapshot's own filename (snapshot_HHMMSSffffff.csv) -- not
        # datetime.now() at restore time, which would misrepresent how old
        # this recovered price actually is.
        if "UnderlyingPrice" in own_rows.columns:
            prices = own_rows["UnderlyingPrice"].dropna()
            spot = prices.iloc[-1] if not prices.empty else None
            if spot:
                us.last_spot[0] = float(spot)
                us.last_spot[1] = None
                name_match = _snapshot_digits(latest_key.rsplit("/", 1)[-1])
                if name_match:
                    hms, micro = name_match[:6], name_match[6:]
                    try:
                        spot_dt = ET.localize(datetime(
                            today.year, today.month, today.day,
                            int(hms[0:2]), int(hms[2:4]), int(hms[4:6]), int(micro),
                        ))
                        us.last_spot[1] = spot_dt.astimezone(timezone.utc).isoformat()
                    except ValueError:
                        pass

        # Restore the shared _last_prices strip from QQQ's price columns only
        # (any col not in core option fields).
        core_cols = {"TradeDate","Expiration","Strike","Type","OptionSymbol","DTE",
                     "OpenInterest","Volume","VolDelta","Bid","Mid","Ask","Last",
                     "IV","Delta","Gamma","Theta","Vega","UnderlyingPrice"}
        for col in (df.columns if us is QQQ_SESSION else ()):
            if col not in core_cols:
                val = df[col].dropna().iloc[-1] if not df[col].dropna().empty else None
                if val is not None:
                    label = col.replace("_", "/") if col in ("JPY_USD", "BTC_USD") else col
                    _last_prices[label] = {"price": float(val), "quote_ts": None}

        _restore_vwap_state(s3, today, date_str, us)

        log.info(
            f"restore_state: {us.symbol} loaded {latest_key.split('/')[-1]} -- "
            f"vol_keys={len(us.prev_vol)}  spot={us.last_spot[0]}  prices={len(_last_prices)}  "
            f"listed={len(listed)}"
        )
    except Exception as e:
        log.warning(f"restore_state failed for {us.symbol} (non-fatal): {e}")


def _vwap_state_key(date_str: str, us: "UnderlyingSession | None" = None) -> str:
    return f"{(us or QQQ_SESSION).day_prefix(date_str)}vwap_state.json"


def _restore_vwap_state(s3, today: date, date_str: str, us: "UnderlyingSession | None" = None) -> None:
    """Best-effort recovery of `vwap_state`'s running sums after a mid-session
    restart. Written every snapshot cycle purely as a restart-recovery aid --
    nothing else reads this object. Without this, a Railway restart mid-day
    would silently reset VWAP to a fresh (lower-sample) accumulation with no
    indication in the payload that this happened.
    """
    us = us or QQQ_SESSION
    try:
        body = s3.get_object(Bucket=R2_BUCKET, Key=_vwap_state_key(date_str, us))["Body"].read()
        data = json.loads(body)
        if data.get("session_date") != today.isoformat():
            return  # stale leftover from a prior day; a fresh accumulator is correct
        if data.get("symbol", TICKER) != us.symbol:
            log.warning(f"restore_vwap_state: {us.symbol} key holds {data.get('symbol')} state -- ignored")
            return
        session_started_at = data.get("session_started_at")
        last_observed_at = data.get("last_observed_at")
        us.vwap_state = ms.VwapState(
            session_date=today,
            session_started_at=datetime.fromisoformat(session_started_at) if session_started_at else None,
            last_observed_at=datetime.fromisoformat(last_observed_at) if last_observed_at else None,
            cum_pv=data.get("cum_pv", 0.0),
            cum_vol=data.get("cum_vol", 0),
            last_dayvolume=data.get("last_dayvolume"),
            vwap=data.get("vwap"),
            vwap_ts=data.get("vwap_ts"),
        )
        log.info(f"restore_state: recovered {us.symbol} vwap_state -- cum_vol={us.vwap_state.cum_vol}  vwap={us.vwap_state.vwap}")
    except Exception as e:
        log.info(f"restore_vwap_state: nothing to recover ({e})")


def _persist_vwap_state(s3, date_str: str, us: "UnderlyingSession | None" = None) -> None:
    """Write `vwap_state`'s running sums so a mid-session restart can recover
    them via `_restore_vwap_state`. Best-effort -- a failure here must never
    interrupt the snapshot it's piggybacking on.
    """
    us = us or QQQ_SESSION
    vs = us.vwap_state
    try:
        data = {
            "symbol": us.symbol,
            "session_date": vs.session_date.isoformat() if vs.session_date else None,
            "session_started_at": vs.session_started_at.isoformat() if vs.session_started_at else None,
            "last_observed_at": vs.last_observed_at.isoformat() if vs.last_observed_at else None,
            "cum_pv": vs.cum_pv,
            "cum_vol": vs.cum_vol,
            "last_dayvolume": vs.last_dayvolume,
            "vwap": vs.vwap,
            "vwap_ts": vs.vwap_ts,
        }
        s3.put_object(
            Bucket=R2_BUCKET, Key=_vwap_state_key(date_str, us),
            Body=json.dumps(data).encode(),
            ContentType="application/json",
            CacheControl="no-cache, max-age=0",
        )
    except Exception as e:
        log.warning(f"vwap_state.json upload failed (non-fatal): {e}")


def load_rvol_baseline(s3, us: "UnderlyingSession | None" = None) -> dict:
    """Best-effort load of the cross-day RVOL baseline. Missing file (first
    deploy of this feature) is not an error -- every bucket simply reports
    "insufficient_history"/"no_data" until enough sessions have run.
    """
    us = us or QQQ_SESSION
    try:
        body = s3.get_object(Bucket=R2_BUCKET, Key=us.rvol_baseline_key)["Body"].read()
        data = json.loads(body)
        if data.get("symbol", TICKER) != us.symbol:
            raise ValueError(f"baseline belongs to {data.get('symbol')}")
        log.info(f"load_rvol_baseline: {us.symbol} loaded, updated_through={data.get('updated_through')}")
        return data
    except Exception as e:
        log.info(f"load_rvol_baseline: no {us.symbol} baseline yet ({e}) -- starting fresh")
        return {
            "symbol": us.symbol,
            "bucket_minutes": RVOL_BUCKET_MINUTES,
            "lookback_days": RVOL_LOOKBACK_DAYS,
            "min_days_required": RVOL_MIN_DAYS_REQUIRED,
            "updated_through": None,
            "buckets": {},
        }


def finalize_rvol_baseline(s3, today: date, us: "UnderlyingSession | None" = None) -> None:
    """Fold today's completed-session bucket readings into the baseline.

    Called once, at end-of-session -- never mid-session -- so the baseline
    file only ever reflects complete trading days; a mid-session crash before
    this runs simply means today's readings are lost for the baseline (not
    corrupted into it), the same "prefer a clean miss over corrupt state"
    tradeoff `restore_state` already makes for CSV-derived state.
    """
    us = us or QQQ_SESSION
    if not us.rvol_today:
        log.info(f"finalize_rvol_baseline: no {us.symbol} bucket readings this session, skipping")
        return
    try:
        baseline = load_rvol_baseline(s3, us)
        buckets = baseline.setdefault("buckets", {})
        for bucket, cum_volume in us.rvol_today.items():
            samples = buckets.get(bucket, {}).get("samples", [])
            samples = ms.prune_baseline_samples(samples, today, baseline.get("lookback_days", RVOL_LOOKBACK_DAYS))
            samples = ms.append_session_reading(samples, today, cum_volume)
            buckets[bucket] = {"samples": samples}
        baseline["updated_through"] = today.isoformat()
        baseline["bucket_minutes"] = RVOL_BUCKET_MINUTES
        baseline["lookback_days"] = RVOL_LOOKBACK_DAYS
        baseline["min_days_required"] = RVOL_MIN_DAYS_REQUIRED
        s3.put_object(
            Bucket=R2_BUCKET, Key=us.rvol_baseline_key,
            Body=json.dumps(baseline).encode(),
            ContentType="application/json",
            CacheControl="no-cache, max-age=0",
        )
        log.info(f"finalize_rvol_baseline: wrote {len(us.rvol_today)} {us.symbol} bucket readings for {today.isoformat()}")
    except Exception as e:
        log.warning(f"finalize_rvol_baseline failed (non-fatal): {e}")


def _momentum_log_key(date_str: str, us: "UnderlyingSession | None" = None) -> str:
    return f"{(us or QQQ_SESSION).day_prefix(date_str)}momentum_log.jsonl"


def _log_momentum_reading(s3, date_str: str, entry: dict, us: "UnderlyingSession | None" = None) -> None:
    """Append one momentum reading to a rolling per-day JSONL log in R2 --
    a public, queryable historical record of the display signal, independent
    of the private crassus trading bot's own decision ledger (which is a
    local file wherever the runner executes, never published -- see
    docs/plans/2026-07-momentum-indicator.md). Read-modify-write is fine at
    this volume: at most one line per SNAPSHOT_SECS-second cycle, capped at
    roughly 390 short lines/day. Best-effort -- a failure here must never
    interrupt the snapshot it's piggybacking on.
    """
    try:
        key = _momentum_log_key(date_str, us)
        try:
            existing = s3.get_object(Bucket=R2_BUCKET, Key=key)["Body"].read().decode()
        except Exception:
            existing = ""
        s3.put_object(
            Bucket=R2_BUCKET, Key=key,
            Body=(existing + json.dumps(entry) + "\n").encode(),
            ContentType="application/x-ndjson",
            CacheControl="no-cache, max-age=0",
        )
    except Exception as e:
        log.warning(f"momentum_log.jsonl append failed (non-fatal): {e}")



__all__ = [
    "Counters",
    "SnapshotTracker",
    "_fmt_oi",
    "_last_prices",
    "UnderlyingSession",
    "QQQ_SESSION",
    "UNDERLYING_SESSIONS",
    "LIST_PAGE_LIMIT",
    "_list_prefix",
    "_snapshot_order",
    "_latest_snapshot_key",
    "restore_state",
    "_vwap_state_key",
    "_restore_vwap_state",
    "_persist_vwap_state",
    "load_rvol_baseline",
    "finalize_rvol_baseline",
    "_momentum_log_key",
    "_log_momentum_reading",
]
