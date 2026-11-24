"""Local cache reader and writer for crypto candle files.

One interval file per coin per interval, in the R2 layout
``crypto/candles/<COIN>/<interval>.json`` (mirroring the stock
backfill's ``nasdaq/candles/<TICKER>/<interval>.json``):

.. code-block:: json

    {
      "schema_version": 1,
      "dataset": "crypto_candles",
      "symbol": "BTC",
      "interval": "1h",
      "base": true,
      "derived_from": null,
      "source": "coinbase",
      "source_label": "Coinbase Exchange public candles (api.exchange.coinbase.com), no key",
      "bars_count": 168,
      "first_bar": "2026-11-17T00:00:00Z",
      "last_bar": "2026-11-23T23:00:00Z",
      "generated_at": "2026-11-24T00:30:00Z",
      "bars": [{"t": "...", "open": 1.0, "high": 1.0, "low": 1.0,
                "close": 1.0, "volume": 1.0,
                "observed_at": "...", "origin": "recorded"}]
    }

Rules the writer enforces and the reader checks:

- ``t`` is ISO-8601 UTC, the bar start as labelled by the source —
  the bar's **source time**. ``observed_at`` is when this collector
  fetched the bar — the **observed time**. ``origin`` is the state
  chip: ``"recorded"`` for a base bar fetched from a provider,
  ``"reconstructed"`` for a bar derived locally by :mod:`resample`.
  ``"synthetic"`` is never written by this collector.
- A bar carries no ``volume`` key when its source carries none
  (CoinGecko daily) — never null, never zero. A derived bar adds
  ``"bars"`` (base bars folded in); a base bar never carries it.
- The still-forming latest bar is stored with ``"forming": true``
  and is excluded from One Clock selection by
  :mod:`one_clock` (its close is in the future at fetch time).
- Bars are stored sorted by ``t``, deduped by ``t``; a re-fetch
  that changes a stored bar is a new revision of the same bar, and
  the writer refuses to silently overwrite a stored bar with
  different OHLC unless ``force`` is given — see :func:`merge_bars`.
  A missing interval file means the interval is absent (labelled
  in provenance), never an empty file standing in for data.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .one_clock import ORIGINS
from .resample import BASE_INTERVALS, DERIVED_FROM, INTERVALS

SCHEMA_VERSION = 1
DATASET = "crypto_candles"

SOURCE_LABELS = {
    "coinbase": "Coinbase Exchange public candles (api.exchange.coinbase.com), no key",
    "tiingo": "Tiingo crypto prices (api.tiingo.com), token-authenticated, free Starter",
    "coingecko": "CoinGecko public OHLC (api.coingecko.com), no key",
}


def artifact_prefix(symbol):
    return f"crypto/candles/{symbol}"


def interval_key(symbol, interval):
    return f"{artifact_prefix(symbol)}/{interval}.json"


def interval_path(out_dir, symbol, interval):
    """Local path of an interval file under an output directory."""
    return Path(out_dir) / "crypto" / "candles" / symbol / f"{interval}.json"


def validate_bar(bar, interval):
    """Fail closed on a malformed bar."""
    if not isinstance(bar, dict):
        raise ValueError(f"bar is not an object: {bar!r}")
    for field in ("t", "open", "high", "low", "close"):
        if field not in bar:
            raise ValueError(f"bar missing {field}: {bar!r}")
    for field in ("open", "high", "low", "close"):
        if not isinstance(bar[field], (int, float)) or isinstance(bar[field], bool):
            raise ValueError(f"bar {field} is not a number: {bar!r}")
    if bar["high"] < max(bar["open"], bar["close"]) or bar["low"] > min(bar["open"], bar["close"]):
        raise ValueError(f"bar high/low inconsistent with open/close: {bar!r}")
    if bar.get("volume") is not None and not isinstance(bar["volume"], (int, float)):
        raise ValueError(f"bar volume is not a number: {bar!r}")
    if bar.get("origin", "recorded") not in ORIGINS:
        raise ValueError(f"bar origin is not a state chip: {bar!r}")
    if interval in BASE_INTERVALS and bar.get("bars") is not None:
        raise ValueError(f"base bar carries derived bars count: {bar!r}")
    return bar


def merge_bars(stored, incoming, interval, force=False):
    """Merge incoming bars into stored bars, deduped by ``t``.

    Bars are keyed by ``t``. An incoming bar with the same ``t``
    and identical OHLC/volume replaces the stored bar (same
    revision, new ``observed_at``). An incoming bar with the same
    ``t`` but different OHLC is a new revision of the same bar: it
    replaces the stored bar only when ``force`` is given, and the
    caller must record the revision bump; otherwise a
    ``ValueError`` is raised rather than silently overwriting
    evidence. Returns the merged list sorted by ``t``.
    """
    merged = {bar["t"]: dict(bar) for bar in stored}
    for bar in incoming:
        validate_bar(bar, interval)
        previous = merged.get(bar["t"])
        if previous is not None:
            same = all(previous.get(f) == bar.get(f)
                       for f in ("open", "high", "low", "close", "volume"))
            if not same and not force:
                raise ValueError(
                    f"bar {bar['t']} changed OHLC; refusing silent overwrite "
                    "(pass force=True to record a new revision)")
        merged[bar["t"]] = dict(bar)
    return [merged[key] for key in sorted(merged)]


def build_interval_file(symbol, interval, bars, source, generated_at,
                        base=None, derived_from=None):
    """Build one interval file dict from bars (already enveloped).

    ``bars`` carry ``observed_at`` and ``origin`` (attached by the
    collector from the fetch). ``base`` defaults to whether the
    interval is a base interval; ``derived_from`` names the base
    for derived intervals, else None.
    """
    if interval not in INTERVALS:
        raise ValueError(f"unknown interval: {interval!r}")
    if base is None:
        base = interval in BASE_INTERVALS
    if base:
        derived_from = None
    elif derived_from is None:
        derived_from = DERIVED_FROM[interval]
    ordered = [validate_bar(dict(bar), interval)
               for bar in sorted(bars, key=lambda b: b["t"])]
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": DATASET,
        "symbol": symbol,
        "interval": interval,
        "base": base,
        "derived_from": derived_from,
        "source": source,
        "source_label": SOURCE_LABELS[source],
        "bars_count": len(ordered),
        "first_bar": ordered[0]["t"] if ordered else None,
        "last_bar": ordered[-1]["t"] if ordered else None,
        "generated_at": generated_at,
        "bars": ordered,
    }


def write_interval_file(out_dir, symbol, interval, content):
    """Write one interval file under ``out_dir``; return its path."""
    path = interval_path(out_dir, symbol, interval)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
    return path


def read_interval_file(path):
    """Read and validate one interval file; return the content dict.

    Raises ``ValueError`` on a schema/dataset mismatch, a bar
    count mismatch, or a malformed bar. A missing file raises
    ``FileNotFoundError`` — the caller labels that "absent", it
    never stands in for an empty file.
    """
    content = json.loads(Path(path).read_text(encoding="utf-8"))
    if content.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version in {path}")
    if content.get("dataset") != DATASET:
        raise ValueError(f"unsupported dataset in {path}")
    bars = content.get("bars")
    if not isinstance(bars, list):
        raise ValueError(f"no bars list in {path}")
    if content.get("bars_count") != len(bars):
        raise ValueError(f"bars_count mismatch in {path}")
    for bar in bars:
        validate_bar(bar, content.get("interval"))
    if [bar["t"] for bar in bars] != sorted(bar["t"] for bar in bars):
        raise ValueError(f"bars not sorted by t in {path}")
    return content


def read_symbol_files(out_dir, symbol):
    """Read every interval file present for a coin; return ``{interval: content}``."""
    found = {}
    for interval in INTERVALS:
        path = interval_path(out_dir, symbol, interval)
        if path.exists():
            found[interval] = read_interval_file(path)
    return found


def envelope_bars(bars, observed_at, origin):
    """Attach the One Clock envelope to fetched bars.

    ``observed_at`` is the fetch time (ISO-8601 UTC); ``origin`` is
    ``"recorded"`` for fetched bars. Derived bars are enveloped by
    the collector with ``origin="reconstructed"`` and the
    generation time instead — see :mod:`collector`.
    """
    out = []
    for bar in bars:
        enveloped = dict(bar)
        enveloped["observed_at"] = observed_at
        enveloped["origin"] = origin
        out.append(enveloped)
    return out
