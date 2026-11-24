"""``snapshot_provenance.v1`` sidecar for one ticker's candle files.

Mirrors the PR #393 sidecar shape (``constructed_at``,
``archive_uploaded_at``, ``latest_published_at``,
``publication.status``, source label, as-of time, universe, one entry
per interval file with bar count, first/last bar, base or
``derived_from``, and Tiingo requests/pages) and adds one
``envelope`` block per file: the One Clock facts a reader needs
without opening the interval file — ``origin`` (``recorded`` /
``reconstructed`` / ``synthetic``), ``revision``, ``available_at``
(the earliest proven availability of these bytes in this cache's
view; null means unknown, never a substituted time) and
``evidence`` (the file's R2 key). The sidecar schema stays
``snapshot_provenance.v1``: the envelope is an additive block, not a
new schema version, so existing sidecar readers keep working.

One sidecar per ticker, not one per file: a ticker publishes eleven
interval files, so eleven sidecars per ticker would be 1,111 sidecar
objects for the universe. A missing sidecar means unknown provenance.
A ticker whose source fetch fails is recorded with
``status: "source_unavailable"`` and no files — a price is never
substituted, carried forward, or invented.
"""

from __future__ import annotations

SCHEMA = "snapshot_provenance.v1"
DATASET = "single_stock_candles"


def artifact_prefix(symbol):
    return f"nasdaq/candles/{symbol}"


def sidecar_key(symbol):
    return f"{artifact_prefix(symbol)}/provenance.json"


def interval_key(symbol, interval):
    return f"{artifact_prefix(symbol)}/{interval}.json"


def build_provenance(symbol, files, universe=None, generated_at=None,
                     publication_status="not_published",
                     publication_reason=None, archive_uploaded_at=None,
                     latest_published_at=None, origin="recorded",
                     first_fetched_at=None, seed_note=None):
    """Build the provenance sidecar for one ticker's interval files.

    ``files`` is ``{interval: {"bars", "first_bar", "last_bar",
    "derived_from", "requests", "pages", "revision", "available_at",
    "evidence"}}`` (see ``cache.write_symbol_files``). ``origin`` is
    the One Clock cohort for every file in this sidecar; a seeded
    sample passes ``origin="recorded"`` with ``first_fetched_at``
    naming when the bars were first fetched, and ``seed_note``
    describing the re-cut. ``publication_status`` is ``published``
    only after the R2 writes for this ticker succeed; otherwise
    ``not_published`` with a reason (e.g. ``"local run: --publish not
    given"``).
    """
    entries = {}
    envelope_entries = {}
    for interval in sorted(files):
        meta = files[interval]
        entries[interval] = {
            "key": interval_key(symbol, interval),
            "derived_from": meta.get("derived_from"),
            "bars": meta.get("bars"),
            "first_bar": meta.get("first_bar"),
            "last_bar": meta.get("last_bar"),
            "tiingo_requests": meta.get("requests"),
            "tiingo_pages": meta.get("pages"),
        }
        envelope_entries[interval] = {
            "origin": meta.get("origin", origin),
            "revision": meta.get("revision", 1),
            "available_at": meta.get("available_at", generated_at),
            "evidence": meta.get("evidence", interval_key(symbol, interval)),
        }
    first_bars = [meta.get("first_bar") for meta in files.values()
                  if meta.get("first_bar")]
    last_bars = [meta.get("last_bar") for meta in files.values()
                 if meta.get("last_bar")]
    sidecar = {
        "schema": SCHEMA,
        "dataset": DATASET,
        "artifact": artifact_prefix(symbol) + "/",
        "symbol": symbol,
        "constructed_at": generated_at,
        "archive_uploaded_at": archive_uploaded_at,
        "latest_published_at": latest_published_at,
        "publication": {"status": publication_status,
                        "reason": publication_reason},
        "source": "Tiingo (api.tiingo.com), token-authenticated, free Starter",
        "as_of": max(last_bars) if last_bars else None,
        "universe": universe,
        "files": entries,
        "envelope": {
            "source": "candle-cache",
            "origin": origin,
            "first_fetched_at": first_fetched_at,
            "files": envelope_entries,
        },
        "coverage": {
            "intervals": len(files),
            "bars_total": sum((meta.get("bars") or 0) for meta in files.values()),
            "first_bar": min(first_bars) if first_bars else None,
            "last_bar": max(last_bars) if last_bars else None,
        },
        "notes": [
            "Intraday bars (1m/1h and every interval derived from them) "
            "carry no volume: Tiingo intraday responses have no volume "
            "field, so none is stored. Daily bars and weekly bars derived "
            "from them carry volume.",
            "Derived intervals are aggregated by the backfill collector, "
            "never fetched: 3m/5m/9m/15m/30m from 1m, 2h/4h from 1h, "
            "1w from 1d.",
            "Envelope: each file's origin/revision/available_at/evidence "
            "is recorded under envelope.files. available_at is the "
            "earliest proven availability of these bytes in this cache's "
            "view; null means unknown, never a substituted time. A bar "
            "with no proven availability is ineligible at every One "
            "Clock cutoff.",
        ],
    }
    if seed_note:
        sidecar["notes"].append(seed_note)
    return sidecar
