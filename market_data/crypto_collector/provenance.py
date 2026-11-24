"""Provenance sidecar for the crypto snapshot (``snapshot_provenance.v1``).

Follows the main collector's sidecar shape in
``market_data/live_market/README.md``: a snapshot's ``constructed_at``,
``archive_uploaded_at``, ``latest_published_at``,
``publication.status`` and per-price ``price_basis`` /
``provider_observed_at`` / ``freshness``. The crypto collector has no
CSV archive, so the sidecar sits next to the latest artifact as
``crypto/latest.provenance.json``; ``archive_uploaded_at`` is null
until/unless a dated archive copy is written.

A missing sidecar means unknown provenance; a sidecar never backfills
a snapshot it was not built from.
"""

from __future__ import annotations

SCHEMA = "snapshot_provenance.v1"
ARTIFACT_KEY = "crypto/latest.json"


def build_provenance(snapshot, *, publication_status, archive_uploaded_at=None,
                     latest_published_at=None):
    """Build the sidecar dict for a snapshot built by :mod:`collector`."""
    coins = {}
    for row in snapshot.get("coins", []):
        coins[row["symbol"]] = {
            "price_basis": row.get("price_basis"),
            "provider_observed_at": row.get("provider_observed_at"),
            "freshness": row.get("freshness"),
            "source": row.get("source"),
            "status": row.get("status"),
        }
    return {
        "schema_version": SCHEMA,
        "artifact": ARTIFACT_KEY,
        "constructed_at": snapshot.get("constructed_at"),
        "archive_uploaded_at": archive_uploaded_at,
        "latest_published_at": latest_published_at,
        "publication": {
            "status": publication_status,
            # "published" when latest.json was written; "unpublished"
            # with a reason when construction failed before publish.
            "reason": None if publication_status == "published" else "not_published",
        },
        "session_model": snapshot.get("session_model"),
        "cadence": snapshot.get("cadence"),
        "universe": snapshot.get("universe"),
        "coins": coins,
        "market": {
            "fear_greed": (snapshot.get("market") or {}).get("fear_greed"),
            "total_market_cap_usd": (snapshot.get("market") or {}).get("total_market_cap_usd"),
            "btc_dominance_pct": (snapshot.get("market") or {}).get("btc_dominance_pct"),
        },
        "pattern_count": len(snapshot.get("patterns", [])),
    }
