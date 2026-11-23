"""Append-only QQQ opening references. One writer uses its existing snapshot loop."""
from __future__ import annotations

import json
import math
from datetime import datetime
from functools import lru_cache

CALIBRATION_KEY = "derived/OIranges.csv"
CALIBRATION_LIMIT = 128 * 1024
PACKET_LIMIT = 512 * 1024
MAX_STRIKES = 201


@lru_cache(maxsize=4)
def opening_calendar(day):
    from .weekly import _weekly_session_bounds
    from .tiers import _load_calendar
    return _weekly_session_bounds(day), max((d for d in _load_calendar() if d < day), default=None)


def _body(obj, limit):
    stream = obj["Body"]
    try:
        raw = stream.read(limit + 1)
    finally:
        stream.close()
    if not raw or len(raw) > limit:
        raise ValueError("opening OI object size")
    return raw


def publish_opening_oi(s3, bucket, *, payload, state, strikes, captured, session_open, previous_session, cursor):
    day = payload["date"]
    if captured < session_open or payload["symbol"] != "QQQ" or payload["expiration"] != day:
        return
    prefix = "intraday/" + day.replace("-", "") + "/opening-oi/v1/"
    if cursor.get("session") != day:
        cursor.clear()
        listing = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=2 * MAX_STRIKES + 1)
        if listing.get("IsTruncated"):
            raise ValueError("opening OI revision bound")
        keys = sorted(item["Key"] for item in listing.get("Contents", []))
        if keys and keys != [prefix + f"{i:04d}.json" for i in range(1, len(keys) + 1)]:
            raise ValueError("opening OI revision continuity")
        prior = json.loads(_body(s3.get_object(Bucket=bucket, Key=keys[-1]), PACKET_LIMIT)) if keys else None
        if prior and (prior.get("schema") != "opening-oi.v1" or prior.get("session") != day or
                      prior.get("settledSession") != previous_session or prior.get("version") != len(keys)):
            raise ValueError("opening OI prior revision identity")
        cursor.update(session=day, prior=prior)
    prior = cursor["prior"]
    if prior and all(leg[s] is not None for leg in prior["legs"] for s in ("call", "put")):
        return
    quote = state.get("QQQ", {})
    opening = prior["openingSpot"] if prior else quote.get("day_open")
    if not prior and (quote.get("day_open_date") != day or not isinstance(opening, (float, int)) or not math.isfinite(opening) or opening <= 0):
        return
    # Use the opening-price window from the session's subscribed contracts.
    # Later spot and subscription ordering never recenter the retained ladder.
    contracts = {s["strike"]: s for s in strikes}
    if prior:
        legs = [leg.copy() for leg in prior["legs"]]
    else:
        from .config import STRIKE_WINDOW
        legs = [{"strike": k, "call": None, "put": None} for k in sorted(contracts) if abs(k-round(opening)) <= STRIKE_WINDOW]
    if not legs or len(legs) > MAX_STRIKES or (len(legs)>1 and not legs[0]["strike"] <= opening <= legs[-1]["strike"]):
        return
    added = False
    for leg in legs:
        contract = contracts.get(leg["strike"])
        if not contract:
            continue
        for side in ("call", "put"):
            if leg[side] is not None:
                continue
            record = state.get(contract[side + "_sym"], {})
            oi = record.get("oi")
            if record.get("oi_for_date") == previous_session and type(oi) is int and oi >= 0:
                leg[side] = oi
                added = True
    if not added:
        return
    if prior:
        calibration = prior["calibration"]
    else:
        obj = s3.get_object(Bucket=bucket, Key=CALIBRATION_KEY)
        raw = _body(obj, CALIBRATION_LIMIT)
        updated = obj.get("LastModified")
        if not obj.get("ETag") or not isinstance(updated, datetime) or updated > session_open:
            return
        calibration = {"etag": obj["ETag"].strip('"'), "uploaded": updated.isoformat(), "csv": raw.decode("utf-8")}
    version = prior["version"] + 1 if prior else 1
    packet = {"schema":"opening-oi.v1","version":version,"instrument":"QQQ","session":day,"settledSession":previous_session,
              "tier":prior["tier"] if prior else payload["tier"],"openingSpot":opening,"openAt":session_open.isoformat(),
              "capturedAt":captured.isoformat(),"snapshotKey":payload["snapshot_key"],"legs":legs,"calibration":calibration}
    data=json.dumps(packet, allow_nan=False, separators=(",", ":")).encode()
    if len(data)>PACKET_LIMIT:
        raise ValueError("opening OI packet size")
    try:
        s3.put_object(Bucket=bucket, Key=prefix+f"{version:04d}.json", Body=data, ContentType="application/json", IfNoneMatch="*")
    except Exception:
        # An uncertain write or another writer requires rereading the winning
        # immutable sequence before computing another version.
        cursor.clear()
        raise
    cursor["prior"] = packet
