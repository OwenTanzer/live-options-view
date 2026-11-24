"""Single-stock historical backfill orchestration.

Two modes, both per ticker, both resumable from the checkpoint
(``pacing.py``):

- **backfill**: fetch the three Tiingo bases (``1m`` = three newest
  pages of 10,000 bars, ``1h`` = back to 2017, ``1d`` = from 2000),
  derive the other eight intervals, write the eleven interval files
  plus the ticker's provenance sidecar to the local output tree
  (``<out>/nasdaq/candles/<TICKER>/``), and — only with ``--publish``
  — upload them to R2. Symbols already complete in the checkpoint are
  skipped unless ``force`` is given.
- **update**: for each ticker with local files, fetch only bars newer
  than the newest stored bar per base, merge, re-derive and rewrite.
  A ticker with no local files is backfilled instead.

Session model (stated explicitly): US equity regular sessions,
weekdays 09:30-16:00 America/New_York. Tiingo 1-minute bars are
already regular-session only (390/day); the resampler drops any
out-of-session bar rather than folding it in. Bars are stored exactly
as Tiingo returns them — including the occasional flat stub bar on a
market holiday at a page boundary (see ``tiingo.py``) — never
corrected, dropped or invented.

Membership is dynamic, mirroring PR #391's collector: the universe
is the live NASDAQ-100 list, re-fetched from Nasdaq's official
list-type endpoint on every run (see :func:`resolve_universe`),
not the embedded 101-listing list — the embedded list is the
fallback and the diff baseline. A failed, empty or garbled list
fetch falls back to the embedded 101 and records that fallback in
every sidecar's ``universe`` block; a bad fetch never empties the
backfill. A ticker that leaves the index is not backfilled or
updated further, but its stored files and checkpoint entries are
kept; a ticker that enters it is backfilled from scratch.

A ticker whose Tiingo fetch 404s (Tiingo has no such ticker) is
recorded ``source_unavailable`` in the checkpoint and the run summary,
with no files written; the run continues with the next ticker. A
rate-limit (429) is retried once after the server's Retry-After (or
the limiter's wait); a second 429 aborts the run with the checkpoint
intact, so the next run resumes where this one stopped.

One ticker is processed at a time, sequentially. Concurrency would
not help: the Tiingo hourly budget (50/hour) is the binding
constraint, and parallel requests would only hit it sooner.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, resample as resample_mod
from .pacing import CheckpointStore, RateLimiter
from .provenance import build_provenance, interval_key, sidecar_key
from .symbols import EXPECTED_LISTINGS, SOURCE_AS_OF, SOURCE_LABEL, SOURCE_URL
from .tiingo import BASE_SPECS, TiingoClient, TiingoNotFound, TiingoRateLimited

UTC = timezone.utc
BASES = ("1m", "1h", "1d")
RETRY_AFTER_CAP_SECONDS = 3600


from . import __version__, resample as resample_mod
from .pacing import CheckpointStore, RateLimiter
from .provenance import build_provenance, interval_key, sidecar_key
from .symbols import (EXPECTED_LISTINGS, SOURCE_AS_OF, SOURCE_LABEL,
                      SOURCE_URL, SYMBOLS, diff_universe,
                      parse_list_response, validate_unique)
from .tiingo import BASE_SPECS, TiingoClient, TiingoNotFound, TiingoRateLimited

UTC = timezone.utc
BASES = ("1m", "1h", "1d")
RETRY_AFTER_CAP_SECONDS = 3600
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _session():
    import requests
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT,
                            "Accept": "application/json"})
    return session


def fetch_list_members(session):
    """Fetch the live constituent list in one request.

    Returns the sorted ``[(symbol, name)]`` list from
    :func:`symbols.parse_list_response`. Raises on transport/HTTP
    errors or on an empty/garbled response; the caller falls back
    to the embedded universe rather than publishing a bad one.
    """
    resp = session.get(SOURCE_URL, timeout=20)
    resp.raise_for_status()
    return parse_list_response(resp.json())


def resolve_universe(session=None, now=None):
    """Resolve the universe for this run: live list vs embedded fallback.

    Returns a dict with ``universe`` (sorted ``[(symbol, name)]``),
    ``added`` / ``removed`` (the diff against the embedded list,
    each a sorted ``[(symbol, name)]``), ``source`` (``"live"`` or
    ``"embedded_fallback"``) and ``error`` (the fetch failure, or
    None). A failed, empty or garbled list fetch returns the
    embedded universe with no recorded changes — a bad fetch never
    empties the backfill, and the fallback is visible in every
    sidecar's ``universe`` block.
    """
    now = now or datetime.now(UTC)
    own_session = session is None
    session = session or _session()
    try:
        try:
            members = fetch_list_members(session)
        except Exception as exc:  # noqa: BLE001 - any list failure falls back
            return {"universe": list(SYMBOLS), "added": [], "removed": [],
                    "source": "embedded_fallback", "error": str(exc),
                    "as_of": now.date().isoformat()}
        validate_unique(members)
        changes = diff_universe(SYMBOLS, members)
        return {"universe": members, "added": changes["added"],
                "removed": changes["removed"], "source": "live",
                "error": None, "as_of": now.date().isoformat()}
    finally:
        if own_session:
            session.close()


def live_universe():
    """Resolve the live universe with a throwaway session (CLI helper)."""
    return resolve_universe()


def universe_block(resolution=None):
    """The universe claim recorded in every provenance sidecar.

    ``resolution`` is a :func:`resolve_universe` result, or None —
    then the block describes the embedded universe with no
    membership claim, as before. ``listings`` is the live count when
    a resolution is given; ``companies`` counts distinct companies
    the same way PR #391 does (Alphabet Inc. appears twice, as
    GOOGL/GOOG, so companies is one less than listings for the
    embedded universe).
    """
    if resolution is None:
        return {"source": SOURCE_LABEL, "source_url": SOURCE_URL,
                "as_of": SOURCE_AS_OF, "listings": EXPECTED_LISTINGS,
                "companies": 100}
    universe = resolution["universe"]
    companies = len({name.split(" Common")[0].split(" Class")[0].strip()
                     for _, name in universe})
    return {"source": SOURCE_LABEL, "source_url": SOURCE_URL,
            "as_of": SOURCE_AS_OF, "listings": len(universe),
            "companies": companies,
            "membership_source": resolution["source"],
            "added": [{"symbol": s, "name": n}
                      for s, n in resolution["added"]],
            "removed": [{"symbol": s, "name": n}
                        for s, n in resolution["removed"]],
            "resolution_as_of": resolution["as_of"],
            "error": resolution["error"]}


def _now_iso():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def interval_file(symbol, interval, bars, generated_at):
    """The published content of one interval file."""
    derived_from = resample_mod.DERIVED_FROM.get(interval)
    return {
        "schema_version": 1,
        "dataset": "single_stock_candles",
        "symbol": symbol,
        "interval": interval,
        "base": derived_from is None,
        "derived_from": derived_from,
        "source": "Tiingo (api.tiingo.com)",
        "bars_count": len(bars),
        "first_bar": bars[0]["t"] if bars else None,
        "last_bar": bars[-1]["t"] if bars else None,
        "generated_at": generated_at,
        "bars": bars,
    }


def ticker_dir(out_dir, symbol):
    return Path(out_dir) / "nasdaq" / "candles" / symbol


def write_symbol_files(out_dir, symbol, derived, generated_at):
    """Write the eleven interval files; return ``{interval: meta}``.

    ``meta`` is what the provenance sidecar records per interval
    (bars, first/last bar, derived_from); request/page counts are
    filled in by the caller, which knows the fetch cost.
    """
    directory = ticker_dir(out_dir, symbol)
    directory.mkdir(parents=True, exist_ok=True)
    meta = {}
    for interval in resample_mod.INTERVALS:
        content = interval_file(symbol, interval, derived[interval], generated_at)
        (directory / f"{interval}.json").write_bytes(json.dumps(content, indent=2).encode("utf-8"))
        meta[interval] = {
            "bars": len(derived[interval]),
            "first_bar": content["first_bar"],
            "last_bar": content["last_bar"],
            "derived_from": resample_mod.DERIVED_FROM.get(interval),
            "requests": 0,
            "pages": 0,
        }
    return meta


def read_symbol_files(out_dir, symbol):
    """Read a ticker's stored interval files; return ``{interval: bars}``.

    Returns None when the ticker has no stored files (never backfilled).
    Only the three bases are read — the derived intervals are rebuilt
    from them, never trusted as inputs.
    """
    directory = ticker_dir(out_dir, symbol)
    bases = {}
    for base in BASES:
        path = directory / f"{base}.json"
        if not path.exists():
            return None
        content = json.loads(path.read_text())
        bases[base] = content.get("bars") or []
    return bases


def _fetch_with_pacing(limiter, fetch):
    """Run one Tiingo fetch under the rate limiter.

    ``fetch`` is a zero-argument callable returning ``(bars, pages)``.
    Pacing itself happens per HTTP request inside the client's ``_get``
    (one fetch can page into several requests); this wrapper only
    handles a 429: sleep once (server Retry-After capped at one hour,
    else the limiter's own wait) and retry once. A second 429
    propagates so the run aborts with the checkpoint intact.
    """
    try:
        return fetch()
    except TiingoRateLimited as exc:
        wait = exc.retry_after
        if wait is None:
            wait = limiter.wait_seconds()
        wait = min(wait, RETRY_AFTER_CAP_SECONDS)
        if wait > 0:
            import time
            time.sleep(wait)
        return fetch()


def backfill_symbol(client, limiter, checkpoint, symbol, out_dir,
                    pages_1m=3, on_symbol_done=None, force=False,
                    universe=None):
    """Backfill one ticker; return its summary dict.

    ``on_symbol_done`` (optional) is called with
    ``(symbol, files, provenance)`` after the local files are written,
    so the CLI can publish the ticker immediately when ``--publish``
    is given — a killed 12-hour run still publishes what it finished.
    """
    generated_at = _now_iso()
    bases = {}
    base_requests = {}
    base_pages = {}
    for base in BASES:
        if not force and checkpoint.is_complete(symbol, base):
            stored = read_symbol_files(out_dir, symbol)
            if stored is not None:
                bases[base] = stored[base]
                entry = checkpoint.symbol_status(symbol)[base]
                base_requests[base] = entry.get("requests", 0)
                base_pages[base] = entry.get("pages", 0)
                continue
        if base == "1m":
            override_pages = pages_1m
        else:
            override_pages = None
        made_before = getattr(client, "requests_made", 0)
        bars, pages = _fetch_with_pacing(
            limiter,
            lambda b=base, p=override_pages: client.fetch_base(symbol, b, max_pages=p))
        made_after = getattr(client, "requests_made", 0)
        bases[base] = bars
        base_requests[base] = max(pages, made_after - made_before)
        base_pages[base] = pages
        checkpoint.mark_base(
            symbol, base, "complete", pages=pages, bars=len(bars),
            first_bar=bars[0]["t"] if bars else None,
            last_bar=bars[-1]["t"] if bars else None,
            requests=base_requests[base])
        checkpoint.save()
    derived = resample_mod.derive_all(bases)
    meta = write_symbol_files(out_dir, symbol, derived, generated_at)
    for base in BASES:
        meta[base]["requests"] = base_requests[base]
        meta[base]["pages"] = base_pages[base]
    provenance = build_provenance(
        symbol, meta, universe=universe_block(universe),
        generated_at=generated_at,
        publication_status="not_published",
        publication_reason="local run: --publish not given")
    (ticker_dir(out_dir, symbol) / "provenance.json").write_text(
        json.dumps(provenance, indent=2))
    files = {interval: interval_file(symbol, interval, derived[interval], generated_at)
             for interval in resample_mod.INTERVALS}
    if on_symbol_done is not None:
        on_symbol_done(symbol, files, provenance)
    return {"symbol": symbol, "status": "complete",
            "bases": {base: {"bars": len(bases[base]),
                             "first_bar": bases[base][0]["t"] if bases[base] else None,
                             "last_bar": bases[base][-1]["t"] if bases[base] else None,
                             "requests": base_requests[base],
                             "pages": base_pages[base]} for base in BASES},
            "intervals": {iv: len(derived[iv]) for iv in resample_mod.INTERVALS}}


def update_symbol(client, limiter, checkpoint, symbol, out_dir,
                  pages_1m=3, on_symbol_done=None, universe=None):
    """Incrementally update one ticker; return its summary dict.

    Fetches only bars newer than the newest stored bar per base. A
    ticker with no stored files is backfilled instead.
    """
    stored = read_symbol_files(out_dir, symbol)
    if stored is None:
        return backfill_symbol(client, limiter, checkpoint, symbol, out_dir,
                               pages_1m=pages_1m, on_symbol_done=on_symbol_done,
                               universe=universe)
    generated_at = _now_iso()
    bases = {}
    new_counts = {}
    base_requests = {}
    base_pages = {}
    for base in BASES:
        existing = stored[base]
        made_before = getattr(client, "requests_made", 0)
        if not existing:
            bars, pages = _fetch_with_pacing(
                limiter, lambda b=base: client.fetch_base(symbol, b))
            bases[base] = bars
            new_counts[base] = len(bars)
        else:
            after_t = existing[-1]["t"]
            bars, pages = _fetch_with_pacing(
                limiter,
                lambda b=base, a=after_t: client.fetch_newer(symbol, b, a))
            merged = {bar["t"]: bar for bar in existing}
            for bar in bars:
                merged[bar["t"]] = bar
            bases[base] = [merged[t] for t in sorted(merged)]
            new_counts[base] = len(bars)
        made_after = getattr(client, "requests_made", 0)
        base_requests[base] = max(pages, made_after - made_before)
        base_pages[base] = pages
        checkpoint.mark_base(
            symbol, base, "complete", pages=pages,
            bars=len(bases[base]),
            first_bar=bases[base][0]["t"] if bases[base] else None,
            last_bar=bases[base][-1]["t"] if bases[base] else None,
            requests=base_requests[base])
        checkpoint.save()
    derived = resample_mod.derive_all(bases)
    meta = write_symbol_files(out_dir, symbol, derived, generated_at)
    for base in BASES:
        meta[base]["requests"] = base_requests[base]
        meta[base]["pages"] = base_pages[base]
    provenance = build_provenance(
        symbol, meta, universe=universe_block(universe),
        generated_at=generated_at,
        publication_status="not_published",
        publication_reason="local run: --publish not given")
    (ticker_dir(out_dir, symbol) / "provenance.json").write_text(
        json.dumps(provenance, indent=2))
    files = {interval: interval_file(symbol, interval, derived[interval], generated_at)
             for interval in resample_mod.INTERVALS}
    if on_symbol_done is not None:
        on_symbol_done(symbol, files, provenance)
    return {"symbol": symbol, "status": "updated",
            "bases": {base: {"bars": len(bases[base]),
                             "new_bars": new_counts[base],
                             "first_bar": bases[base][0]["t"] if bases[base] else None,
                             "last_bar": bases[base][-1]["t"] if bases[base] else None,
                             "requests": base_requests[base]} for base in BASES},
            "intervals": {iv: len(derived[iv]) for iv in resample_mod.INTERVALS}}


def run(symbols, out_dir, mode="backfill", client=None, checkpoint=None,
        pages_1m=3, on_symbol_done=None, force=False,
        universe_resolution=None):
    """Run ``mode`` (``backfill`` or ``update``) over ``symbols``.

    Returns a summary dict. A ticker whose Tiingo fetch 404s is
    recorded ``source_unavailable`` and skipped; a second 429 aborts
    the run (the exception propagates after the checkpoint is saved).
    ``force`` re-fetches symbols the checkpoint already marks complete
    (backfill only; update always re-derives from stored bars).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if checkpoint is None:
        checkpoint = CheckpointStore(out_dir / ".backfill-checkpoint.json")
        checkpoint.load()
    if client is None:
        client = TiingoClient(limiter=checkpoint.limiter)
    elif getattr(client, "limiter", None) is None:
        client.limiter = checkpoint.limiter
    results = []
    try:
        for symbol in symbols:
            try:
                if mode == "update":
                    summary = update_symbol(client, checkpoint.limiter, checkpoint,
                                            symbol, out_dir, pages_1m=pages_1m,
                                            on_symbol_done=on_symbol_done,
                                            universe=universe_resolution)
                else:
                    summary = backfill_symbol(client, checkpoint.limiter, checkpoint,
                                              symbol, out_dir, pages_1m=pages_1m,
                                              on_symbol_done=on_symbol_done,
                                              force=force,
                                              universe=universe_resolution)
            except TiingoNotFound:
                for base in BASES:
                    checkpoint.mark_base(symbol, base, "source_unavailable")
                checkpoint.save()
                summary = {"symbol": symbol, "status": "source_unavailable",
                           "bases": {}, "intervals": {}}
            results.append(summary)
    finally:
        # A 429 abort (or any other failure) must not lose the request
        # log: the next run resumes from this checkpoint, and the
        # limiter needs the failed attempts in its window to pace
        # correctly instead of hammering an exhausted budget.
        checkpoint.save()
    complete = sum(1 for r in results if r["status"] in ("complete", "updated"))
    unavailable = sum(1 for r in results if r["status"] == "source_unavailable")
    total_requests = sum(
        (r.get("bases") or {}).get(base, {}).get("requests", 0)
        for r in results for base in BASES)
    return {
        "collector": "single_stock_historical_backfill",
        "version": __version__,
        "mode": mode,
        "universe": universe_block(universe_resolution),
        "symbols": len(symbols),
        "complete": complete,
        "source_unavailable": unavailable,
        "tiingo_requests": total_requests,
        "generated_at": _now_iso(),
        "results": results,
    }
