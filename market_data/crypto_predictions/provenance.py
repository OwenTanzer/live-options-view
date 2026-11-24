"""``snapshot_provenance.v1`` sidecar for one coin's predictions.

Mirrors the candle cache's sidecar shape (see
``market_data/crypto_candle_cache/provenance.py``): ``constructed_at``,
``archive_uploaded_at``, ``latest_published_at``, ``publication.status``
(``published``, or ``not_published`` with a reason), the source label,
the as-of time and the universe. One sidecar per coin.

Coverage is stated honestly:

- ``"current"`` — the file holds a live estimate for the next hour.
- ``"historical"`` — every published estimate's target hour has
  passed, but the value rows are retained as historical evidence
  (never retracted), so the prediction can still be judged.
- ``"absent"`` — no prediction file, with a reason. CoinGecko-labelled
  coins have no 1h base at all (CoinGecko free OHLC has no 1m/1h
  series), so they have no estimate by design, not by failure. A coin
  whose 1h file holds fewer than 2 completed bars is likewise absent.

A missing sidecar means unknown provenance.
"""

from __future__ import annotations

from market_data.crypto_candle_cache import one_clock as oc

SCHEMA = "snapshot_provenance.v1"
DATASET = "crypto_predictions"


def artifact_prefix(symbol):
    return f"crypto/predictions/{symbol}"


def sidecar_key(symbol):
    return f"{artifact_prefix(symbol)}/provenance.json"


def build_provenance(symbol, content, generated_at=None, universe=None,
                     publication_status="not_published",
                     publication_reason=None, archive_uploaded_at=None,
                     latest_published_at=None):
    """Build the provenance sidecar for one coin's prediction file.

    ``content`` is the :func:`predictions.build_prediction_file`
    result (or an absent-coverage file). ``publication_status`` is
    ``published`` only after the R2 writes for this coin succeed;
    otherwise ``not_published`` with a reason (e.g. ``"local run:
    --publish not given"``).
    """
    rows = content.get("observations", []) if content else []
    # Live: a value row whose target hour has not passed at the
    # generation time. Historical: a value row whose target hour
    # has passed — retained, never retracted. No null-value rows
    # are ever published by this adapter.
    generated_ms = None
    if generated_at:
        generated_ms = oc.instant_ms(generated_at, "generated_at")
    live = []
    historical = []
    for r in rows:
        if r.get("value") is None:
            continue
        target_end = r["value"].get("target_end")
        if generated_ms is not None and target_end is not None:
            if oc.instant_ms(target_end, "target_end") <= generated_ms:
                historical.append(r)
                continue
        live.append(r)
    latest = [r for r in rows if r.get("value") is not None]
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
        "as_of": content.get("generated_at") if content else None,
        "universe": universe,
        "files": {
            "predictions": {
                "key": f"{artifact_prefix(symbol)}.json",
                "source": "crypto_predictions",
                "observations": len(rows),
                "live": len(live),
                "historical": len(historical),
                "coverage": content.get("coverage") if content else "absent",
                "reason": content.get("reason") if content else None,
            },
        },
        "coverage": {
            "live_predictions": len(live),
            "historical": len(historical),
            "target_start": latest[-1]["value"]["target_start"] if latest else None,
            "target_end": latest[-1]["value"]["target_end"] if latest else None,
        },
        "estimator": {
            "module": "market_data.crypto_predictions.estimator",
            "signals": ["last-hour momentum", "24h move", "7d move",
                        "hourly volatility", "last-hour range",
                        "BTC correlation"],
            "note": "Estimates only. Origin synthetic; a prediction is "
                    "never a recorded price and is retained as "
                    "historical evidence when its target hour passes.",
        },
        "notes": [
            "Crypto trades 24/7: buckets align to 00:00 UTC, not an "
            "equity session open. There is no session filter.",
            "Every prediction is a One Clock observation with origin "
            "\"synthetic\", session \"continuous\", eventAt at the "
            "target hour's start. Live prices and future estimates "
            "stay in separate cohorts.",
            "CoinGecko-labelled coins have no 1h base: CoinGecko "
            "free OHLC has no 1m/1h series, so they have no "
            "prediction by design and are labelled absent, never "
            "interpolated from daily bars.",
        ],
    }
