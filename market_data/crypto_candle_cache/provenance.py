"""``snapshot_provenance.v1`` sidecar for one coin's candle files.

Mirrors the single-stock backfill's sidecar shape (see
``market_data/single_stock_historical_backfill/provenance.py``, PR
#393): ``constructed_at``, ``archive_uploaded_at``,
``latest_published_at``, ``publication.status`` (``published``, or
``not_published`` with a reason), the source label, the as-of time
and the universe. One sidecar per coin, not one per file: a coin
publishes eleven interval files, so eleven sidecars per coin would
be 550 sidecar objects for the universe. The single sidecar lists
every interval file with its bar count, first/last bar, base or
``derived_from``, the source that actually supplied it, and the
requests behind it — the same claims, one object per coin.

Coverage is stated per interval, honestly:

- ``"full"`` — the interval file exists and spans the requested
  window (for a backfill, the source's full depth).
- ``"partial"`` — the file exists but does not span the window, or
  contains a price gap (see :func:`one_clock.detect_gaps`).
- ``"absent"`` — no file, with a reason. CoinGecko-labelled coins
  have no intraday base at all (CoinGecko free OHLC has no 1m/1h
  series), so their 1m/3m/5m/9m/15m/30m/1h/2h/4h entries are
  absent by design, not by failure. A coin whose labelled source
  fails is recorded with ``status: "source_unavailable"`` and no
  files — a price is never substituted, carried forward, or
  invented.

A missing sidecar means unknown provenance.
"""

from __future__ import annotations

SCHEMA = "snapshot_provenance.v1"
DATASET = "crypto_candles"


def artifact_prefix(symbol):
    return f"crypto/candles/{symbol}"


def sidecar_key(symbol):
    return f"{artifact_prefix(symbol)}/provenance.json"


def interval_key(symbol, interval):
    return f"{artifact_prefix(symbol)}/{interval}.json"


def build_provenance(symbol, files, universe=None, generated_at=None,
                     publication_status="not_published",
                     publication_reason=None, archive_uploaded_at=None,
                     latest_published_at=None):
    """Build the provenance sidecar for one coin's interval files.

    ``files`` is ``{interval: {"bars", "first_bar", "last_bar",
    "derived_from", "source", "requests", "coverage", "reason"}}``.
    Intervals absent from ``files`` are listed as absent with the
    caller's reason in ``files`` under the ``"_absent"`` key
    (``{interval: reason}``) when the caller knows why. ``publication_status``
    is ``published`` only after the R2 writes for this coin succeed;
    otherwise ``not_published`` with a reason (e.g. ``"local run:
    --publish not given"``).
    """
    entries = {}
    for interval in sorted(files):
        if interval.startswith("_"):
            continue
        meta = files[interval]
        entries[interval] = {
            "key": interval_key(symbol, interval),
            "derived_from": meta.get("derived_from"),
            "source": meta.get("source"),
            "bars": meta.get("bars"),
            "first_bar": meta.get("first_bar"),
            "last_bar": meta.get("last_bar"),
            "requests": meta.get("requests"),
            "coverage": meta.get("coverage", "full"),
            "reason": meta.get("reason"),
        }
    absent = files.get("_absent", {})
    for interval in sorted(absent):
        entries[interval] = {
            "key": interval_key(symbol, interval),
            "derived_from": None,
            "source": None,
            "bars": 0,
            "first_bar": None,
            "last_bar": None,
            "requests": 0,
            "coverage": "absent",
            "reason": absent[interval],
        }
    first_bars = [meta.get("first_bar") for meta in files.values()
                  if isinstance(meta, dict) and meta.get("first_bar")]
    last_bars = [meta.get("last_bar") for meta in files.values()
                 if isinstance(meta, dict) and meta.get("last_bar")]
    return {
        "schema": SCHEMA,
        "dataset": DATASET,
        "artifact": artifact_prefix(symbol) + "/",
        "symbol": symbol,
        "constructed_at": generated_at,
        "archive_uploaded_at": archive_uploaded_at,
        "latest_published_at": latest_published_at,
        "publication": {"status": publication_status,
                        "reason": publication_reason},
        "session_model": "continuous_24_7",
        "as_of": max(last_bars) if last_bars else None,
        "universe": universe,
        "files": entries,
        "coverage": {
            "intervals": len([i for i in entries
                              if entries[i]["coverage"] != "absent"]),
            "bars_total": sum((meta.get("bars") or 0) for meta in entries.values()),
            "first_bar": min(first_bars) if first_bars else None,
            "last_bar": max(last_bars) if last_bars else None,
        },
        "notes": [
            "Crypto trades 24/7: buckets align to 00:00 UTC, not an "
            "equity session open. There is no session filter; a bar "
            "is never dropped for falling outside market hours.",
            "Every bar carries its source time (t), observed time "
            "(observed_at) and state chip (origin: recorded for a "
            "fetched base bar, reconstructed for a locally derived "
            "bar). Synthetic bars are never written by this collector.",
            "Derived intervals are aggregated by the collector, never "
            "fetched: 3m/5m/9m/15m/30m from 1m, 2h/4h from 1h, 1w from 1d.",
            "CoinGecko-labelled coins have no intraday base: CoinGecko "
            "free OHLC has no 1m/1h series, so their intraday "
            "intervals are absent by design and labelled as such.",
        ],
    }
