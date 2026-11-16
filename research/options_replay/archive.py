"""Read-only archive access and day packs for the options Replay (OA-237 S1).

Source: the live collector's intraday chain snapshots in R2,
`intraday/{YYYYMMDD}/snapshot_{HHMMSSffffff}.csv` (filename time is US/Eastern
wall-clock; `provenance.constructed_at` confirms it). Each snapshot is one 0DTE
QQQ chain at one moment: per-contract Bid/Ask plus the collector's
`UnderlyingPrice` for that same snapshot. Rows carry no per-quote timestamp, so
a quote's age is the age of the snapshot that holds it.

This module only ever calls `list_objects_v2` and `get_object`. It never writes
to R2, KV, D1 or the Worker; everything it fetches is cached under a local,
disposable directory (temporary storage is all S1 needs).

A *day pack* is the compact, server-side form of one session: for every
snapshot inside the regular session, the time of day, the recorded underlying,
the parity-implied underlying, a usability verdict with its reason, and the
0DTE bid/ask board. Packs contain the date; they are never sent to the browser
before reveal.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time
from pathlib import Path
from statistics import median
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "crassus"))
try:  # same NYSE calendar the bots use (holidays + 13:00 early closes)
    from crassus.exchange_calendar import is_holiday, session_close
except Exception:  # pragma: no cover - degrade to a plain 16:00 close
    def is_holiday(_d: date) -> bool:  # type: ignore[no-redef]
        return False

    def session_close(_d: date) -> time:  # type: ignore[no-redef]
        return time(16, 0)

SYMBOL = os.environ.get('OPTIONS_TRAINER_SYMBOL', 'QQQ')
if SYMBOL not in {'SPY', 'QQQ'}:
    raise ValueError('Unsupported trainer symbol')
PREFIX = 'intraday/spy/' if SYMBOL == 'SPY' else 'intraday/'
PACK_VERSION = 4
SESSION_OPEN_S = 9 * 3600 + 30 * 60
# A snapshot older than this, at the replay clock, no longer quotes anything.
MAX_QUOTE_AGE_S = 150
# Parity tolerance: |K + Cmid - Pmid - UnderlyingPrice| must be within this
# many dollars plus half the two legs' combined spread for the snapshot's
# underlying to count as parity-consistent with its option quotes.
PARITY_TOL_USD = 0.15
DEFAULT_CACHE = Path(os.environ.get("OPTIONS_REPLAY_CACHE",
                                    REPO_ROOT / "research" / "options_replay" / (".cache/spy" if SYMBOL == "SPY" else ".cache")))


# ---------------------------------------------------------------------------
# R2 (read-only)
# ---------------------------------------------------------------------------

def _load_env_file(path: Path) -> None:
    """Fill missing R2_* vars from a dotenv-style file (values never logged)."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("R2_") and k not in os.environ:
            os.environ[k] = v.strip().strip('"').strip("'")


def make_s3():
    env_file = os.environ.get("OPTIONS_REPLAY_ENV_FILE")
    if env_file:
        _load_env_file(Path(env_file))
    import boto3  # imported lazily so tests run without it

    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


class Archive:
    """Cached, read-only view of `intraday/` in the collector bucket."""

    def __init__(self, cache_dir: Path | str = DEFAULT_CACHE, s3: Any = None,
                 bucket: str | None = None):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.bucket = bucket or os.environ.get("R2_BUCKET_NAME", "qqq-options-chain-data")
        self._s3 = s3

    @property
    def s3(self):
        if self._s3 is None:
            self._s3 = make_s3()
        return self._s3

    def list_keys(self, prefix: str, refresh: bool = False) -> list[str]:
        marker = self.cache_dir / "listings" / (prefix.strip("/").replace("/", "__") + ".json")
        if marker.exists() and not refresh:
            return json.loads(marker.read_text())
        keys: list[str] = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(o["Key"] for o in page.get("Contents", []))
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(keys))
        return keys

    def get(self, key: str) -> bytes:
        path = self.cache_dir / "objects" / key
        if path.exists():
            return path.read_bytes()
        body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return body

    def get_many(self, keys: list[str], workers: int = 16) -> list[bytes]:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(self.get, keys))

    # -- intraday listing -------------------------------------------------

    def snapshot_index(self, refresh: bool = False) -> dict[str, list[str]]:
        """{YYYYMMDD: [snapshot csv keys, chronological]} for QQQ days."""
        out: dict[str, list[str]] = {}
        for key in self.list_keys(PREFIX, refresh=refresh):
            parsed = parse_snapshot_key(key)
            if parsed:
                out.setdefault(parsed[0], []).append(key)
        for keys in out.values():
            keys.sort()
        return dict(sorted(out.items()))


# ---------------------------------------------------------------------------
# Parsing and per-snapshot checks (pure)
# ---------------------------------------------------------------------------

def parse_snapshot_key(key: str) -> tuple[str, int] | None:
    """(YYYYMMDD, seconds since ET midnight) for a QQQ snapshot csv key."""
    if SYMBOL == "SPY":
        if not key.startswith(PREFIX):
            return None
        key = "intraday/" + key[len(PREFIX):]
    parts = key.split("/")
    if len(parts) != 3 or parts[0] != "intraday" or not (parts[1].isdigit() and len(parts[1]) == 8):
        return None
    name = parts[2]
    if not (name.startswith("snapshot_") and name.endswith(".csv")):
        return None
    stamp = name[len("snapshot_"):-len(".csv")]
    if len(stamp) < 6 or not stamp[:6].isdigit():
        return None
    hh, mm, ss = int(stamp[:2]), int(stamp[2:4]), int(stamp[4:6])
    if hh > 23 or mm > 59 or ss > 59:
        return None
    return parts[1], hh * 3600 + mm * 60 + ss


def _num(value: str | None) -> float | None:
    try:
        x = float(value) if value not in (None, "") else None
    except ValueError:
        return None
    return x if x is not None and math.isfinite(x) else None


def parse_snapshot(body: bytes, trade_day: str) -> dict:
    """Board of 0DTE quotes + recorded underlying for one snapshot csv.

    Returns {"spot": float|None, "q": {"C709": [bid, ask], ...}} keeping only
    rows whose TradeDate and Expiration are the snapshot's own session (0DTE).
    Bid/Ask are kept as recorded (None when blank); usability is decided later.
    """
    text = body.decode("utf-8", errors="replace")
    spots: list[float] = []
    board: dict[str, list] = {}
    volume = []
    for row in csv.DictReader(io.StringIO(text)):
        if row.get("TradeDate") != trade_day or row.get("Expiration") != trade_day:
            continue
        side = (row.get("Type") or "").lower()
        strike = _num(row.get("Strike"))
        if side not in ("call", "put") or strike is None:
            continue
        key = ("C" if side == "call" else "P") + _fmt_strike(strike)
        delta = _num(row.get("VolDelta"))
        if delta is not None and delta >= 0:
            volume.append(delta)
        board[key] = [_num(row.get("Bid")), _num(row.get("Ask"))]
        spot = _num(row.get("UnderlyingPrice"))
        if spot is not None and spot > 0:
            spots.append(spot)
    spot = median(spots) if spots else None
    return {"spot": spot, "q": board, "volume": sum(volume) if volume else None}


def _fmt_strike(strike: float) -> str:
    return str(int(strike)) if float(strike).is_integer() else f"{strike:g}"


def contract_strike(contract: str) -> float:
    return float(contract[1:])


def quote_usable(bid: float | None, ask: float | None) -> tuple[bool, bool, str | None]:
    """(can_buy_at_ask, can_sell_at_bid, reason_if_neither) for one recorded quote."""
    if bid is None and ask is None:
        return False, False, "no quote"
    if ask is not None and bid is not None and ask < bid:
        return False, False, "crossed quote"
    can_buy = ask is not None and ask > 0 and bid is not None and bid >= 0
    can_sell = bid is not None and bid > 0
    reason = None if (can_buy or can_sell) else "no usable bid/ask"
    return can_buy, can_sell, reason


def parity_spot(board: dict[str, list], spot: float | None) -> tuple[float | None, float | None]:
    """(implied underlying, tolerance) from the call/put pair nearest `spot`.

    S_implied = K + mid_call - mid_put (0DTE, zero rate/time; the convention
    research/historical_archive_parity.py already states). Tolerance is
    PARITY_TOL_USD plus half the two legs' combined spread.
    """
    if spot is None:
        return None, None
    strikes = sorted({contract_strike(k) for k in board}, key=lambda k: abs(k - spot))
    for k in strikes[:6]:
        c = board.get("C" + _fmt_strike(k))
        p = board.get("P" + _fmt_strike(k))
        if not c or not p or None in c or None in p:
            continue
        if min(c[0], p[0]) <= 0 or c[1] < c[0] or p[1] < p[0]:
            continue
        implied = k + (c[0] + c[1]) / 2 - (p[0] + p[1]) / 2
        tol = PARITY_TOL_USD + ((c[1] - c[0]) + (p[1] - p[0])) / 2
        return implied, tol
    return None, None


def assess(snapshots: list[dict]) -> list[dict]:
    """Annotate chronological snapshots with `ok`/`reason`/`content_t`.

    - no recorded underlying -> unusable (reason "no_underlying")
    - no two-sided call/put pair near the money -> unusable ("no_pair")
    - recorded underlying inconsistent with the parity-implied price of the
      snapshot's own option mids -> unusable ("parity"). This is a
      consistency check on the recorded values; the archive has no per-quote
      timestamps, so it is not proof that spot and quotes were observed at
      the same instant.
    - board + underlying byte-identical to the previous snapshot -> its
      content is as old as the first snapshot of that identical run
      (`content_t`); the engine treats it as stale once that age passes
      MAX_QUOTE_AGE_S, so a frozen feed cannot keep quoting.
    """
    prev_sig = None
    content_t = None
    for snap in snapshots:
        sig = (snap["spot"], json.dumps(snap["q"], sort_keys=True))
        if sig != prev_sig:
            content_t = snap["t"]
        prev_sig = sig
        snap["content_t"] = content_t
        implied, tol = parity_spot(snap["q"], snap["spot"])
        snap["parity_spot"] = None if implied is None else round(implied, 4)
        snap["parity_tol"] = None if tol is None else round(tol, 4)
        if snap["spot"] is None:
            snap["ok"], snap["reason"] = False, "no_underlying"
        elif implied is None:
            snap["ok"], snap["reason"] = False, "no_pair"
        elif abs(implied - snap["spot"]) > tol:
            snap["ok"], snap["reason"] = False, "parity"
        else:
            snap["ok"], snap["reason"] = True, None
    return snapshots


def session_bounds(day: date) -> tuple[int, int]:
    close = session_close(day)
    return SESSION_OPEN_S, close.hour * 3600 + close.minute * 60


def coverage(times: Iterable[int], day: date, max_age_s: int = MAX_QUOTE_AGE_S) -> dict:
    """How much of the regular session has a snapshot no older than `max_age_s`.

    `ratio` is the quoted fraction of session seconds under exactly the rule
    the engine applies at play time, so cadence (60 s vs ~70 s) does not
    matter, only real holes do. A snapshot taken up to 5 minutes before the
    open still quotes the first seconds of the session.
    """
    open_s, close_s = session_bounds(day)
    ts = sorted(t for t in times if open_s - 300 <= t < close_s)
    quoted = 0
    for i, t in enumerate(ts):
        end = min(t + max_age_s, ts[i + 1] if i + 1 < len(ts) else close_s, close_s)
        start = max(t, open_s)
        quoted += max(0, end - start)
    inside = [t for t in ts if t >= open_s]
    gaps = [b - a for a, b in zip([open_s] + inside, inside + [close_s])]
    return {"session_s": close_s - open_s, "quoted_s": quoted,
            "ratio": round(quoted / (close_s - open_s), 4),
            "max_gap_s": max(gaps) if gaps else close_s - open_s, "snapshots": len(inside)}


# ---------------------------------------------------------------------------
# Day packs
# ---------------------------------------------------------------------------

def build_pack(archive: Archive, yyyymmdd: str, keys: list[str]) -> dict:
    day = datetime.strptime(yyyymmdd, "%Y%m%d").date()
    open_s, close_s = session_bounds(day)
    chosen = []
    for key in keys:
        parsed = parse_snapshot_key(key)
        # keep a few minutes before the open so the opening quote is covered
        if parsed and open_s - 300 <= parsed[1] <= close_s:
            chosen.append((parsed[1], key))
    chosen.sort()
    bodies = archive.get_many([k for _, k in chosen])
    trade_day = day.isoformat()
    snaps = []
    for (t, key), body in zip(chosen, bodies):
        snap = parse_snapshot(body, trade_day)
        snaps.append({"t": t, "key": key, **snap})
    assess(snaps)
    return {
        "version": PACK_VERSION,
        "day": trade_day,
        "open_s": open_s,
        "close_s": close_s,
        "coverage_all": coverage([s["t"] for s in snaps], day),
        "coverage_usable": playable_coverage(snaps, open_s, close_s),
        "snapshots": snaps,
    }


def load_or_build_pack(archive: Archive, yyyymmdd: str, keys: list[str]) -> dict:
    path = archive.cache_dir / "packs" / f"{yyyymmdd}.v{PACK_VERSION}.json.gz"
    if path.exists():
        return json.loads(gzip.decompress(path.read_bytes()))
    pack = build_pack(archive, yyyymmdd, keys)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps(pack, separators=(",", ":")).encode()))
    return pack


def playable_coverage(snaps: list[dict], open_s: int, close_s: int,
                      max_age_s: int = MAX_QUOTE_AGE_S) -> dict:
    """Session seconds at which the engine would quote (same rule as
    ReplaySession.board_state): the latest snapshot at or before t is `ok`, at
    most `max_age_s` old, and its content changed within `max_age_s`."""
    ts = [s["t"] for s in snaps]
    live = 0
    max_dark = 0
    # session seconds before the first snapshot are dark
    cur_dark = max(0, min(ts[0], close_s) - open_s) if ts else close_s - open_s
    for i, s in enumerate(snaps):
        seg_start = max(s["t"], open_s)
        seg_end = min(ts[i + 1] if i + 1 < len(ts) else close_s, close_s)
        if seg_end <= seg_start:
            continue
        good_end = seg_start
        if s["ok"]:
            good_end = max(seg_start, min(seg_end, s["t"] + max_age_s,
                                          s.get("content_t", s["t"]) + max_age_s))
        if good_end > seg_start:
            live += good_end - seg_start
            max_dark = max(max_dark, cur_dark)
            cur_dark = 0
        cur_dark += seg_end - good_end
    max_dark = max(max_dark, cur_dark)
    return {"session_s": close_s - open_s, "live_s": live,
            "ratio": round(live / (close_s - open_s), 4), "longest_dark_s": max_dark}


def pack_qualifies(pack: dict, min_ratio: float = 0.85, max_dark_s: int = 900) -> tuple[bool, str | None]:
    """Refuse a day at load time when too little of it is actually tradable."""
    cov = pack["coverage_usable"]
    if cov["ratio"] < min_ratio:
        return False, f"quotes live for {cov['ratio']:.0%} of the session < {min_ratio:.0%}"
    if cov["longest_dark_s"] > max_dark_s:
        return False, f"longest disabled stretch {cov['longest_dark_s']}s > {max_dark_s}s"
    return True, None


def raw_quote(archive: Archive, key: str, contract: str) -> dict | None:
    """Re-read one contract's row straight from the archived csv (for reconciliation)."""
    parsed = parse_snapshot_key(key)
    if not parsed:
        return None
    day = datetime.strptime(parsed[0], "%Y%m%d").date().isoformat()
    side = "call" if contract[0] == "C" else "put"
    strike = float(contract[1:])
    for row in csv.DictReader(io.StringIO(archive.get(key).decode("utf-8", errors="replace"))):
        if (row.get("TradeDate") == day and row.get("Expiration") == day
                and (row.get("Type") or "").lower() == side and _num(row.get("Strike")) == strike):
            return {"key": key, "OptionSymbol": row.get("OptionSymbol"), "Bid": _num(row.get("Bid")),
                    "Ask": _num(row.get("Ask")), "UnderlyingPrice": _num(row.get("UnderlyingPrice")),
                    **{k: _num(row.get(k)) for k in ("Delta", "Theta", "Vega", "IV")}}
    return None
