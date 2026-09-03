#!/usr/bin/env python3
"""One-shot, read-only MOO-144 Tradier option Time & Sale probe.

Captures a narrow near-the-money QQQ 0DTE universe for 30 minutes, preserves
every payload as gzip-compressed NDJSON, summarizes field coverage and sequence
quality, and uploads the run artifacts to an isolated R2 prefix.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import signal
import sys
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import boto3
import requests


API = "https://api.tradier.com/v1"
STREAM = "https://stream.tradier.com/v1/markets/events"
ET = ZoneInfo("America/New_York")
STOP = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def on_stop(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def normalize(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    return [value] if isinstance(value, dict) else []


class Tradier:
    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        response = self.session.get(f"{API}{path}", params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def post(self, path: str) -> dict[str, Any]:
        response = self.session.post(f"{API}{path}", timeout=30)
        response.raise_for_status()
        return response.json()


def select_symbols(client: Tradier, strike_count: int) -> tuple[list[str], dict[str, Any]]:
    today = datetime.now(ET).date().isoformat()
    clock = client.get("/markets/clock").get("clock") or {}
    if clock.get("state") != "open" and os.getenv("MOO144_ALLOW_CLOSED") != "1":
        raise RuntimeError(f"Tradier market clock is not open: {clock.get('state')!r}")

    quote_payload = client.get("/markets/quotes", symbols="QQQ")
    quotes = normalize((quote_payload.get("quotes") or {}).get("quote"))
    if not quotes:
        raise RuntimeError("Tradier returned no QQQ quote")
    spot = float(quotes[0].get("last") or quotes[0].get("bid") or quotes[0].get("ask"))

    expiration_payload = client.get("/markets/options/expirations", symbol="QQQ", includeAllRoots="true")
    expirations = (expiration_payload.get("expirations") or {}).get("date") or []
    if isinstance(expirations, str):
        expirations = [expirations]
    if today not in expirations:
        raise RuntimeError(f"QQQ has no 0DTE expiration for {today}; available head={expirations[:3]}")

    chain_payload = client.get("/markets/options/chains", symbol="QQQ", expiration=today, greeks="true")
    chain = normalize((chain_payload.get("options") or {}).get("option"))
    by_strike: dict[float, dict[str, str]] = defaultdict(dict)
    for option in chain:
        try:
            strike = float(option["strike"])
            kind = str(option["option_type"])
            symbol = str(option["symbol"])
        except (KeyError, TypeError, ValueError):
            continue
        if kind in {"call", "put"}:
            by_strike[strike][kind] = symbol

    nearest = sorted(by_strike, key=lambda strike: abs(strike - spot))[:strike_count]
    selected: list[str] = []
    for strike in sorted(nearest):
        for kind in ("call", "put"):
            symbol = by_strike[strike].get(kind)
            if symbol:
                selected.append(symbol)
    if len(selected) < 4:
        raise RuntimeError(f"Too few usable 0DTE contracts: {len(selected)}")

    universe = {
        "underlying": "QQQ",
        "expiration": today,
        "spot": spot,
        "strikes": sorted(nearest),
        "option_symbols": selected,
        "clock": clock,
    }
    return ["QQQ", *selected], universe


def r2_client() -> tuple[Any, str]:
    required = ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing R2 variables: {', '.join(missing)}")
    account = os.environ["R2_ACCOUNT_ID"]
    client = boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    return client, os.environ["R2_BUCKET_NAME"]


def upload_file(client: Any, bucket: str, path: Path, key: str, content_type: str, encoding: str | None = None) -> None:
    extra = {"ContentType": content_type}
    if encoding:
        extra["ContentEncoding"] = encoding
    client.upload_file(str(path), bucket, key, ExtraArgs=extra)


def main() -> int:
    token = os.getenv("TRADIER_TOKEN")
    if not token:
        raise RuntimeError("TRADIER_TOKEN is required")

    run_date = os.getenv("MOO144_RUN_DATE")
    et_date = datetime.now(ET).date().isoformat()
    if run_date and run_date != et_date:
        print(json.dumps({"event": "skip", "reason": "run_date_mismatch", "expected": run_date, "actual": et_date}), flush=True)
        return 0

    duration = int(os.getenv("MOO144_DURATION_SECONDS", "1800"))
    strike_count = int(os.getenv("MOO144_STRIKE_COUNT", "8"))
    if not 60 <= duration <= 3600:
        raise RuntimeError("MOO144_DURATION_SECONDS must be between 60 and 3600")
    if not 2 <= strike_count <= 20:
        raise RuntimeError("MOO144_STRIKE_COUNT must be between 2 and 20")

    run_id = f"{et_date}-{uuid.uuid4().hex[:10]}"
    prefix = f"moo144/tradier/{et_date}/{run_id}"
    started_at = utc_now()
    print(json.dumps({"event": "probe_start", "run_id": run_id, "duration_seconds": duration}), flush=True)

    client = Tradier(token)
    symbols, universe = select_symbols(client, strike_count)
    print(json.dumps({"event": "universe", "run_id": run_id, **universe}), flush=True)

    r2, bucket = r2_client()
    deadline = time.monotonic() + duration
    counts: Counter[str] = Counter()
    field_present: Counter[str] = Counter()
    field_total: Counter[str] = Counter()
    timesale_by_symbol: Counter[str] = Counter()
    sequence_gaps: Counter[str] = Counter()
    sequence_out_of_order: Counter[str] = Counter()
    last_sequence: dict[str, int] = {}
    seen: set[tuple[Any, ...]] = set()
    reconnects = 0
    malformed = 0
    last_heartbeat = time.monotonic()
    expected_fields = ("symbol", "exch", "bid", "ask", "last", "size", "date", "seq", "flag", "cancel", "correction", "session")

    with tempfile.TemporaryDirectory(prefix="moo144-") as tmp:
        tmpdir = Path(tmp)
        raw_path = tmpdir / "tradier-events.ndjson.gz"
        try:
            with gzip.open(raw_path, "wt", encoding="utf-8") as raw:
                while not STOP and time.monotonic() < deadline:
                    session_data = client.post("/markets/events/session")
                    stream = session_data.get("stream") or {}
                    session_id = stream.get("sessionid")
                    if not session_id:
                        raise RuntimeError("Tradier did not return a market stream session id")
                    if reconnects:
                        raw.write(json.dumps({"type": "gap", "reason": "stream_reconnect", "receipt_timestamp": utc_now(), "reconnect": reconnects}) + "\n")
                    payload = {
                        "symbols": ",".join(symbols),
                        "sessionid": session_id,
                        "filter": "quote,timesale",
                        "linebreak": "true",
                        "validOnly": "false",
                        "advancedDetails": "true",
                    }
                    try:
                        with client.session.get(STREAM, params=payload, stream=True, timeout=(15, 90)) as response:
                            response.raise_for_status()
                            for line in response.iter_lines(decode_unicode=True):
                                if STOP or time.monotonic() >= deadline:
                                    break
                                if not line:
                                    continue
                                receipt = utc_now()
                                try:
                                    event = json.loads(line)
                                except json.JSONDecodeError:
                                    malformed += 1
                                    raw.write(json.dumps({"type": "malformed", "receipt_timestamp": receipt, "raw": line}) + "\n")
                                    continue
                                event["collector_receipt_timestamp"] = receipt
                                event["provider"] = "tradier"
                                event_type = str(event.get("type", "unknown"))
                                counts[event_type] += 1
                                if event_type == "timesale":
                                    symbol = str(event.get("symbol", "unknown"))
                                    timesale_by_symbol[symbol] += 1
                                    for field in expected_fields:
                                        field_total[field] += 1
                                        if field in event and event[field] not in (None, ""):
                                            field_present[field] += 1
                                    dedup_key = tuple(event.get(field) for field in ("symbol", "date", "seq", "flag", "cancel", "correction"))
                                    event["duplicate_in_run"] = dedup_key in seen
                                    seen.add(dedup_key)
                                    try:
                                        sequence = int(event["seq"])
                                        previous = last_sequence.get(symbol)
                                        if previous is not None:
                                            if sequence < previous:
                                                sequence_out_of_order[symbol] += 1
                                            elif sequence > previous + 1:
                                                sequence_gaps[symbol] += sequence - previous - 1
                                        last_sequence[symbol] = max(sequence, previous or sequence)
                                    except (KeyError, TypeError, ValueError):
                                        pass
                                raw.write(json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n")
                                if time.monotonic() - last_heartbeat >= 300:
                                    print(json.dumps({"event": "heartbeat", "run_id": run_id, "counts": counts}), flush=True)
                                    last_heartbeat = time.monotonic()
                    except (requests.RequestException, OSError) as exc:
                        reconnects += 1
                        print(json.dumps({"event": "stream_reconnect", "run_id": run_id, "attempt": reconnects, "error": type(exc).__name__}), flush=True)
                        time.sleep(min(5 * reconnects, 30))

            digest = hashlib.sha256(raw_path.read_bytes()).hexdigest()
            finished_at = utc_now()
            summary = {
                "schema_version": 1,
                "run_id": run_id,
                "provider": "tradier",
                "started_at": started_at,
                "finished_at": finished_at,
                "requested_duration_seconds": duration,
                "stopped_by_signal": STOP,
                "universe": universe,
                "event_counts": dict(counts),
                "timesale_counts_by_symbol": dict(timesale_by_symbol),
                "timesale_expected_field_population": {
                    field: {"present": field_present[field], "total": field_total[field]}
                    for field in expected_fields
                },
                "sequence_gap_count_by_symbol": dict(sequence_gaps),
                "sequence_out_of_order_count_by_symbol": dict(sequence_out_of_order),
                "unique_timesale_keys": len(seen),
                "reconnects": reconnects,
                "malformed_payloads": malformed,
                "limitations": [
                    "Tradier timesale exposes at-event bid/ask but no separate quote timestamp, so quote age is not directly measurable.",
                    "Provider flags can identify conditions, but customer identity, opening/closing status, and multi-leg grouping are not inferred.",
                    "This is a narrow near-ATM capability sample, not a production collector implementation.",
                ],
                "raw_artifact": {
                    "key": f"{prefix}/tradier-events.ndjson.gz",
                    "sha256": digest,
                    "bytes": raw_path.stat().st_size,
                },
            }
            summary_path = tmpdir / "summary.json"
            summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            manifest = {
                "schema_version": 1,
                "issue": "MOO-144",
                "run_id": run_id,
                "prefix": prefix,
                "artifacts": [
                    summary["raw_artifact"],
                    {"key": f"{prefix}/summary.json", "bytes": summary_path.stat().st_size},
                ],
            }
            manifest_path = tmpdir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            upload_file(r2, bucket, raw_path, summary["raw_artifact"]["key"], "application/x-ndjson", "gzip")
            upload_file(r2, bucket, summary_path, f"{prefix}/summary.json", "application/json")
            upload_file(r2, bucket, manifest_path, f"{prefix}/manifest.json", "application/json")
            print(json.dumps({"event": "probe_complete", "run_id": run_id, "prefix": prefix, "summary": summary}), flush=True)
            return 0 if counts["timesale"] > 0 else 2
        except Exception:
            if raw_path.exists() and raw_path.stat().st_size:
                try:
                    partial_key = f"{prefix}/partial-tradier-events.ndjson.gz"
                    upload_file(r2, bucket, raw_path, partial_key, "application/x-ndjson", "gzip")
                    print(json.dumps({"event": "partial_upload", "run_id": run_id, "key": partial_key}), flush=True)
                except Exception as upload_error:
                    print(json.dumps({"event": "partial_upload_failed", "run_id": run_id, "error": type(upload_error).__name__}), flush=True)
