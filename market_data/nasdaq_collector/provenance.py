"""``snapshot_provenance.v1`` sidecar for the NASDAQ-100 snapshot.

Mirrors the live-market collector's sidecar shape (see
``market_data/live_market/README.md``): ``constructed_at``,
``archive_uploaded_at``, ``latest_published_at``, ``publication.status``
(``published``, or ``not_published`` with a reason), per-stock
``price_basis``, ``provider_observed_at``, ``freshness`` and the source
label, plus the snapshot's as-of time. A missing sidecar means unknown
provenance; a stale or source-unavailable stock is labelled, never filled
with a substituted price.
"""

from __future__ import annotations

SCHEMA = "snapshot_provenance.v1"
ARTIFACT_KEY = "nasdaq/latest.json"
SIDECAR_KEY = "nasdaq/latest.provenance.json"


def build_provenance(snapshot, publication_status="not_published",
                     publication_reason=None, archive_uploaded_at=None,
                     latest_published_at=None):
    """Build the provenance sidecar for a snapshot dict.

    ``publication_status`` is ``published`` only after the R2 write of
    ``nasdaq/latest.json`` succeeds; otherwise ``not_published`` with a
    reason (e.g. ``"local run: --publish not given"``).
    """
    stocks = {}
    for row in snapshot.get("stocks", []):
        stocks[row["symbol"]] = {
            "price_basis": row.get("price_basis"),
            "provider_observed_at": row.get("provider_observed_at"),
            "freshness": row.get("freshness"),
            "status": row.get("status"),
        }
    index = snapshot.get("index") or {}
    return {
        "schema": SCHEMA,
        "dataset": "nasdaq_snapshot",
        "artifact": ARTIFACT_KEY,
        "constructed_at": snapshot.get("generated_at"),
        "archive_uploaded_at": archive_uploaded_at,
        "latest_published_at": latest_published_at,
        "publication": {"status": publication_status,
                        "reason": publication_reason},
        "source": snapshot.get("sources", {}).get("prices"),
        "as_of": snapshot.get("as_of"),
        "universe": snapshot.get("universe"),
        "index_changes": snapshot.get("index_changes"),
        "index": {
            "symbol": index.get("symbol"),
            "price_basis": index.get("price_basis"),
            "provider_observed_at": index.get("provider_observed_at"),
            "freshness": index.get("freshness"),
            "status": index.get("status"),
        },
        "stocks": stocks,
        "coverage": snapshot.get("coverage"),
    }
