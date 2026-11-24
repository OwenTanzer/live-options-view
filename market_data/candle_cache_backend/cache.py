"""Local cache reader and writer for the R2 candle layout.

The on-disk shape is exactly the PR #393 shape, so the PR #391 panel
keeps reading these files unchanged:

- ``nasdaq/candles/<TICKER>/<interval>.json`` — ``schema_version: 1``,
  ``dataset: "single_stock_candles"``, ``symbol``, ``interval``,
  ``base`` (true for 1m/1h/1d), ``derived_from`` (null for bases),
  ``source``, ``bars_count``, ``first_bar``, ``last_bar``,
  ``generated_at``, ``bars`` (``{"t","open","high","low","close"}``;
  daily/weekly bars also carry ``volume``/``adj_close``/
  ``adj_volume``; derived bars also carry ``bars``).
- ``nasdaq/candles/<TICKER>/provenance.json`` — the
  ``snapshot_provenance.v1`` sidecar (see ``provenance.py``), which
  also carries the One Clock envelope block.

The writer never corrects, drops or invents a bar: bars are stored
exactly as fetched (including a flat holiday stub bar), derived
intervals are aggregated by ``resample.py`` and never fetched, and a
ticker whose source fetch fails gets no files at all. The reader
stamps every returned bar with its envelope (``envelope.stamp_bar``)
and selects as-of a cutoff through ``selector`` — the stored file is
never mutated by a read. A stale cache is reported, never silently
refreshed: ``read_as_of`` returns the ``stale`` state when the latest
eligible bar is older than ``max_age_ms``, and the caller decides
what to show.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import resample as resample_mod
from .envelope import (
    evidence_key,
    format_time,
    observations_for_bars,
    parse_time,
    session_for,
)
from .provenance import build_provenance, interval_key, sidecar_key
from .selector import as_of_bars, reading_as_of, select_as_of

UTC = timezone.utc
BASES = ("1m", "1h", "1d")


def _now_iso():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def interval_file(symbol, interval, bars, generated_at):
    """The published content of one interval file (PR #393 shape)."""
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


def ticker_prefix(symbol):
    return f"nasdaq/candles/{symbol}"


def write_symbol_files(store, symbol, derived, generated_at=None,
                       origin="recorded", available_at=None,
                       requests=None, pages=None, revision=1):
    """Write the eleven interval files; return ``{interval: meta}``.

    ``derived`` is ``{interval: bars}`` for all eleven intervals
    (see ``resample.derive_all``). ``meta`` is what the provenance
    sidecar records per interval; request/page counts default to 0
    (the writer does not fetch, so it cannot know them — the
    backfill collector fills them in). ``available_at`` defaults to
    ``generated_at``: the write is when these bytes first became
    available in this cache's view. A re-write of unchanged bytes
    keeps the same ``revision``; the caller bumps it when the
    content changes.
    """
    generated_at = generated_at or _now_iso()
    available_at = available_at or generated_at
    meta = {}
    for interval in resample_mod.INTERVALS:
        content = interval_file(symbol, interval, derived[interval],
                                generated_at)
        store.write_json(interval_key(symbol, interval), content)
        meta[interval] = {
            "bars": len(derived[interval]),
            "first_bar": content["first_bar"],
            "last_bar": content["last_bar"],
            "derived_from": resample_mod.DERIVED_FROM.get(interval),
            "requests": (requests or {}).get(interval, 0),
            "pages": (pages or {}).get(interval, 0),
            "revision": revision,
            "origin": origin,
            "available_at": available_at,
            "evidence": interval_key(symbol, interval),
        }
    return meta


def write_symbol(store, symbol, derived, generated_at=None, origin="recorded",
                 available_at=None, requests=None, pages=None, revision=1,
                 universe=None, first_fetched_at=None, seed_note=None):
    """Write a ticker's interval files plus its provenance sidecar.

    Returns the provenance dict. The sidecar is written last, so a
    ticker is never left with files and no provenance claim.
    """
    meta = write_symbol_files(store, symbol, derived,
                              generated_at=generated_at, origin=origin,
                              available_at=available_at, requests=requests,
                              pages=pages, revision=revision)
    provenance = build_provenance(
        symbol, meta, universe=universe,
        generated_at=generated_at or _now_iso(),
        publication_status="not_published",
        publication_reason="local write: --publish not given",
        origin=origin, first_fetched_at=first_fetched_at,
        seed_note=seed_note)
    store.write_json(sidecar_key(symbol), provenance)
    return provenance


def read_interval(store, symbol, interval):
    """Read one stored interval file; raise ``KeyError`` if absent."""
    return store.read_json(interval_key(symbol, interval))


def read_bases(store, symbol):
    """Read a ticker's three stored bases; ``None`` when not cached.

    Only the three bases are read — derived intervals are rebuilt
    from them, never trusted as inputs.
    """
    bases = {}
    for base in BASES:
        try:
            bases[base] = read_interval(store, symbol, base)["bars"]
        except KeyError:
            return None
    return bases


def read_provenance(store, symbol):
    """Read a ticker's provenance sidecar; ``None`` when absent."""
    try:
        return store.read_json(sidecar_key(symbol))
    except KeyError:
        return None


def _envelope_for(store, symbol, interval, content):
    """The envelope facts for one interval file.

    From the provenance sidecar when present (``origin`` /
    ``revision`` / ``available_at`` per file); otherwise the
    cache-derived fallback — ``origin="recorded"``, ``revision=1``,
    ``available_at`` = the file's ``generated_at`` — labelled as
    such in ``details`` so a derived availability is never mistaken
    for a proven publication time.
    """
    provenance = read_provenance(store, symbol)
    entry = None
    if provenance:
        entry = provenance.get("envelope", {}).get("files", {}).get(interval)
    if entry is not None:
        return {"origin": entry.get("origin", "recorded"),
                "revision": entry.get("revision", 1),
                "available_at": entry.get("available_at"),
                "details": {"provenance": "snapshot_provenance.v1 sidecar"}}
    return {"origin": "recorded", "revision": 1,
            "available_at": content.get("generated_at"),
            "details": {"provenance": "cache-derived: no sidecar; "
                        "available_at is the file generated_at"}}


def stamped_bars(store, symbol, interval):
    """A ticker's bars for one interval, each with its envelope.

    Returns observation dicts (see ``envelope.stamp_bar``), sorted by
    event time. The stored file is not mutated. Raises ``KeyError``
    when the interval file is absent.
    """
    content = read_interval(store, symbol, interval)
    facts = _envelope_for(store, symbol, interval, content)
    return observations_for_bars(
        content["bars"], symbol=symbol, interval=interval,
        origin=facts["origin"], revision=facts["revision"],
        available_at=facts["available_at"], details=facts["details"])


def read_as_of(store, symbol, interval, *, cutoff, session=None,
               origin="recorded", max_age_ms=15 * 60 * 1000):
    """The cache state for one interval as of a chosen time.

    ``cutoff`` is epoch milliseconds or an offset-qualified ISO
    timestamp. ``session`` defaults to the session of the latest bar
    at or before the cutoff. Returns the ``selector.as_of_bars``
    result (``bars`` / ``reading`` / ``gaps`` / ``labels``) with the
    ``interval`` and ``symbol`` attached. A missing file is
    ``unavailable`` with no bars — never a substituted price. A
    stale cache (latest eligible bar older than ``max_age_ms``) is
    reported as ``stale`` in ``reading["state"]``; the bars are
    still returned, labelled, so the caller can show them as stale
    rather than silently refreshing.
    """
    try:
        rows = stamped_bars(store, symbol, interval)
    except KeyError:
        return {"symbol": symbol, "interval": interval, "bars": [],
                "reading": {"state": "unavailable", "row": None,
                            "ageMs": None, "publicationAgeMs": None},
                "gaps": [{"label": "unavailable", "from": None,
                          "to": None, "bars": 0}],
                "labels": ["unavailable"]}
    if session is None:
        cutoff_ms = parse_time(cutoff)
        eligible_times = [row["eventAt"] for row in rows
                          if row["eventAt"] <= cutoff_ms]
        if not eligible_times:
            return {"symbol": symbol, "interval": interval, "bars": [],
                    "reading": {"state": "unavailable", "row": None,
                                "ageMs": None, "publicationAgeMs": None},
                    "gaps": [{"label": "unavailable", "from": None,
                              "to": None, "bars": 0}],
                    "labels": ["unavailable"]}
        session = session_for(max(eligible_times))
    result = as_of_bars(rows, session=session, instrument=symbol,
                        origin=origin, cutoff=cutoff, max_age_ms=max_age_ms)
    result["symbol"] = symbol
    result["interval"] = interval
    return result


def cache_age_ms(store, symbol, interval, *, now=None):
    """Milliseconds since the latest bar in a cached interval file.

    ``None`` when the file is absent. Age is measured from the
    observation (bar) time, never from transport activity.
    """
    try:
        content = read_interval(store, symbol, interval)
    except KeyError:
        return None
    if not content["bars"]:
        return None
    now_ms = parse_time(now) if now is not None else int(
        datetime.now(UTC).timestamp() * 1000)
    return now_ms - parse_time(content["bars"][-1]["t"])


def verify_tree(store, symbol):
    """Check one ticker's cached files against its provenance sidecar.

    Returns ``{"ok", "problems", "checked"}``. Problems cover: a
    missing sidecar, a missing interval file, a bar count that
    disagrees with the sidecar, a first/last bar that disagrees, a
    file whose bars are not sorted by time, and an envelope block
    that is missing or lacks ``origin``/``revision``/
    ``available_at`` for a file. Verification never fetches and
    never rewrites.
    """
    problems = []
    checked = []
    provenance = read_provenance(store, symbol)
    if provenance is None:
        problems.append("missing provenance sidecar")
        return {"ok": False, "problems": problems, "checked": checked}
    envelope_files = provenance.get("envelope", {}).get("files", {})
    for interval in resample_mod.INTERVALS:
        checked.append(interval)
        try:
            content = read_interval(store, symbol, interval)
        except KeyError:
            problems.append(f"{interval}: missing interval file")
            continue
        entry = provenance.get("files", {}).get(interval, {})
        if content["bars_count"] != len(content["bars"]):
            problems.append(f"{interval}: bars_count disagrees with bars")
        if entry.get("bars") != len(content["bars"]):
            problems.append(f"{interval}: sidecar bar count disagrees")
        if content["bars"]:
            if content["first_bar"] != content["bars"][0]["t"]:
                problems.append(f"{interval}: first_bar disagrees")
            if content["last_bar"] != content["bars"][-1]["t"]:
                problems.append(f"{interval}: last_bar disagrees")
            times = [bar["t"] for bar in content["bars"]]
            if times != sorted(times):
                problems.append(f"{interval}: bars not sorted by time")
        env = envelope_files.get(interval)
        if env is None:
            problems.append(f"{interval}: envelope block missing")
        else:
            for field in ("origin", "revision", "available_at"):
                if field not in env:
                    problems.append(f"{interval}: envelope lacks {field}")
            if env.get("origin") not in ("recorded", "reconstructed",
                                         "synthetic"):
                problems.append(f"{interval}: envelope origin invalid")
    return {"ok": not problems, "problems": problems, "checked": checked}
