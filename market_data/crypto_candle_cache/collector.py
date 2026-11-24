"""Per-coin collection: fetch bases, derive intervals, write the cache.

Source plan per coin (fixed by :mod:`coins`):

- Coinbase-labelled coins: all three bases (``1m``/``1h``/``1d``)
  from :mod:`coinbase` (public candles, no key), paging backward
  from ``now`` over the requested window. When a Coinbase base
  fetch fails or returns fewer bars than the window asks for, the
  Tiingo backstop (:mod:`tiingo`) is tried for that base and the
  interval file records the source that actually supplied it.
- CoinGecko-labelled coins: the ``1d`` base only, from
  :mod:`coingecko` (public OHLC, 365 days). There is no 1m/1h base
  — CoinGecko free OHLC has no 1m/1h series — so the intraday
  intervals are absent by design and the sidecar says so. The
  ``1w`` file is derived from the 1d base.

Windows (``backfill`` defaults; overridable on the CLI): 1m covers
7 days, 1h covers 90 days, 1d covers the source's full depth
(Coinbase paging back to the product's first bar; CoinGecko 365
days). The deep first fill for the whole universe runs in the
parallel Crypto Backend job (``~/workspace/crypto-backend/``), not
here: this collector is the contract, the seed, and the steady-state
``update`` path. ``update`` fetches only bars newer than the newest
stored bar per base.

Membership is dynamic, mirroring the Coinbase Price Tracker: the
universe is the live top 50 by market cap (see
:func:`resolve_universe`), not the embedded 50-coin list — the
embedded list is the fallback and the diff baseline. A failed,
empty or partial CoinGecko top-markets fetch falls back to the
embedded 50 and records that fallback in every sidecar's
``universe`` block; a bad fetch never shrinks or empties the
cache. A coin that leaves the top 50 keeps its stored files (they
are never deleted); a coin that enters it is backfilled from
scratch. The 1d-base-only rule for CoinGecko-labelled coins is
unchanged: an entrant probed CoinGecko-sourced gets daily bars
only, never intraday.

Every bar written carries the One Clock envelope (see
:mod:`cache`): ``t`` is the source time, ``observed_at`` the fetch
time, ``origin`` the state chip. The still-forming latest bar of a
base is stored with ``"forming": true`` — it is a real bar, but it
is not yet an observation (its close is in the future at fetch
time) and :mod:`one_clock` excludes it from selection. Derived
bars are enveloped with ``origin="reconstructed"`` and the
generation time. A coin whose labelled source fails is recorded
``source_unavailable`` in the run summary with no files — a price
is never substituted, carried forward, or invented.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import cache as cache_mod
from . import coins as coins_mod
from . import resample as resample_mod
from .coingecko import CoinGeckoClient
from .coinbase import CoinbaseClient
from .coins import (COINS, coin_from_market_row, diff_universe,
                    parse_top_markets)
from .one_clock import detect_gaps
from .provenance import build_provenance
from .resample import _parse_time
from .tiingo import TiingoCryptoClient, TiingoError

UTC = timezone.utc

DEFAULT_WINDOWS = {"1m": timedelta(days=7), "1h": timedelta(days=90),
                   "1d": None}  # None: the source's full depth
COINGECKO_INTRADAY_ABSENT = "CoinGecko free OHLC has no 1m/1h series"


def _now_iso(now=None):
    moment = now or datetime.now(UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mark_forming(bars, interval, now):
    """Mark the still-forming latest bar(s) of a base (close after now)."""
    from .one_clock import bar_end_ms
    now_ms = int(now.timestamp() * 1000)
    out = []
    for bar in bars:
        marked = dict(bar)
        if bar_end_ms(bar, interval) > now_ms:
            marked["forming"] = True
        out.append(marked)
    return out


def universe_block(resolution):
    """The universe claim recorded in every provenance sidecar.

    ``resolution`` is a :func:`resolve_universe` result, or None
    (an explicit ``--symbols`` run, or a caller that did not
    resolve) — then the block is None and the sidecar carries no
    membership claim rather than a guessed one.
    """
    if resolution is None:
        return None
    return {"source": resolution["source"],
            "membership_source": resolution["source"],
            "as_of": resolution["as_of"],
            "coins": len(resolution["coins"]),
            "added": resolution["added"],
            "removed": resolution["removed"],
            "error": resolution["error"]}


def resolve_universe(clients, now=None):
    """Resolve the universe for this run: live top 50 vs embedded fallback.

    ``clients`` is the ``default_clients`` dict (only its
    ``"coingecko"`` and ``"coinbase"`` clients are used). Returns a
    dict with ``coins`` (the :class:`coins.Coin` tuple to collect,
    in live rank order), ``added`` / ``removed`` (the diff against
    the embedded list, each a list of change records with symbol,
    name, CoinGecko id, rank and source), ``source`` (``"live"`` or
    ``"embedded_fallback"``) and ``error`` (the fetch failure, or
    None). A failed, empty or partial top-markets fetch returns the
    embedded 50 with no recorded changes — a bad fetch never
    shrinks or empties the cache, and the fallback is visible in
    every sidecar's ``universe`` block.
    """
    now = now or datetime.now(UTC)
    as_of = now.date().isoformat()
    try:
        entries = parse_top_markets(clients["coingecko"].fetch_top_markets())
    except Exception as exc:  # noqa: BLE001 - any list failure falls back
        return {"coins": COINS, "added": [], "removed": [],
                "source": "embedded_fallback", "error": str(exc),
                "as_of": as_of}
    by_id = {coin.coingecko_id: coin for coin in COINS}
    live = []
    for entry in entries:
        embedded = by_id.get(entry["coingecko_id"])
        if embedded is not None:
            live.append(type(embedded)(
                embedded.symbol, embedded.name, embedded.coingecko_id,
                embedded.source, embedded.coinbase_product, entry["rank"]))
        else:
            source = ("coinbase" if clients["coinbase"].has_product(
                f"{entry['symbol']}-USD") else "coingecko")
            live.append(coin_from_market_row(entry, source))
    live = tuple(live)
    changes = diff_universe(COINS, live)
    return {"coins": live, "added": changes["added"],
            "removed": changes["removed"], "source": "live",
            "error": None, "as_of": as_of}


def fetch_coin_bases(coin, clients, now, windows=None, max_requests=None):
    """Fetch the three bases for one coin.

    ``clients`` is ``{"coinbase": ..., "tiingo": ..., "coingecko": ...}``.
    Returns ``(bases, meta, absent)`` where ``bases`` is
    ``{interval: enveloped bars}``, ``meta`` is ``{interval: {"source",
    "requests"}}`` and ``absent`` is ``{interval: reason}``. Raises
    ``RuntimeError`` when no base at all could be fetched (the caller
    records the coin ``source_unavailable``).
    """
    windows = dict(DEFAULT_WINDOWS, **(windows or {}))
    bases, meta, absent = {}, {}, {}
    requests = [0]

    def fetch_coinbase_base(interval):
        start = now - windows[interval] if windows[interval] else None
        # Full-depth 1d: page back from now in 300-bar steps; the
        # walk ends when a page adds no new bars.
        window_start = start or (now - timedelta(days=365 * 12))
        bars, made = clients["coinbase"].fetch_history(
            coin.coinbase_product, interval, window_start, now)
        requests[0] += made
        return bars, made

    def fetch_tiingo_base(interval):
        start = now - windows[interval] if windows[interval] else now - timedelta(days=365 * 9)
        bars, made = clients["tiingo"].fetch_bars(
            coin.symbol, interval,
            start.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d"))
        requests[0] += made
        return bars, made

    if coin.source == "coinbase":
        for interval in ("1m", "1h", "1d"):
            bars, made, source = [], 0, None
            try:
                bars, made = fetch_coinbase_base(interval)
                if bars:
                    source = "coinbase"
            except Exception:  # noqa: BLE001 - backstop, then give up
                bars, made = [], 0
            if not bars:
                try:
                    bars, made = fetch_tiingo_base(interval)
                    if bars:
                        source = "tiingo"
                except Exception:  # noqa: BLE001 - recorded absent below
                    bars, made = [], 0
            if not bars:
                absent[interval] = "Coinbase fetch failed and Tiingo backstop failed"
                continue
            bases[interval] = cache_mod.envelope_bars(
                _mark_forming(bars, interval, now), _now_iso(now), "recorded")
            meta[interval] = {"source": source, "requests": made}
    else:  # CoinGecko-labelled: daily base only.
        for interval in ("1m", "1h"):
            absent[interval] = COINGECKO_INTRADAY_ABSENT
        try:
            bars, made = clients["coingecko"].fetch_daily(coin.coingecko_id)
            requests[0] += made
        except Exception:  # noqa: BLE001 - recorded absent below
            bars, made = [], 0
        if not bars:
            absent["1d"] = "CoinGecko fetch failed"
        else:
            bases["1d"] = cache_mod.envelope_bars(
                _mark_forming(bars, "1d", now), _now_iso(now), "recorded")
            meta["1d"] = {"source": "coingecko", "requests": made}
    if max_requests is not None and requests[0] > max_requests:
        raise RuntimeError(f"request budget exceeded: {requests[0]} > {max_requests}")
    if not bases:
        raise RuntimeError(f"no base fetched for {coin.symbol}")
    return bases, meta, absent


def build_coin_files(coin, bases, meta, absent, generated_at):
    """Derive all intervals and build interval-file dicts + sidecar meta.

    Returns ``(files, provenance_meta)`` where ``files`` is
    ``{interval: content-dict}`` and ``provenance_meta`` is the
    ``files`` argument for :func:`provenance.build_provenance`.
    """
    derived = resample_mod.derive_all(
        {interval: [dict(b) for b in bars] for interval, bars in bases.items()})
    files = {}
    prov_meta = {"_absent": dict(absent)}
    generated_ms = int(datetime.fromisoformat(
        generated_at.replace("Z", "+00:00")).timestamp() * 1000)
    from .one_clock import bar_end_ms
    for interval in sorted(set(resample_mod.INTERVALS) - set(derived)):
        # A missing interval is listed as absent, never silently left
        # out: a missing base carries its own reason; a missing
        # derived interval carries its base's reason through.
        if interval in resample_mod.BASE_INTERVALS:
            reason = absent.get(interval, "base absent")
        else:
            base = resample_mod.DERIVED_FROM[interval]
            reason = absent.get(base, f"base {base} absent")
            reason = f"derived from {base}, which is absent: {reason}"
        prov_meta["_absent"][interval] = reason
    for interval, bars in derived.items():
        base = interval in resample_mod.BASE_INTERVALS
        source = meta[interval]["source"] if interval in meta else meta[
            resample_mod.DERIVED_FROM[interval]]["source"]
        enveloped = []
        for bar in bars:
            out = dict(bar)
            if base:
                out.setdefault("observed_at", generated_at)
                out.setdefault("origin", "recorded")
            else:
                out["observed_at"] = generated_at
                out["origin"] = "reconstructed"
            if bar_end_ms(out, interval) > generated_ms:
                out["forming"] = True
            enveloped.append(out)
        content = cache_mod.build_interval_file(
            coin.symbol, interval, enveloped, source, generated_at,
            base=base,
            derived_from=None if base else resample_mod.DERIVED_FROM[interval])
        files[interval] = content
        gaps = detect_gaps(enveloped, interval)
        prov_meta[interval] = {
            "bars": len(enveloped),
            "first_bar": enveloped[0]["t"] if enveloped else None,
            "last_bar": enveloped[-1]["t"] if enveloped else None,
            "derived_from": None if base else resample_mod.DERIVED_FROM[interval],
            "source": source,
            "requests": meta[interval]["requests"] if interval in meta else 0,
            "coverage": "partial" if gaps else "full",
            "reason": f"{len(gaps)} price gap(s)" if gaps else None,
        }
    return files, prov_meta


def collect_coin(coin, clients, out_dir, now, windows=None, max_requests=None,
                 publish=None, universe=None):
    """Collect one coin end to end; return a run-summary dict.

    Writes interval files + ``provenance.json`` under ``out_dir``.
    ``publish`` is an optional callable ``(symbol, files, provenance)``
    invoked after local writes (the CLI passes the R2 publisher).
    ``universe`` is a :func:`universe_block` dict recorded in the
    sidecar, or None. A failed coin returns ``{"symbol", "status":
    "source_unavailable", "reason"}`` with no files written.
    """
    generated_at = _now_iso(now)
    try:
        bases, meta, absent = fetch_coin_bases(
            coin, clients, now, windows=windows, max_requests=max_requests)
    except RuntimeError as exc:
        return {"symbol": coin.symbol, "status": "source_unavailable",
                "reason": str(exc)}
    files, prov_meta = build_coin_files(coin, bases, meta, absent, generated_at)
    for interval, content in files.items():
        cache_mod.write_interval_file(out_dir, coin.symbol, interval, content)
    provenance = build_provenance(
        coin.symbol, prov_meta, universe=universe, generated_at=generated_at,
        publication_status="not_published",
        publication_reason="local run: --publish not given")
    sidecar_path = Path(out_dir) / "crypto" / "candles" / coin.symbol / "provenance.json"
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    if publish is not None:
        publish(coin.symbol, files, provenance)
        provenance["publication"] = {"status": "published", "reason": None}
        provenance["latest_published_at"] = _now_iso()
        sidecar_path.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    return {"symbol": coin.symbol, "status": "ok",
            "intervals": sorted(files),
            "bars_total": sum(len(c["bars"]) for c in files.values()),
            "absent": sorted(prov_meta["_absent"])}


def update_coin(coin, clients, out_dir, now, max_requests=None, publish=None,
                universe=None):
    """Fetch only bars newer than the newest stored bar, per base.

    A coin with no stored files is backfilled instead (see
    :func:`collect_coin`). Stored forming bars are dropped before
    the merge — a forming bar's OHLC is still moving, so it is
    re-fetched rather than merged against. The Tiingo backstop is
    a backfill concern: a failed update leaves stored files
    untouched and returns ``source_unavailable`` for the run.
    ``universe`` is a :func:`universe_block` dict recorded in the
    sidecar, or None.
    """
    from .one_clock import bar_end_ms

    generated_at = _now_iso(now)
    stored = cache_mod.read_symbol_files(out_dir, coin.symbol)
    if not stored:
        return collect_coin(coin, clients, out_dir, now,
                            max_requests=max_requests, publish=publish,
                            universe=universe)
    now_ms = int(now.timestamp() * 1000)
    bases, meta, absent = {}, {}, {}
    requests = [0]
    for interval in ("1m", "1h", "1d"):
        content = stored.get(interval)
        if content is None:
            if coin.source == "coingecko":
                absent[interval] = COINGECKO_INTRADAY_ABSENT
            continue
        stored_bars = [b for b in content["bars"]
                       if bar_end_ms(b, interval) <= now_ms]
        newest = stored_bars[-1]["t"] if stored_bars else None
        start = (_parse_time(newest) if newest else
                 now - timedelta(days=7))
        try:
            if coin.source == "coinbase":
                bars, made = clients["coinbase"].fetch_history(
                    coin.coinbase_product, interval, start, now)
                source = "coinbase"
            else:
                bars, made = clients["coingecko"].fetch_daily(coin.coingecko_id)
                bars = [b for b in bars if b["t"] > (newest or "")]
                source = "coingecko"
        except Exception as exc:  # noqa: BLE001 - files untouched
            return {"symbol": coin.symbol, "status": "source_unavailable",
                    "reason": f"{interval} update failed: {exc}"}
        requests[0] += made
        enveloped = cache_mod.envelope_bars(
            _mark_forming(bars, interval, now), _now_iso(now), "recorded")
        merged = cache_mod.merge_bars(stored_bars, enveloped, interval)
        bases[interval] = merged
        meta[interval] = {"source": source, "requests": made}
    if max_requests is not None and requests[0] > max_requests:
        return {"symbol": coin.symbol, "status": "source_unavailable",
                "reason": f"request budget exceeded: {requests[0]} > {max_requests}"}
    if not bases:
        return {"symbol": coin.symbol, "status": "source_unavailable",
                "reason": "no stored bases to update"}
    files, prov_meta = build_coin_files(coin, bases, meta, absent, generated_at)
    for interval, content in files.items():
        cache_mod.write_interval_file(out_dir, coin.symbol, interval, content)
    provenance = build_provenance(
        coin.symbol, prov_meta, universe=universe, generated_at=generated_at,
        publication_status="not_published",
        publication_reason="local run: --publish not given")
    sidecar_path = Path(out_dir) / "crypto" / "candles" / coin.symbol / "provenance.json"
    sidecar_path.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    if publish is not None:
        publish(coin.symbol, files, provenance)
        provenance["publication"] = {"status": "published", "reason": None}
        provenance["latest_published_at"] = _now_iso()
        sidecar_path.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    return {"symbol": coin.symbol, "status": "ok",
            "intervals": sorted(files),
            "bars_total": sum(len(c["bars"]) for c in files.values()),
            "absent": sorted(prov_meta["_absent"])}


def default_clients():
    """Build the three source clients with default pacing."""
    return {
        "coinbase": CoinbaseClient(),
        "tiingo": TiingoCryptoClient(),
        "coingecko": CoinGeckoClient(),
    }


def run(coins, out_dir, clients=None, now=None, windows=None,
        max_requests=None, publish=None, universe=None):
    """Collect every coin in ``coins``; return per-coin summaries.

    ``universe`` is a :func:`universe_block` dict recorded in every
    sidecar, or None. The CLI resolves the live universe and passes
    it here; an explicit ``--symbols`` run passes None.
    """
    clients = clients or default_clients()
    now = now or datetime.now(UTC)
    return [collect_coin(coin, clients, out_dir, now, windows=windows,
                         max_requests=max_requests, publish=publish,
                         universe=universe)
            for coin in coins]
