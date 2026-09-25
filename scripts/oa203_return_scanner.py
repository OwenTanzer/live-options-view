#!/usr/bin/env python3
"""OA-203 daily top-500 liquid options-chain intraday-return scanner.

One process per NYSE session (Railway cron, before the open):

1. **Universe.** Rank underlyings by the previous NYSE session's OCC cleared
   option volume (https://marketdata.theocc.com/volume-query, public, no
   key). OCC reports each underlying by account type (customer, firm,
   market maker); every cleared contract appears once per side, so chain
   volume is the sum over all rows divided by two. Walk the ranking, look up
   each underlying's nearest unexpired expiry on Tradier (same-day included)
   and keep the first ``universe_size`` that have one. Skipped names and
   the reason are kept. The raw OCC file, its hash and the ranking rule are
   archived with the universe, so selection only uses information available
   before the open and can be reproduced.
2. **Sampling.** Tradier allows one market-data stream per account, and the
   MOO-169 QQQ/IBIT collector owns it, so this scanner polls REST instead.
   Each sweep walks the universe in blocks: one batched underlying quote
   request per block, then the block's nearest-expiry chains. Requests are
   paced against a local cap *and* Tradier's token-wide ``X-Ratelimit-*``
   headers, so other services sharing the token keep headroom. With the
   120/minute market-data limit a 500-chain sweep takes about five minutes;
   moves shorter than that are only seen by the after-close backfill.
   Every sampled contract row is archived (gzip JSONL per sweep), with a
   per-chain status for every sweep, so failures are explicit.
3. **After close.** Build the strike-level leaderboard from the archive
   (``oa203_returns``), fetch Tradier 1-minute trade bars for the top
   contracts, rebuild with trade-basis returns merged in, and write a
   summary that labels the session ``complete`` or ``partial`` with reasons.

Everything lands in a local per-date spool and is uploaded to R2 under
``oa203/scanner/<date>/``. ``build``/``inspect``/``readout`` run the same
code offline against a downloaded day directory.

Usage::

    python scripts/oa203_return_scanner.py run            # daily cron
    python scripts/oa203_return_scanner.py universe       # selection dry run
    python scripts/oa203_return_scanner.py smoke --symbols SPY,QQQ --sweeps 2 --dir /tmp/oa203
    python scripts/oa203_return_scanner.py build --dir <day dir>
    python scripts/oa203_return_scanner.py inspect --dir <day dir> --symbol SPY260928C00660000
    python scripts/oa203_return_scanner.py readout --dir <day dir> [--dir ...]
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import os
import signal
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from zoneinfo import ZoneInfo

import requests

from oa203_returns import (
    ContractInfo,
    Observation,
    ReturnPolicy,
    contract_metrics,
    rank,
    trade_bar_metrics,
)

API = "https://api.tradier.com/v1"
OCC_VOLUME_URL = "https://marketdata.theocc.com/volume-query"
ET = ZoneInfo("America/New_York")
SELECTION_VERSION = "oa203-universe-v1"
STOP = False


def log(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, "at": datetime.now(timezone.utc).isoformat(), **fields},
                     sort_keys=True, default=str), flush=True)


def on_stop(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def now_ms() -> int:
    return int(time.time() * 1000)


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass
class Config:
    universe_size: int = 500
    max_rpm: int = 100          # own cap, below Tradier's 120/min market-data limit
    reserve: int = 10           # pause when the token-wide remaining budget drops this low
    block_size: int = 50
    workers: int = 3
    backfill_top: int = 200
    universe_lead_min: int = 30  # start selection this long before the open
    spool_dir: Path = Path("/data/oa203")
    r2_prefix: str = "oa203/scanner"
    upload: bool = True
    policy: ReturnPolicy = field(default_factory=ReturnPolicy)

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            universe_size=_env_int("OA203_UNIVERSE_SIZE", 500),
            max_rpm=_env_int("OA203_MAX_RPM", 100),
            reserve=_env_int("OA203_RATE_RESERVE", 10),
            block_size=_env_int("OA203_BLOCK_SIZE", 50),
            workers=_env_int("OA203_WORKERS", 3),
            backfill_top=_env_int("OA203_BACKFILL_TOP", 200),
            universe_lead_min=_env_int("OA203_UNIVERSE_LEAD_MIN", 30),
            spool_dir=Path(os.getenv("OA203_SPOOL_DIR", "/data/oa203")),
            r2_prefix=os.getenv("OA203_R2_PREFIX", "oa203/scanner"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "universe_size": self.universe_size, "max_rpm": self.max_rpm,
            "reserve": self.reserve, "block_size": self.block_size, "workers": self.workers,
            "backfill_top": self.backfill_top, "universe_lead_min": self.universe_lead_min,
            "r2_prefix": self.r2_prefix, "return_policy": self.policy.as_dict(),
        }


# --------------------------------------------------------------------------
# Calendar
# --------------------------------------------------------------------------

def nyse_session_bounds(day: date) -> tuple[datetime, datetime] | None:
    """Exchange hours for ``day`` including early closes; None if closed."""
    import pandas_market_calendars as mcal
    schedule = mcal.get_calendar("NYSE").schedule(start_date=day, end_date=day)
    if schedule.empty:
        return None
    row = schedule.iloc[0]
    return (row["market_open"].to_pydatetime().astimezone(ET),
            row["market_close"].to_pydatetime().astimezone(ET))


def previous_session(day: date) -> date:
    import pandas_market_calendars as mcal
    schedule = mcal.get_calendar("NYSE").schedule(start_date=day - timedelta(days=14),
                                                  end_date=day - timedelta(days=1))
    if schedule.empty:
        raise RuntimeError(f"no NYSE session in the 14 days before {day}")
    return schedule.index[-1].date()


# --------------------------------------------------------------------------
# OCC volume ranking
# --------------------------------------------------------------------------

def fetch_occ_volume(report_date: date, session: requests.Session | None = None) -> bytes:
    params = {
        "reportDate": report_date.strftime("%Y%m%d"), "format": "csv",
        "volumeQueryType": "O", "symbolType": "ALL", "reportType": "D",
        "accountType": "ALL", "productKind": "ALL", "porc": "BOTH",
    }
    http = session or requests.Session()
    response = http.get(OCC_VOLUME_URL, params=params, timeout=120,
                        headers={"User-Agent": "Mozilla/5.0 (OA-203 research scanner)"})
    response.raise_for_status()
    return response.content


def parse_occ_volume(body: bytes, report_date: date) -> dict[str, dict[str, int]]:
    """Per-underlying contracts from OCC's volume-by-account-type CSV.

    Raises if the file has no rows for ``report_date`` (not yet published,
    or an error page), rather than ranking on stale or empty data.
    """
    text = body.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    expected = {"quantity", "underlying", "actype", "porc", "actdate"}
    if not reader.fieldnames or not expected <= {f.strip() for f in reader.fieldnames}:
        raise RuntimeError(f"OCC volume file has unexpected header: {text[:200]!r}")
    want = report_date.strftime("%m/%d/%Y")
    sides: dict[str, Counter] = defaultdict(Counter)
    seen = 0
    for row in reader:
        row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
        if row["actdate"] != want or not row["underlying"]:
            continue
        try:
            quantity = int(row["quantity"])
        except ValueError:
            continue
        seen += 1
        key = "calls" if row["porc"] == "C" else "puts" if row["porc"] == "P" else "other"
        sides[row["underlying"]][key] += quantity
    if not seen:
        raise RuntimeError(f"OCC volume file has no rows for {want}")
    # Each cleared contract is reported once per side (buyer and seller).
    return {u: {"contracts": sum(c.values()) // 2, "calls": c["calls"] // 2, "puts": c["puts"] // 2}
            for u, c in sides.items()}


def rank_underlyings(volume: dict[str, dict[str, int]]) -> list[tuple[str, dict[str, int]]]:
    return sorted(volume.items(), key=lambda kv: (-kv[1]["contracts"], kv[0]))


# --------------------------------------------------------------------------
# Rate-limited Tradier client
# --------------------------------------------------------------------------

class RateLimited(RuntimeError):
    pass


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


class Tradier:
    """Market-data client paced against a local cap and Tradier's headers."""

    def __init__(self, token: str, max_rpm: int = 100, reserve: int = 10,
                 session: Any = None, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
        self.max_rpm, self.reserve = max_rpm, reserve
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.sent: deque[float] = deque()
        self.blocked_until = 0.0
        self.stats: Counter[str] = Counter()

    def _acquire(self) -> None:
        with self.lock:
            while True:
                now = self.clock()
                while self.sent and now - self.sent[0] >= 60:
                    self.sent.popleft()
                wait = max(self.blocked_until - now,
                           (self.sent[0] + 60 - now) if len(self.sent) >= self.max_rpm else 0)
                if wait <= 0:
                    self.sent.append(now)
                    return
                self.stats["rate_waits"] += 1
                self.sleep(min(wait, 61))

    def _observe(self, headers: Any) -> None:
        try:
            available = int(headers.get("X-Ratelimit-Available"))
            expiry = int(headers.get("X-Ratelimit-Expiry")) / 1000.0
        except (TypeError, ValueError):
            return
        if available <= self.reserve:
            with self.lock:
                self.blocked_until = max(self.blocked_until, expiry + 0.25)

    def request(self, method: str, path: str, *, params: dict | None = None,
                data: dict | None = None, attempts: int = 3) -> Any:
        last_exc: Exception | None = None
        for attempt in range(attempts):
            self._acquire()
            self.stats["requests"] += 1
            try:
                response = self.session.request(method, f"{API}{path}", params=params,
                                                data=data, timeout=60)
            except requests.RequestException as exc:
                self.stats["transport_errors"] += 1
                last_exc = exc
                self.sleep(2 ** attempt)
                continue
            self._observe(response.headers)
            if response.status_code == 429:
                self.stats["rate_limited"] += 1
                with self.lock:
                    self.blocked_until = max(self.blocked_until, self.clock() + 60)
                last_exc = RateLimited(f"429 on {path}")
                continue
            if response.status_code >= 500:
                self.stats["server_errors"] += 1
                last_exc = RuntimeError(f"HTTP {response.status_code} on {path}")
                self.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            return response.json()
        raise last_exc or RuntimeError(f"request failed: {path}")

    def expirations(self, symbol: str) -> list[str]:
        data = self.request("GET", "/markets/options/expirations",
                            params={"symbol": symbol, "includeAllRoots": "true"})
        return [str(d) for d in _as_list((data.get("expirations") or {}).get("date"))]

    def chain(self, symbol: str, expiration: str) -> list[dict[str, Any]]:
        data = self.request("GET", "/markets/options/chains",
                            params={"symbol": symbol, "expiration": expiration, "greeks": "false"})
        return [c for c in _as_list((data.get("options") or {}).get("option")) if isinstance(c, dict)]

    def quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        data = self.request("POST", "/markets/quotes",
                            data={"symbols": ",".join(symbols), "greeks": "false"})
        quotes = _as_list((data.get("quotes") or {}).get("quote"))
        return {q["symbol"]: q for q in quotes if isinstance(q, dict) and q.get("symbol")}

    def timesales(self, symbol: str, day: date) -> list[dict[str, Any]]:
        data = self.request("GET", "/markets/timesales", params={
            "symbol": symbol, "interval": "1min", "session_filter": "open",
            "start": f"{day.isoformat()} 09:30", "end": f"{day.isoformat()} 16:00"})
        return [b for b in _as_list((data.get("series") or {}).get("data")) if isinstance(b, dict)]


def load_token() -> str:
    token = os.getenv("TRADIER_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TRADIER_TOKEN is not set")
    return token


# --------------------------------------------------------------------------
# Universe selection
# --------------------------------------------------------------------------

def nearest_expiration(dates: Iterable[str], today: date) -> str | None:
    upcoming = sorted(d for d in dates if d >= today.isoformat())
    return upcoming[0] if upcoming else None


def select_universe(client: Tradier, ranked: list[tuple[str, dict[str, int]]], today: date,
                    target: int, workers: int = 3) -> dict[str, Any]:
    """First ``target`` ranked underlyings with an unexpired expiry on Tradier."""
    selected: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    position = 0

    def lookup(item: tuple[int, str, dict[str, int]]) -> dict[str, Any]:
        rank_no, symbol, vol = item
        base = {"occ_rank": rank_no, "underlying": symbol, **{f"occ_{k}": v for k, v in vol.items()}}
        try:
            dates = client.expirations(symbol)
        except Exception as exc:  # recorded per name, never a silent pass
            return {**base, "skip_reason": "provider_error", "detail": str(exc)[:300]}
        if not dates:
            return {**base, "skip_reason": "no_listed_expirations"}
        expiry = nearest_expiration(dates, today)
        if expiry is None:
            return {**base, "skip_reason": "no_unexpired_expiration"}
        return {**base, "expiration": expiry,
                "days_to_expiration": (date.fromisoformat(expiry) - today).days}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        while len(selected) < target and position < len(ranked):
            need = target - len(selected)
            batch = [(position + i + 1, *ranked[position + i])
                     for i in range(min(need, len(ranked) - position))]
            position += len(batch)
            for result in pool.map(lookup, batch):
                (skipped if "skip_reason" in result else selected).append(result)
    selected = sorted(selected, key=lambda r: r["occ_rank"])[:target]
    return {
        "selection_version": SELECTION_VERSION,
        "rule": ("rank underlyings by previous-session OCC cleared contracts "
                 "(sum over account types / 2); keep the first N with an unexpired "
                 "Tradier expiration; chain = nearest expiration on/after the trade date"),
        "target": target,
        "selected_count": len(selected),
        "shortfall": max(0, target - len(selected)),
        "candidates_examined": position,
        "selected": selected,
        "skipped": skipped,
        "skip_counts": dict(Counter(s["skip_reason"] for s in skipped)),
    }


# --------------------------------------------------------------------------
# Sampling
# --------------------------------------------------------------------------

def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # drop NaN


def _spot(quote: dict[str, Any] | None) -> tuple[float | None, str | None, int | None]:
    if not quote:
        return None, None, None
    bid, ask = _num(quote.get("bid")), _num(quote.get("ask"))
    if bid and ask and bid > 0 and ask >= bid:
        stamps = [int(s) for s in (quote.get("bid_date"), quote.get("ask_date")) if s]
        return round((bid + ask) / 2, 6), "mid", (min(stamps) if stamps else None)
    last = _num(quote.get("last"))
    return (last, "last", int(quote["trade_date"]) if quote.get("trade_date") else None) if last else (None, None, None)


def chain_rows(sweep: int, fetched_ms: int, underlying: str, contracts: list[dict[str, Any]],
               quote: dict[str, Any] | None) -> list[dict[str, Any]]:
    spot, spot_source, spot_ms = _spot(quote)
    rows = []
    for c in contracts:
        rows.append({
            "sweep": sweep, "t": fetched_ms, "underlying": underlying,
            "spot": spot, "spot_source": spot_source, "spot_ms": spot_ms,
            "symbol": c.get("symbol"), "root": c.get("root_symbol"),
            "type": c.get("option_type"), "strike": _num(c.get("strike")),
            "expiration": c.get("expiration_date"),
            "bid": _num(c.get("bid")), "ask": _num(c.get("ask")),
            "bid_ms": c.get("bid_date"), "ask_ms": c.get("ask_date"),
            "bid_size": c.get("bidsize"), "ask_size": c.get("asksize"),
            "last": _num(c.get("last")), "trade_ms": c.get("trade_date"),
            "volume": c.get("volume"), "oi": c.get("open_interest"),
            "contract_size": c.get("contract_size") or 100,
        })
    return rows


def run_sweep(client: Tradier, universe: list[dict[str, Any]], sweep: int, out_path: Path,
              block_size: int, workers: int, deadline: datetime | None = None) -> dict[str, Any]:
    """Sample every chain once; write rows to ``out_path``; return the manifest entry."""
    started = now_ms()
    chains: dict[str, dict[str, Any]] = {}
    truncated = False
    with gzip.open(out_path, "wt", encoding="utf-8") as out, ThreadPoolExecutor(max_workers=workers) as pool:
        for start in range(0, len(universe), block_size):
            if STOP or (deadline and datetime.now(ET) >= deadline):
                truncated = True
                break
            block = universe[start:start + block_size]
            symbols = [u["underlying"] for u in block]
            try:
                quotes = client.quotes(symbols)
                quote_error = None
            except Exception as exc:
                quotes, quote_error = {}, str(exc)[:300]

            def fetch(u: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]] | None, int, str | None]:
                try:
                    contracts = client.chain(u["underlying"], u["expiration"])
                    return u, contracts, now_ms(), None
                except Exception as exc:
                    return u, None, now_ms(), str(exc)[:300]

            for u, contracts, fetched, error in pool.map(fetch, block):
                name = u["underlying"]
                status: dict[str, Any] = {"fetched_ms": fetched}
                if error is not None:
                    status.update(status="error", error=error)
                elif not contracts:
                    status.update(status="empty")
                else:
                    rows = chain_rows(sweep, fetched, name, contracts, quotes.get(name))
                    for row in rows:
                        out.write(json.dumps(row, separators=(",", ":")) + "\n")
                    status.update(status="ok", rows=len(rows))
                if name not in quotes:
                    status["spot_missing"] = quote_error or "not returned"
                chains[name] = status
    counts = Counter(s["status"] for s in chains.values())
    return {"sweep": sweep, "file": out_path.name, "started_ms": started, "ended_ms": now_ms(),
            "truncated": truncated, "status_counts": dict(counts),
            "rows": sum(s.get("rows", 0) for s in chains.values()), "chains": chains}


# --------------------------------------------------------------------------
# Archive layout and storage
# --------------------------------------------------------------------------

class DayArchive:
    """Local per-date directory, mirrored to R2 under ``<prefix>/<date>/``."""

    def __init__(self, root: Path, day: date, prefix: str, upload: bool) -> None:
        self.day = day
        self.dir = root / day.isoformat()
        (self.dir / "sweeps").mkdir(parents=True, exist_ok=True)
        self.prefix = f"{prefix}/{day.isoformat()}"
        self.upload = upload
        self._r2: tuple[Any, str] | None = None
        self.upload_failures: list[dict[str, str]] = []

    def path(self, name: str) -> Path:
        return self.dir / name

    def write_json(self, name: str, payload: Any) -> Path:
        path = self.path(name)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path

    def append_manifest(self, entry: dict[str, Any]) -> None:
        with self.path("sweeps/manifest.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps(entry, sort_keys=True) + "\n")

    def manifest(self) -> list[dict[str, Any]]:
        return _read_manifest(self.dir)

    def push(self, name: str) -> None:
        if not self.upload:
            return
        try:
            if self._r2 is None:
                from moo144_tradier_probe import r2_client
                self._r2 = r2_client()
            from moo144_tradier_probe import upload_file_verified
            client, bucket = self._r2
            gz = name.endswith(".gz")
            upload_file_verified(client, bucket, self.path(name), f"{self.prefix}/{name}",
                                 "application/gzip" if gz else
                                 "text/csv" if name.endswith(".csv") else "application/json")
            self.upload_failures = [f for f in self.upload_failures if f["name"] != name]
        except Exception as exc:
            log("oa203_upload_failed", name=name, error=str(exc)[:300])
            self.upload_failures = [f for f in self.upload_failures if f["name"] != name]
            self.upload_failures.append({"name": name, "error": str(exc)[:300]})

    def retry_uploads(self) -> None:
        for failure in list(self.upload_failures):
            self.push(failure["name"])


def iter_rows(day_dir: Path) -> Iterator[dict[str, Any]]:
    for path in sorted((day_dir / "sweeps").glob("sweep_*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as src:
            for line in src:
                if line.strip():
                    yield json.loads(line)


# --------------------------------------------------------------------------
# Leaderboard build (live and offline share this path)
# --------------------------------------------------------------------------

def _contract_paths(day_dir: Path, start_ms: int | None, end_ms: int | None,
                    buckets: int = 64) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    """Group archived rows by contract with bounded memory (hash-partitioned temp files)."""
    with tempfile.TemporaryDirectory(prefix="oa203-build-") as tmp:
        handles = [open(Path(tmp) / f"b{i:02d}.jsonl", "w", encoding="utf-8") for i in range(buckets)]
        try:
            for row in iter_rows(day_dir):
                if not row.get("symbol"):
                    continue
                if (start_ms and row["t"] < start_ms) or (end_ms and row["t"] >= end_ms):
                    continue
                bucket = int(hashlib.md5(row["symbol"].encode()).hexdigest(), 16) % buckets
                handles[bucket].write(json.dumps(row, separators=(",", ":")) + "\n")
        finally:
            for h in handles:
                h.close()
        for i in range(buckets):
            grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
            with open(Path(tmp) / f"b{i:02d}.jsonl", encoding="utf-8") as src:
                for line in src:
                    row = json.loads(line)
                    grouped[row["symbol"]].append(row)
            for symbol in sorted(grouped):
                yield symbol, sorted(grouped[symbol], key=lambda r: (r["t"], r["sweep"]))


def chain_ok_sweeps(manifest: list[dict[str, Any]]) -> Counter:
    ok: Counter = Counter()
    for entry in manifest:
        for underlying, status in entry.get("chains", {}).items():
            if status.get("status") == "ok":
                ok[underlying] += 1
    return ok


def load_backfill(day_dir: Path) -> dict[str, dict[str, Any]]:
    path = day_dir / "backfill_timesales.jsonl.gz"
    if not path.exists():
        return {}
    out = {}
    with gzip.open(path, "rt", encoding="utf-8") as src:
        for line in src:
            if line.strip():
                record = json.loads(line)
                out[record["symbol"]] = record
    return out


def build_contract_rows(day_dir: Path, policy: ReturnPolicy, start_ms: int | None = None,
                        end_ms: int | None = None) -> list[dict[str, Any]]:
    ok = chain_ok_sweeps(_read_manifest(day_dir))
    backfill = load_backfill(day_dir)
    rows = []
    for symbol, path_rows in _contract_paths(day_dir, start_ms, end_ms):
        last = path_rows[-1]
        info = ContractInfo(
            symbol=symbol, underlying=last["underlying"], option_type=last["type"],
            strike=last["strike"], expiration=last["expiration"], root=last.get("root"),
            contract_size=int(last.get("contract_size") or 100),
            volume=last.get("volume"), open_interest=path_rows[0].get("oi"),
            chain_ok_sweeps=ok.get(last["underlying"], 0),
        )
        observations = [Observation(t=r["t"], bid=r["bid"], ask=r["ask"], bid_ms=r.get("bid_ms"),
                                    ask_ms=r.get("ask_ms"), underlying_price=r.get("spot"))
                        for r in path_rows]
        row = contract_metrics(info, observations, policy)
        record = backfill.get(symbol)
        if record is not None:
            row["backfill_status"] = "error" if record.get("error") else "ok"
            if not record.get("error"):
                row.update(trade_bar_metrics(record.get("bars") or [], info.contract_size, policy))
        rows.append(row)
    return rank(rows)


def _read_manifest(day_dir: Path) -> list[dict[str, Any]]:
    path = day_dir / "sweeps" / "manifest.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _cell(value: Any) -> Any:
    if isinstance(value, list):
        return "|".join(str(v) for v in value)
    if isinstance(value, float):
        return repr(round(value, 8))
    return value


def write_outputs(day_dir: Path, rows: list[dict[str, Any]], top: int = 200) -> dict[str, str]:
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    with gzip.open(day_dir / "contracts.csv.gz", "wt", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _cell(row.get(k)) for k in columns})
    board = [r for r in rows if (r.get("rank") or 10**9) <= top or (r.get("clean_rank") or 10**9) <= top]
    with open(day_dir / "leaderboard.csv", "w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in board:
            writer.writerow({k: _cell(row.get(k)) for k in columns})
    return {"contracts": "contracts.csv.gz", "leaderboard": "leaderboard.csv"}


# --------------------------------------------------------------------------
# After-close backfill
# --------------------------------------------------------------------------

def backfill_symbols(rows: list[dict[str, Any]], top: int) -> list[dict[str, Any]]:
    chosen = [r for r in rows if (r.get("rank") or 10**9) <= top
              or (r.get("clean_rank") or 10**9) <= top]
    return sorted(chosen, key=lambda r: (r.get("rank") or 10**9))


def run_backfill(client: Tradier, day: date, day_dir: Path, rows: list[dict[str, Any]],
                 top: int, workers: int) -> dict[str, Any]:
    targets = backfill_symbols(rows, top)

    def fetch(row: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"symbol": row["symbol"], "fetched_ms": now_ms(),
                    "bars": client.timesales(row["symbol"], day)}
        except Exception as exc:
            return {"symbol": row["symbol"], "fetched_ms": now_ms(), "error": str(exc)[:300]}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        records = list(pool.map(fetch, targets))
    with gzip.open(day_dir / "backfill_timesales.jsonl.gz", "wt", encoding="utf-8") as out:
        for record in records:
            out.write(json.dumps(record, separators=(",", ":")) + "\n")
    return {"requested": len(targets), "errors": sum(1 for r in records if r.get("error"))}


# --------------------------------------------------------------------------
# Completeness
# --------------------------------------------------------------------------

def assess_session(universe: dict[str, Any], manifest: list[dict[str, Any]], open_ms: int,
                   close_ms: int, upload_failures: list[dict[str, str]],
                   backfill: dict[str, Any] | None, rate_stats: dict[str, int]) -> dict[str, Any]:
    reasons: list[str] = []
    if universe.get("shortfall"):
        reasons.append(f"universe_shortfall:{universe['shortfall']}")
    in_session = [m for m in manifest if m["ended_ms"] > open_ms and m["started_ms"] < close_ms]
    if not in_session:
        reasons.append("no_sweeps_in_session")
    chain_total = sum(len(m["chains"]) for m in in_session)
    chain_ok = sum(m["status_counts"].get("ok", 0) for m in in_session)
    ok_rate = chain_ok / chain_total if chain_total else 0.0
    if chain_total and ok_rate < 0.98:
        reasons.append(f"chain_success_rate:{ok_rate:.4f}")
    durations = [m["ended_ms"] - m["started_ms"] for m in in_session if not m.get("truncated")]
    typical = statistics.median(durations) if durations else None
    gaps = []
    if in_session and typical:
        starts = sorted(m["started_ms"] for m in in_session)
        edges = [open_ms] + starts
        gaps = [(b - a) for a, b in zip(edges, edges[1:]) if (b - a) > 2 * typical + 60_000]
        if gaps:
            reasons.append(f"sweep_gaps:{len(gaps)}")
        last_end = max(m["ended_ms"] for m in in_session)
        if close_ms - last_end > 2 * typical + 60_000:
            reasons.append("stopped_before_close")
    if upload_failures:
        reasons.append(f"upload_failures:{len(upload_failures)}")
    if backfill is None:
        reasons.append("backfill_not_run")
    elif backfill.get("errors"):
        reasons.append(f"backfill_errors:{backfill['errors']}")
    if rate_stats.get("rate_limited"):
        reasons.append(f"rate_limited_responses:{rate_stats['rate_limited']}")
    return {
        "status": "partial" if reasons else "complete",
        "reasons": reasons,
        "sweeps_in_session": len(in_session),
        "chain_fetches": chain_total,
        "chain_success_rate": round(ok_rate, 6),
        "typical_sweep_s": round(typical / 1000, 1) if typical else None,
        "gaps_ms": gaps,
    }


# --------------------------------------------------------------------------
# Daily run
# --------------------------------------------------------------------------

def _sleep_until(when: datetime) -> None:
    while not STOP:
        remaining = (when - datetime.now(ET)).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 30))


def build_universe_for(day: date, client: Tradier, cfg: Config, archive: DayArchive) -> dict[str, Any]:
    report_date = previous_session(day)
    body = fetch_occ_volume(report_date)
    (archive.path(f"occ_volume_{report_date.isoformat()}.csv.gz")).write_bytes(gzip.compress(body))
    ranked = rank_underlyings(parse_occ_volume(body, report_date))
    universe = select_universe(client, ranked, day, cfg.universe_size, cfg.workers)
    universe.update({
        "trade_date": day.isoformat(),
        "occ_report_date": report_date.isoformat(),
        "occ_sha256": hashlib.sha256(body).hexdigest(),
        "occ_underlyings_ranked": len(ranked),
        "selected_at": datetime.now(timezone.utc).isoformat(),
        "config": cfg.as_dict(),
    })
    return universe


def finalize(archive: DayArchive, client: Tradier, cfg: Config, universe: dict[str, Any],
             open_ms: int | None, close_ms: int | None, backfill_enabled: bool = True) -> dict[str, Any]:
    rows = build_contract_rows(archive.dir, cfg.policy, open_ms, close_ms)
    backfill = None
    if backfill_enabled and rows:
        backfill = run_backfill(client, archive.day, archive.dir, rows, cfg.backfill_top, cfg.workers)
        rows = build_contract_rows(archive.dir, cfg.policy, open_ms, close_ms)
    outputs = write_outputs(archive.dir, rows)
    for name in ("backfill_timesales.jsonl.gz", "contracts.csv.gz", "leaderboard.csv",
                 "sweeps/manifest.jsonl"):
        if archive.path(name).exists():
            archive.push(name)
    archive.retry_uploads()
    manifest = archive.manifest()
    bounds_open = open_ms or min((m["started_ms"] for m in manifest), default=0)
    bounds_close = close_ms or max((m["ended_ms"] for m in manifest), default=0)
    summary = {
        "trade_date": archive.day.isoformat(),
        "session_open_ms": open_ms, "session_close_ms": close_ms,
        "assessment": assess_session(universe, manifest, bounds_open, bounds_close,
                                     archive.upload_failures, backfill, dict(client.stats)),
        "universe": {k: universe.get(k) for k in ("target", "selected_count", "shortfall",
                                                  "skip_counts", "occ_report_date")},
        "contracts": len(rows),
        "ranked_contracts": sum(1 for r in rows if r.get("rank")),
        "clean_ranked_contracts": sum(1 for r in rows if r.get("clean_rank")),
        "backfill": backfill,
        "request_stats": dict(client.stats),
        "outputs": outputs,
        "return_policy": cfg.policy.as_dict(),
        "finalized_at": datetime.now(timezone.utc).isoformat(),
    }
    archive.write_json("summary.json", summary)
    archive.push("summary.json")
    log("oa203_session_finalized", **summary["assessment"], contracts=summary["contracts"])
    return summary


def run_day(cfg: Config) -> int:
    day = datetime.now(ET).date()
    bounds = nyse_session_bounds(day)
    if bounds is None:
        log("oa203_no_session", date=day.isoformat())
        return 0
    open_at, close_at = bounds
    if datetime.now(ET) >= close_at:
        log("oa203_session_already_closed", date=day.isoformat())
        return 0
    client = Tradier(load_token(), cfg.max_rpm, cfg.reserve)
    archive = DayArchive(cfg.spool_dir, day, cfg.r2_prefix, cfg.upload)

    universe_path = archive.path("universe.json")
    if universe_path.exists():  # same-day restart keeps the persisted selection
        universe = json.loads(universe_path.read_text(encoding="utf-8"))
        log("oa203_universe_reloaded", selected=universe["selected_count"])
    else:
        _sleep_until(open_at - timedelta(minutes=cfg.universe_lead_min))
        universe = build_universe_for(day, client, cfg, archive)
        archive.write_json("universe.json", universe)
        archive.push("universe.json")
        archive.push(f"occ_volume_{universe['occ_report_date']}.csv.gz")
        log("oa203_universe_selected", selected=universe["selected_count"],
            shortfall=universe["shortfall"], skips=universe["skip_counts"])

    _sleep_until(open_at)
    sweep = max((m["sweep"] for m in archive.manifest()), default=0)
    while not STOP and datetime.now(ET) < close_at:
        sweep += 1
        name = f"sweeps/sweep_{sweep:04d}.jsonl.gz"
        entry = run_sweep(client, universe["selected"], sweep, archive.path(name),
                          cfg.block_size, cfg.workers, deadline=close_at)
        archive.append_manifest(entry)
        archive.push(name)
        log("oa203_sweep", sweep=sweep, rows=entry["rows"], status=entry["status_counts"],
            seconds=round((entry["ended_ms"] - entry["started_ms"]) / 1000, 1),
            truncated=entry["truncated"], requests=client.stats["requests"])
    if STOP:
        log("oa203_stopped_before_close", sweep=sweep)
        archive.push("sweeps/manifest.jsonl")
        return 0
    finalize(archive, client, cfg, universe,
             int(open_at.timestamp() * 1000), int(close_at.timestamp() * 1000))
    return 0


# --------------------------------------------------------------------------
# Inspection and readout
# --------------------------------------------------------------------------

def inspect_contract(day_dir: Path, symbol: str, policy: ReturnPolicy) -> dict[str, Any]:
    path = [r for r in iter_rows(day_dir) if r.get("symbol") == symbol]
    path.sort(key=lambda r: r["t"])
    if not path:
        raise SystemExit(f"{symbol} not found in {day_dir}")
    summary = json.loads((day_dir / "summary.json").read_text()) if (day_dir / "summary.json").exists() else {}
    open_ms, close_ms = summary.get("session_open_ms"), summary.get("session_close_ms")
    rows = [r for r in path if (not open_ms or r["t"] >= open_ms) and (not close_ms or r["t"] < close_ms)]
    info = ContractInfo(symbol=symbol, underlying=rows[-1]["underlying"], option_type=rows[-1]["type"],
                        strike=rows[-1]["strike"], expiration=rows[-1]["expiration"],
                        contract_size=int(rows[-1].get("contract_size") or 100),
                        volume=rows[-1].get("volume"), open_interest=rows[0].get("oi"),
                        chain_ok_sweeps=chain_ok_sweeps(_read_manifest(day_dir)).get(rows[-1]["underlying"], 0))
    obs = [Observation(r["t"], r["bid"], r["ask"], r.get("bid_ms"), r.get("ask_ms"), r.get("spot")) for r in rows]
    metrics = contract_metrics(info, obs, policy)
    record = load_backfill(day_dir).get(symbol)
    if record and not record.get("error"):
        metrics.update(trade_bar_metrics(record.get("bars") or [], info.contract_size, policy))
    return {
        "metrics": metrics,
        "path": [{"time_et": datetime.fromtimestamp(r["t"] / 1000, ET).strftime("%H:%M:%S"),
                  "t": r["t"], "bid": r["bid"], "ask": r["ask"], "mid": o.mid,
                  "quote_age_s": o.quote_age_s, "spot": r.get("spot"), "volume": r.get("volume")}
                 for r, o in zip(rows, obs)],
        "trade_bars": (record or {}).get("bars"),
    }


def _hour_bucket(ms: Any) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, ET).strftime("%H:00")


def _moneyness_bucket(row: dict[str, Any]) -> str | None:
    pct = row.get("strike_vs_spot_pct")
    if pct is None:
        return None
    # Signed so positive means out of the money for both calls and puts.
    otm = pct if row["option_type"] == "call" else -pct
    for edge, label in ((-0.05, "ITM >5%"), (-0.01, "ITM 1-5%"), (0.01, "ATM ±1%"),
                        (0.05, "OTM 1-5%"), (0.10, "OTM 5-10%")):
        if otm < edge:
            return label
    return "OTM >10%"


def readout(day_dirs: list[Path], top: int = 100, clean_only: bool = True) -> str:
    """Where outsized moves concentrate, across one or more archived days."""
    winners: list[dict[str, Any]] = []
    universe_rows = 0
    for day_dir in day_dirs:
        with gzip.open(day_dir / "contracts.csv.gz", "rt", encoding="utf-8") as src:
            for row in csv.DictReader(src):
                universe_rows += 1
                key = "clean_rank" if clean_only else "rank"
                if row.get(key) and int(row[key]) <= top:
                    row["strike_vs_spot_pct"] = float(row["strike_vs_spot_pct"]) if row.get("strike_vs_spot_pct") else None
                    row["days_to_expiration"] = (date.fromisoformat(row["expiration"]) - date.fromisoformat(day_dir.name)).days \
                        if _is_date(day_dir.name) else None
                    winners.append(row)
    lines = [f"# OA-203 readout — {', '.join(d.name for d in day_dirs)}", "",
             f"Top {top} {'clean ' if clean_only else ''}contracts per day by mid first-to-max return "
             f"({len(winners)} rows out of {universe_rows} contracts sampled).", ""]
    dims = [
        ("Underlying", lambda r: r["underlying"]),
        ("Call / put", lambda r: r["option_type"]),
        ("Strike vs spot at entry", _moneyness_bucket),
        ("Days to expiration", lambda r: r.get("days_to_expiration")),
        ("Entry hour (ET)", lambda r: _hour_bucket(r.get("mid_first_to_max_entry_ms"))),
        ("Exit hour (ET)", lambda r: _hour_bucket(r.get("mid_first_to_max_exit_ms"))),
    ]
    for title, key in dims:
        counts = Counter(key(r) for r in winners)
        lines += [f"## {title}", "", "| Value | Count | Share |", "|---|---:|---:|"]
        for value, count in counts.most_common(15):
            lines.append(f"| {value} | {count} | {count / max(1, len(winners)):.0%} |")
        lines.append("")
    return "\n".join(lines)


def _is_date(text: str) -> bool:
    try:
        date.fromisoformat(text)
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="daily session (cron)")
    uni = sub.add_parser("universe", help="select today's universe and print it (no sampling)")
    uni.add_argument("--date", type=date.fromisoformat, default=None)
    uni.add_argument("--size", type=int, default=None)
    smoke = sub.add_parser("smoke", help="sample named underlyings now, locally, without R2")
    smoke.add_argument("--symbols", required=True)
    smoke.add_argument("--sweeps", type=int, default=2)
    smoke.add_argument("--dir", type=Path, required=True)
    smoke.add_argument("--no-backfill", action="store_true")
    build = sub.add_parser("build", help="rebuild leaderboard from a local day directory")
    build.add_argument("--dir", type=Path, required=True)
    insp = sub.add_parser("inspect", help="show one contract's path and return calculation")
    insp.add_argument("--dir", type=Path, required=True)
    insp.add_argument("--symbol", required=True)
    rd = sub.add_parser("readout", help="concentration readout across day directories")
    rd.add_argument("--dir", type=Path, action="append", required=True)
    rd.add_argument("--top", type=int, default=100)
    rd.add_argument("--all", action="store_true", help="use all ranks, not only clean rows")
    args = parser.parse_args(argv)
    cfg = Config.from_env()

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)

    if args.command == "run":
        return run_day(cfg)
    if args.command == "universe":
        day = args.date or datetime.now(ET).date()
        client = Tradier(load_token(), cfg.max_rpm, cfg.reserve)
        with tempfile.TemporaryDirectory() as tmp:
            archive = DayArchive(Path(tmp), day, cfg.r2_prefix, upload=False)
            cfg.universe_size = args.size or cfg.universe_size
            universe = build_universe_for(day, client, cfg, archive)
        print(json.dumps({k: v for k, v in universe.items() if k != "selected"}, indent=2, default=str))
        for row in universe["selected"][:25]:
            print(row)
        return 0
    if args.command == "smoke":
        day = datetime.now(ET).date()
        cfg.upload = False
        client = Tradier(load_token(), cfg.max_rpm, cfg.reserve)
        archive = DayArchive(args.dir, day, cfg.r2_prefix, upload=False)
        ranked = [(s.strip().upper(), {"contracts": 0, "calls": 0, "puts": 0})
                  for s in args.symbols.split(",") if s.strip()]
        universe = select_universe(client, ranked, day, len(ranked), cfg.workers)
        universe["trade_date"] = day.isoformat()
        archive.write_json("universe.json", universe)
        for sweep in range(1, args.sweeps + 1):
            name = f"sweeps/sweep_{sweep:04d}.jsonl.gz"
            entry = run_sweep(client, universe["selected"], sweep, archive.path(name),
                              cfg.block_size, cfg.workers)
            archive.append_manifest(entry)
            log("oa203_sweep", sweep=sweep, rows=entry["rows"], status=entry["status_counts"])
        summary = finalize(archive, client, cfg, universe, None, None,
                           backfill_enabled=not args.no_backfill)
        print(json.dumps(summary, indent=2, default=str))
        return 0
    if args.command == "build":
        rows = build_contract_rows(args.dir, cfg.policy, *_bounds_from_summary(args.dir))
        print(json.dumps(write_outputs(args.dir, rows)))
        return 0
    if args.command == "inspect":
        print(json.dumps(inspect_contract(args.dir, args.symbol, cfg.policy), indent=2, default=str))
        return 0
    if args.command == "readout":
        print(readout(args.dir, args.top, clean_only=not args.all))
        return 0
    return 2


def _bounds_from_summary(day_dir: Path) -> tuple[int | None, int | None]:
    path = day_dir / "summary.json"
    if not path.exists():
        return None, None
    summary = json.loads(path.read_text(encoding="utf-8"))
    return summary.get("session_open_ms"), summary.get("session_close_ms")


if __name__ == "__main__":
    sys.exit(main())
