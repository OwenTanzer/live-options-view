"""``snapshot_provenance.v1`` sidecar for one coin's predictions.

Mirrors the candle cache's sidecar shape (see
``market_data/crypto_candle_cache/provenance.py``): ``constructed_at``,
``archive_uploaded_at``, ``latest_published_at``, ``publication.status``
(``published``, or ``not_published`` with a reason), the source label,
the as-of time and the universe. One sidecar per coin.

Coverage is stated honestly:

- ``"current"`` — the file holds a live estimate for the next hour.
- ``"expired"`` — every published estimate has been retracted.
- ``"absent"`` — no prediction file, with a reason. CoinGecko-labelled
  coins have no 1h base at all (CoinGecko free OHLC has no 1m/1h
  series), so they have no estimate by design, not by failure. A coin
  whose 1h file holds fewer than 2 completed bars is likewise absent.

A missing sidecar means unknown provenance.
"""

from __future__ import annotations

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
    # Effective live: a value row superseded by a revision-2
    # retraction of the same id is not live.
    retracted_ids = {r.get("id") for r in rows if r.get("value") is None}
    live = [r for r in rows if r.get("value") is not None
            and r.get("id") not in retracted_ids]
    retracted = [r for r in rows if r.get("value") is None]
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
                "retracted": len(retracted),
                "coverage": content.get("coverage") if content else "absent",
                "reason": content.get("reason") if content else None,
            },
        },
        "coverage": {
            "live_predictions": len(live),
            "retracted": len(retracted),
            "target_start": live[-1]["value"]["target_start"] if live else None,
            "target_end": live[-1]["value"]["target_end"] if live else None,
        },
        "estimator": {
            "module": "market_data.crypto_predictions.estimator",
            "signals": ["last-hour momentum", "24h move", "7d move",
                        "hourly volatility", "last-hour range",
                        "BTC correlation"],
            "note": "Estimates only. Origin synthetic; a prediction is "
                    "never a recorded price and is retracted when its "
                    "target hour passes.",
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
