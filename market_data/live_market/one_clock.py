"""Public snapshot projection into the common immutable publication log."""
import math
import json
import time
import threading
from botocore.client import BaseClient

from .publications import PublicationLog, PublicationError, BudgetedS3, snapshot_publication_id
from .config import log

OUTPUTS = {"price": {"spot": "number"}, "skew": {"imbalance": "number", "strikes": "number", "callOI": "number", "putOI": "number"}}
# Instruments registered with the One Clock backend sources. Other collected
# chains (SLV, DRAM) keep the latest/archive path but are not committed here
# until their One Clock source registration lands.
INSTRUMENTS = frozenset({"QQQ", "SPY", "IBIT"})
_publication_client = None
_client_lock = threading.Lock()


def publication_transport(client):
    global _publication_client
    if isinstance(client, BaseClient):
        with _client_lock:
            if _publication_client is None:
                from .prices import make_s3
                _publication_client = make_s3(publication=True)
        client = _publication_client
    return BudgetedS3(client, seconds=20, requests=40)


def project_snapshot(payload):
    symbol, key, timestamp = payload["symbol"], payload["snapshot_key"], payload["timestamp"]
    if symbol not in INSTRUMENTS:
        raise PublicationError("unregistered snapshot instrument")
    market = payload.get("underlying_market", {})
    live = market.get("freshness") == "live"
    try:
        receipt = PublicationLog._time(market.get("spot_ts"))
        observed = PublicationLog._time(timestamp)
        live = live and 0 <= (observed - receipt).total_seconds() <= 150
    except PublicationError:
        live = False
    spot = payload.get("underlying_price")
    finite = lambda n: type(n) in {int, float} and math.isfinite(n)
    price = {"spot": spot} if live and finite(spot) and spot > 0 else None
    skew = None
    for reading in payload.get("market_readings", []):
        if reading.get("reading_id") != "reference_oi_skew":
            continue
        if (live and reading.get("schema_version") == "market_reading.v1" and
                reading.get("symbol") == symbol and reading.get("interpretation", {}).get("kind") == "reference" and
                reading.get("status", {}).get("code") == "ok" and reading.get("freshness", {}).get("status") == "live" and
                reading.get("input", {}).get("snapshot_key") == key and
                reading.get("input", {}).get("snapshot_timestamp") == timestamp):
            values = reading.get("values", {})
            candidate = dict(zip(("imbalance", "strikes", "callOI", "putOI"),
                                 (values.get(k) for k in ("imbalance_ratio", "band_strikes", "near_money_call_oi", "near_money_put_oi"))))
            if all(finite(n) for n in candidate.values()):
                skew = candidate
        break
    base = {"id": key, "revision": 1, "session": payload["date"], "eventAt": timestamp}
    return [{**base, "source": "price", "value": price}, {**base, "source": "skew", "value": skew}]


def publish_snapshot(s3, bucket, payload, csv_bytes):
    started = time.monotonic()
    transport = publication_transport(s3)
    # Unknown numeric cells are explicit nulls in JSON evidence; preserve the
    # original CSV separately. Non-finite optional Greeks must not prevent a
    # valid publication of unrelated, qualified public price/skew readings.
    def json_value(value):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if isinstance(value, dict):
            return {key: json_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [json_value(item) for item in value]
        return value
    evidence_payload = json.dumps(json_value(payload), default=str, allow_nan=False,
                                  sort_keys=True, separators=(",", ":")).encode()
    publisher = PublicationLog(transport, bucket, source="collector-chain", instrument=payload["symbol"],
                               owner="market_data.live_market", outputs=OUTPUTS)
    try:
        result = publisher.publish(publication_id=snapshot_publication_id(payload["snapshot_key"]),
                                   records=project_snapshot(payload),
                                   evidence=[(csv_bytes, "text/csv", "collector.snapshot.csv.v1"),
                                             (evidence_payload, "application/json", "collector.snapshot.payload.v1")],
                                   created_at=payload["timestamp"])
    except Exception:
        log.warning("One Clock publication failed symbol=%s requests=%s elapsed_ms=%s",
                    payload["symbol"], transport.requests, round((time.monotonic()-started)*1000))
        raise
    log.info("One Clock publication committed symbol=%s sequence=%s requests=%s elapsed_ms=%s",
             payload["symbol"], result["sequence"], transport.requests, round((time.monotonic()-started)*1000))
    return result
