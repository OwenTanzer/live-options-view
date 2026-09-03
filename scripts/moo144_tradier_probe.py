#!/usr/bin/env python3
"""One-shot, read-only MOO-144 Tradier option Time & Sale probe.

Captures a narrow near-the-money QQQ 0DTE universe, writes normalized provider
payloads to immutable gzip NDJSON checkpoints, and uploads verified artifacts
to an isolated R2 prefix. It never accesses account or order endpoints.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import random
import signal
import statistics
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import boto3
import requests


API = "https://api.tradier.com/v1"
STREAM = "https://stream.tradier.com/v1/markets/events"
ET = ZoneInfo("America/New_York")
STOP = False
EXPECTED_TIMESALE_FIELDS = (
    "symbol", "exch", "bid", "ask", "last", "size", "date", "seq",
    "flag", "cancel", "correction", "session",
)


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


def parse_epoch_ms(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def validate_run_window(
    run_date: str | None,
    clock: dict[str, Any],
    duration_seconds: int,
    now_et: datetime,
) -> str:
    if not run_date:
        raise RuntimeError("MOO144_RUN_DATE is required for this one-shot worker")
    try:
        expected = datetime.strptime(run_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise RuntimeError("MOO144_RUN_DATE must use YYYY-MM-DD") from exc
    if expected != now_et.date():
        raise RuntimeError(f"Run-date mismatch: expected {expected}, actual {now_et.date()}")
    if clock.get("date") and str(clock["date"]) != run_date:
        raise RuntimeError(f"Tradier clock date mismatch: {clock.get('date')!r}")
    if clock.get("state") != "open":
        raise RuntimeError(f"Tradier market clock is not open: {clock.get('state')!r}")

    next_change = str(clock.get("next_change") or "")
    try:
        hour, minute = (int(piece) for piece in next_change.split(":", 1))
        close_et = now_et.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except (TypeError, ValueError):
        raise RuntimeError(f"Tradier clock supplied no usable next_change: {next_change!r}")
    if close_et <= now_et:
        raise RuntimeError(f"Tradier next_change is not in the future: {next_change!r}")
    if now_et + timedelta(seconds=duration_seconds + 60) > close_et:
        raise RuntimeError("Requested capture plus finalization buffer does not fit before market close")
    return run_date


class Tradier:
    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        })

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        response = self.session.get(f"{API}{path}", params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def create_market_session(self) -> str:
        response = self.session.post(f"{API}/markets/events/session", data={}, timeout=30)
        response.raise_for_status()
        stream = (response.json().get("stream") or {})
        session_id = stream.get("sessionid")
        if not session_id:
            raise RuntimeError("Tradier did not return a market stream session id")
        return str(session_id)


def select_symbols(
    client: Tradier,
    strike_count: int,
    duration_seconds: int,
    run_date: str | None,
    now_et: datetime | None = None,
) -> tuple[list[str], dict[str, Any]]:
    now_et = now_et or datetime.now(ET)
    clock = client.get("/markets/clock").get("clock") or {}
    trade_date = validate_run_window(run_date, clock, duration_seconds, now_et)

    quote_payload = client.get("/markets/quotes", symbols="QQQ")
    quotes = normalize((quote_payload.get("quotes") or {}).get("quote"))
    if not quotes:
        raise RuntimeError("Tradier returned no QQQ quote")
    quote = quotes[0]
    price_values = [quote.get("last"), quote.get("bid"), quote.get("ask")]
    try:
        spot = next(float(value) for value in price_values if value not in (None, "") and float(value) > 0)
    except StopIteration as exc:
        raise RuntimeError("Tradier returned no positive QQQ reference price") from exc

    expiration_payload = client.get(
        "/markets/options/expirations", symbol="QQQ", includeAllRoots="true"
    )
    expirations = (expiration_payload.get("expirations") or {}).get("date") or []
    if isinstance(expirations, str):
        expirations = [expirations]
    if trade_date not in expirations:
        raise RuntimeError(
            f"QQQ has no 0DTE expiration for {trade_date}; available head={expirations[:3]}"
        )

    chain_payload = client.get(
        "/markets/options/chains", symbol="QQQ", expiration=trade_date, greeks="true"
    )
    chain = normalize((chain_payload.get("options") or {}).get("option"))
    by_strike: dict[float, dict[str, str]] = defaultdict(dict)
    metadata: dict[str, dict[str, Any]] = {}
    for option in chain:
        try:
            strike = float(option["strike"])
            kind = str(option["option_type"])
            symbol = str(option["symbol"])
        except (KeyError, TypeError, ValueError):
            continue
        if kind in {"call", "put"}:
            by_strike[strike][kind] = symbol
            metadata[symbol] = {
                "expiration": trade_date,
                "strike": strike,
                "option_type": kind,
            }

    complete_strikes = [
        strike
        for strike, legs in by_strike.items()
        if {"call", "put"}.issubset(legs)
    ]
    nearest = sorted(
        complete_strikes, key=lambda strike: abs(strike - spot)
    )[:strike_count]
    selected = [
        by_strike[strike][kind]
        for strike in sorted(nearest)
        for kind in ("call", "put")
        if kind in by_strike[strike]
    ]
    if len(selected) < 4:
        raise RuntimeError(f"Too few usable 0DTE contracts: {len(selected)}")

    universe = {
        "underlying": "QQQ",
        "expiration": trade_date,
        "spot": spot,
        "strikes": sorted(nearest),
        "option_symbols": selected,
        "option_metadata": {symbol: metadata[symbol] for symbol in selected},
        "clock": clock,
    }
    return ["QQQ", *selected], universe


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def r2_client() -> tuple[Any, str]:
    required = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME")
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


def put_bytes_verified(
    client: Any,
    bucket: str,
    key: str,
    body: bytes,
    content_type: str,
    content_encoding: str | None = None,
) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "Bucket": bucket, "Key": key, "Body": body, "ContentType": content_type,
    }
    if content_encoding:
        arguments["ContentEncoding"] = content_encoding
    client.put_object(**arguments)
    head = client.head_object(Bucket=bucket, Key=key)
    if int(head.get("ContentLength", -1)) != len(body):
        raise RuntimeError(f"R2 verification failed for {key}")
    return {"key": key, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}


def claim_run_once(
    client: Any,
    bucket: str,
    run_date: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    key = f"moo144/tradier/{run_date}/run-claim.json"
    body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            IfNoneMatch="*",
        )
    except Exception as exc:
        response = getattr(exc, "response", {}) or {}
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        code = (response.get("Error") or {}).get("Code")
        if status == 412 or code in {"PreconditionFailed", "412"}:
            raise RuntimeError(
                f"MOO-144 capture already claimed for {run_date}; refusing a second launch"
            ) from exc
        raise
    head = client.head_object(Bucket=bucket, Key=key)
    if int(head.get("ContentLength", -1)) != len(body):
        raise RuntimeError(f"R2 verification failed for {key}")
    return {"key": key, "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}


def upload_file_verified(
    client: Any,
    bucket: str,
    path: Path,
    key: str,
    content_type: str,
    content_encoding: str | None = None,
) -> dict[str, Any]:
    size = path.stat().st_size
    extra: dict[str, str] = {"ContentType": content_type}
    if content_encoding:
        extra["ContentEncoding"] = content_encoding
    client.upload_file(str(path), bucket, key, ExtraArgs=extra)
    head = client.head_object(Bucket=bucket, Key=key)
    if int(head.get("ContentLength", -1)) != size:
        raise RuntimeError(f"R2 verification failed for {key}")
    return {"key": key, "bytes": size, "sha256": sha256_file(path)}


class Stats:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.timesale_by_symbol: Counter[str] = Counter()
        self.field_key_present: Counter[str] = Counter()
        self.field_non_null: Counter[str] = Counter()
        self.field_non_empty: Counter[str] = Counter()
        self.field_total: Counter[str] = Counter()
        self.flags: Counter[str] = Counter()
        self.sessions: Counter[str] = Counter()
        self.cancel_count = 0
        self.correction_count = 0
        self.duplicate_count = 0
        self.malformed = 0
        self.last_sequence: dict[str, int] = {}
        self.sequence_discontinuities: Counter[str] = Counter()
        self.sequence_out_of_order: Counter[str] = Counter()
        self.seen: set[tuple[Any, ...]] = set()
        self.quote_timestamps: dict[str, int] = {}
        self.quote_ages_ms: list[int] = []

    def observe(self, event: dict[str, Any]) -> dict[str, Any]:
        event_type = str(event.get("type", "unknown"))
        self.counts[event_type] += 1
        symbol = str(event.get("symbol", "unknown"))
        if event_type == "quote":
            candidates = [
                value for value in (
                    parse_epoch_ms(event.get("biddate")),
                    parse_epoch_ms(event.get("askdate")),
                )
                if value is not None
            ]
            if candidates:
                self.quote_timestamps[symbol] = max(candidates)
            return event
        if event_type != "timesale":
            return event

        self.timesale_by_symbol[symbol] += 1
        for field in EXPECTED_TIMESALE_FIELDS:
            self.field_total[field] += 1
            if field in event:
                self.field_key_present[field] += 1
            if event.get(field) is not None:
                self.field_non_null[field] += 1
            if event.get(field) not in (None, ""):
                self.field_non_empty[field] += 1
        self.flags[str(event.get("flag") or "<empty>")] += 1
        self.sessions[str(event.get("session") or "<missing>")] += 1
        self.cancel_count += int(bool(event.get("cancel")))
        self.correction_count += int(bool(event.get("correction")))

        dedup_key = tuple(
            event.get(field)
            for field in ("symbol", "date", "seq", "flag", "cancel", "correction")
        )
        duplicate = dedup_key in self.seen
        event["duplicate_in_run"] = duplicate
        self.duplicate_count += int(duplicate)
        self.seen.add(dedup_key)

        try:
            sequence = int(event["seq"])
            previous = self.last_sequence.get(symbol)
            if previous is not None:
                if sequence < previous:
                    self.sequence_out_of_order[symbol] += 1
                elif sequence > previous + 1:
                    self.sequence_discontinuities[symbol] += 1
            self.last_sequence[symbol] = max(sequence, previous if previous is not None else sequence)
        except (KeyError, TypeError, ValueError):
            pass

        trade_ms = parse_epoch_ms(event.get("date"))
        quote_ms = self.quote_timestamps.get(symbol)
        if trade_ms is not None and quote_ms is not None and quote_ms <= trade_ms:
            age = trade_ms - quote_ms
            self.quote_ages_ms.append(age)
            event["preceding_quote_age_ms"] = age
        return event

    def summary(self) -> dict[str, Any]:
        ages = sorted(self.quote_ages_ms)
        age_summary: dict[str, Any] = {"count": len(ages)}
        if ages:
            age_summary.update({
                "min": ages[0],
                "median": statistics.median(ages),
                "p95": ages[min(len(ages) - 1, int(0.95 * (len(ages) - 1)))],
                "max": ages[-1],
            })
        return {
            "event_counts": dict(self.counts),
            "timesale_counts_by_symbol": dict(self.timesale_by_symbol),
            "timesale_field_population": {
                field: {
                    "total": self.field_total[field],
                    "key_present": self.field_key_present[field],
                    "non_null": self.field_non_null[field],
                    "non_empty": self.field_non_empty[field],
                }
                for field in EXPECTED_TIMESALE_FIELDS
            },
            "flag_frequencies": dict(self.flags),
            "session_frequencies": dict(self.sessions),
            "cancel_count": self.cancel_count,
            "correction_count": self.correction_count,
            "duplicate_count": self.duplicate_count,
            "unique_timesale_event_keys": len(self.seen),
            "sequence_discontinuities_by_symbol": dict(self.sequence_discontinuities),
            "sequence_out_of_order_by_symbol": dict(self.sequence_out_of_order),
            "preceding_quote_age_ms": age_summary,
            "malformed_payloads": self.malformed,
        }


class SegmentWriter:
    def __init__(
        self,
        directory: Path,
        prefix: str,
        r2: Any,
        bucket: str,
        checkpoint_seconds: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.directory = directory
        self.prefix = prefix
        self.r2 = r2
        self.bucket = bucket
        self.checkpoint_seconds = checkpoint_seconds
        self.clock = clock
        self.index = 0
        self.records = 0
        self.opened_at = clock()
        self.path: Path | None = None
        self.handle: Any = None
        self.artifacts: list[dict[str, Any]] = []
        self._open()

    def _open(self) -> None:
        self.path = self.directory / f"normalized-events-part-{self.index:03d}.ndjson.gz"
        self.handle = gzip.open(self.path, "wt", encoding="utf-8")
        self.records = 0
        self.opened_at = self.clock()

    def write(self, event: dict[str, Any]) -> None:
        self.handle.write(json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n")
        self.records += 1
        if self.clock() - self.opened_at >= self.checkpoint_seconds:
            self.rotate()

    def rotate(self, final: bool = False) -> None:
        if self.handle is None or self.path is None:
            return
        self.handle.close()
        if self.records:
            key = f"{self.prefix}/{self.path.name}"
            upload_started = self.clock()
            artifact = upload_file_verified(
                self.r2, self.bucket, self.path, key, "application/x-ndjson", "gzip"
            )
            artifact["upload_seconds"] = round(self.clock() - upload_started, 3)
            artifact["records"] = self.records
            self.artifacts.append(artifact)
            self.path.unlink()
        elif self.path.exists():
            self.path.unlink()
        self.handle = None
        self.path = None
        if not final:
            self.index += 1
            self._open()

    def close(self) -> None:
        self.rotate(final=True)


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, requests.HTTPError):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if status in (401, 403):
            return False
        if isinstance(status, int) and 400 <= status < 500 and status != 429:
            return False
    return isinstance(exc, (requests.RequestException, OSError))


def stream_payload(symbols: list[str], session_id: str) -> dict[str, str]:
    return {
        "symbols": ",".join(symbols),
        "sessionid": session_id,
        "filter": "quote,timesale",
        "linebreak": "true",
        "validOnly": "false",
        "advancedDetails": "true",
    }


def capture(
    client: Tradier,
    symbols: list[str],
    writer: SegmentWriter,
    stats: Stats,
    duration_seconds: int,
    max_consecutive_reconnects: int,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    deadline = monotonic() + duration_seconds
    reconnects = 0
    consecutive_failures = 0
    last_heartbeat = monotonic()

    while not STOP and monotonic() < deadline:
        received = False
        try:
            session_id = client.create_market_session()
            payload = stream_payload(symbols, session_id)
            with client.session.get(
                STREAM, params=payload, stream=True, timeout=(15, 10)
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines(decode_unicode=True):
                    if STOP or monotonic() >= deadline:
                        break
                    if not line:
                        continue
                    received = True
                    receipt = utc_now()
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError("non-object event")
                    except (json.JSONDecodeError, ValueError, TypeError):
                        stats.malformed += 1
                        raw = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
                        writer.write({
                            "type": "malformed",
                            "collector_receipt_timestamp": receipt,
                            "provider_payload": raw,
                        })
                        continue
                    event["collector_receipt_timestamp"] = receipt
                    event["provider"] = "tradier"
                    writer.write(stats.observe(event))
                    if monotonic() - last_heartbeat >= 300:
                        print(json.dumps({
                            "event": "heartbeat",
                            "event_counts": dict(stats.counts),
                            "uploaded_parts": len(writer.artifacts),
                        }), flush=True)
                        last_heartbeat = monotonic()
                if STOP or monotonic() >= deadline:
                    break
                raise requests.ConnectionError("Tradier stream ended cleanly before deadline")
        except (requests.RequestException, OSError) as exc:
            if not is_retryable(exc):
                raise
            reconnects += 1
            consecutive_failures = 1 if received else consecutive_failures + 1
            writer.write({
                "type": "gap",
                "reason": "stream_reconnect",
                "receipt_timestamp": utc_now(),
                "reconnect": reconnects,
                "error_type": type(exc).__name__,
            })
            if consecutive_failures > max_consecutive_reconnects:
                raise RuntimeError(
                    f"Tradier stream exceeded {max_consecutive_reconnects} consecutive reconnects"
                ) from exc
            delay = min(2 ** (consecutive_failures - 1), 15) + random.uniform(0, 1)
            remaining = min(delay, max(0.0, deadline - monotonic()))
            while remaining > 0 and not STOP:
                interval = min(0.5, remaining)
                sleeper(interval)
                remaining -= interval
    return reconnects


def json_artifact(
    r2: Any,
    bucket: str,
    key: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    body = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    return put_bytes_verified(r2, bucket, key, body, "application/json")


def main() -> int:
    token = os.getenv("TRADIER_TOKEN")
    if not token:
        raise RuntimeError("TRADIER_TOKEN is required")
    run_date = os.getenv("MOO144_RUN_DATE")
    duration = int(os.getenv("MOO144_DURATION_SECONDS", "1800"))
    strike_count = int(os.getenv("MOO144_STRIKE_COUNT", "8"))
    checkpoint_seconds = int(os.getenv("MOO144_CHECKPOINT_SECONDS", "180"))
    max_reconnects = int(os.getenv("MOO144_MAX_CONSECUTIVE_RECONNECTS", "5"))
    if not 60 <= duration <= 3600:
        raise RuntimeError("MOO144_DURATION_SECONDS must be between 60 and 3600")
    if not 2 <= strike_count <= 20:
        raise RuntimeError("MOO144_STRIKE_COUNT must be between 2 and 20")
    if not 30 <= checkpoint_seconds <= 300:
        raise RuntimeError("MOO144_CHECKPOINT_SECONDS must be between 30 and 300")
    if not 1 <= max_reconnects <= 10:
        raise RuntimeError("MOO144_MAX_CONSECUTIVE_RECONNECTS must be between 1 and 10")

    et_date = datetime.now(ET).date().isoformat()
    run_id = f"{et_date}-{uuid.uuid4().hex[:10]}"
    prefix = f"moo144/tradier/{et_date}/{run_id}"
    started_at = utc_now()
    print(json.dumps({
        "event": "probe_start", "run_id": run_id, "duration_seconds": duration,
    }), flush=True)

    client = Tradier(token)
    symbols, universe = select_symbols(client, strike_count, duration, run_date)
    r2, bucket = r2_client()
    claim = claim_run_once(
        r2,
        bucket,
        str(run_date),
        {
            "schema_version": 1,
            "issue": "MOO-144",
            "run_date": run_date,
            "claimed_at": started_at,
            "run_id": run_id,
        },
    )
    start_payload = {
        "schema_version": 2,
        "issue": "MOO-144",
        "run_id": run_id,
        "started_at": started_at,
        "requested_duration_seconds": duration,
        "universe": universe,
        "claim": claim,
    }
    preflight = json_artifact(r2, bucket, f"{prefix}/run-started.json", start_payload)
    print(json.dumps({
        "event": "r2_preflight_pass", "run_id": run_id, "key": preflight["key"],
    }), flush=True)

    stats = Stats()
    with tempfile.TemporaryDirectory(prefix="moo144-") as tmp:
        writer = SegmentWriter(
            Path(tmp), prefix, r2, bucket, checkpoint_seconds
        )
        try:
            reconnects = capture(
                client, symbols, writer, stats, duration, max_reconnects
            )
        finally:
            writer.close()

        finished_at = utc_now()
        summary = {
            "schema_version": 2,
            "run_id": run_id,
            "provider": "tradier",
            "started_at": started_at,
            "finished_at": finished_at,
            "requested_duration_seconds": duration,
            "stopped_by_signal": STOP,
            "universe": universe,
            "reconnects": reconnects,
            **stats.summary(),
            "limitations": [
                "The preserved payload is normalized/enriched JSON, not byte-exact wire data.",
                "Sequence discontinuities are diagnostic only until Tradier sequence scope is established.",
                "Customer identity, opening/closing status, and multi-leg grouping are not inferred.",
                "This is a narrow near-ATM capability sample, not a production collector.",
            ],
            "event_parts": writer.artifacts,
        }
        summary_meta = json_artifact(
            r2, bucket, f"{prefix}/summary.json", summary
        )
        manifest = {
            "schema_version": 2,
            "issue": "MOO-144",
            "run_id": run_id,
            "prefix": prefix,
            "artifacts": [preflight, *writer.artifacts, summary_meta],
            "run_claim": claim,
        }
        manifest_meta = json_artifact(
            r2, bucket, f"{prefix}/manifest.json", manifest
        )
        print(json.dumps({
            "event": "probe_complete",
            "run_id": run_id,
            "prefix": prefix,
            "manifest": manifest_meta,
            "event_counts": dict(stats.counts),
        }), flush=True)
        return 0 if stats.counts["timesale"] > 0 else 2


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({
            "event": "probe_failed",
            "error": type(exc).__name__,
            "message": str(exc),
        }), flush=True)
        raise
