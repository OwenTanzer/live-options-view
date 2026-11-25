"""The session read loop.

Consumes the stream from the open to the close, measures reconnect gaps,
discards pre-open events, and stops on lease loss or spool exhaustion. It
records what happened in a ``CaptureResult``; judging that record is
``run.main``'s job.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from contextlib import closing, contextmanager
from datetime import datetime
from typing import Callable

import requests
import websocket

from . import config
from ._probe import STREAM, Tradier, is_retryable, parse_epoch_ms, stream_payload, utc_now
from .config import ET
from .spool import SegmentSpool, SpoolExhausted
from .stats import BoundedStats


@contextmanager
def market_lines(client, symbols, session_id):
    payload = stream_payload(symbols, session_id)
    request = requests.Request("GET", STREAM, params=payload).prepare()
    if len(request.path_url) <= 6000:
        with client.session.get(STREAM, params=payload, stream=True, timeout=(15, 10)) as response:
            response.raise_for_status()
            yield response.iter_lines(chunk_size=1, decode_unicode=True)
        return

    # Large subscriptions travel in a WebSocket frame, outside URL size limits.
    def messages(sock):
        while True:
            frame = sock.recv()
            if not frame:
                raise requests.ConnectionError("Tradier WebSocket closed")
            if isinstance(frame, bytes):
                frame = frame.decode("utf-8", errors="replace")
            for line in frame.splitlines():
                if line.lstrip().startswith('{"error"'):
                    raise requests.ConnectionError("Tradier rejected WebSocket subscription")
                yield line

    try:
        with closing(websocket.create_connection("wss://ws.tradier.com/v1/markets/events", timeout=15)) as sock:
            sock.settimeout(10)
            sock.send(json.dumps({"symbols": symbols, "sessionid": session_id,
                                  "filter": ["quote", "timesale"], "linebreak": True,
                                  "validOnly": False, "advancedDetails": True}))
            yield messages(sock)
    except websocket.WebSocketException as exc:
        raise requests.ConnectionError("Tradier WebSocket transport failure") from exc


class CaptureResult:
    def __init__(self) -> None:
        self.reconnects = 0
        self.gap_seconds = 0.0
        self.stop_reason: str | None = None  # None, "lease_lost", or "spool_exhausted"
        self.stream_connected_at: datetime | None = None
        self.opening_stream_ready_at: datetime | None = None
        self.preopen_events_discarded = 0
        self.excluded_timesales: Counter[str] = Counter()


def capture_session(
    client: Tradier,
    symbols: list[str],
    spool: SegmentSpool,
    stats: BoundedStats,
    session_close: datetime,
    max_consecutive_reconnects: int,
    lease_lost: threading.Event | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    now_et: Callable[[], datetime] = lambda: datetime.now(ET),
    result: CaptureResult | None = None,
    session_open: datetime | None = None,
    recent=None,
) -> CaptureResult:
    """Run the stream-read loop until session close, a lost lease, or an
    exhausted spool.

    ``result`` may be supplied by the caller so it can be shared with a
    concurrently-running health publisher: this function mutates it in
    place (rather than only returning a fresh one at the end) so
    ``reconnects``/``gap_seconds`` are visible to another thread *during*
    a long-running capture, not just after it returns.

    A gap stays open (no ``stream_reconnect_resumed`` is written, and
    ``consecutive_failures`` keeps climbing) across every failed reconnect
    attempt following one ``stream_disconnect`` -- it only closes once an
    actual event is received again, and ``gap_seconds`` accumulates the
    full outage, not just backoff sleep time.
    """
    result = result if result is not None else CaptureResult()
    lease_lost = lease_lost or threading.Event()
    consecutive_failures = 0
    gap_open = False
    gap_started_at: float | None = None
    # The boundary a gap starts from is the last moment the stream was known
    # to be alive -- not when the current connection attempt began. A long
    # healthy connection that later drops must not have its entire lifetime
    # counted as outage.
    last_good_at = monotonic()
    last_receipt = None
    last_trade_provider_ms = None

    def stop_requested() -> bool:
        return config.STOP or lease_lost.is_set()

    while not stop_requested() and now_et() < session_close:
        phase = "create_session"
        try:
            session_id = client.create_market_session()
            phase = "connect"
            with market_lines(client, symbols, session_id) as lines:
                phase = "read"
                connected_at = now_et() if session_open is not None else None
                if result.stream_connected_at is None:
                    result.stream_connected_at = connected_at
                # Readiness belongs to this connection, never an old one that
                # disconnected before the open. A 200 response alone is insufficient.
                connection_ready_at = None
                # Do not wait for requests' default 512-byte buffer to fill at
                # the boundary; process each complete provider line immediately.
                for line in lines:
                    observed_at = now_et()
                    if stop_requested() or observed_at >= session_close:
                        break
                    if not line:
                        continue
                    last_good_at = monotonic()
                    if gap_open:
                        outage_seconds = monotonic() - gap_started_at
                        result.gap_seconds += outage_seconds
                        resumed = {
                            "type": "gap",
                            "reason": "stream_reconnect_resumed",
                            "receipt_timestamp": utc_now(),
                            "reconnect": result.reconnects,
                            "outage_seconds": round(outage_seconds, 3),
                        }
                        resumed = stats.observe(resumed)
                        spool.write(resumed)
                        if recent is not None:
                            recent.observe(resumed)
                        print(json.dumps({"event": "stream_reconnect_resumed", **resumed}), flush=True)
                        gap_open = False
                        gap_started_at = None
                        consecutive_failures = 0
                    receipt = utc_now()
                    last_receipt = receipt
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError("non-object event")
                    except (json.JSONDecodeError, ValueError, TypeError):
                        if session_open is not None and observed_at < session_open:
                            continue
                        stats.observe_malformed()
                        raw = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
                        spool.write({
                            "type": "malformed",
                            "collector_receipt_timestamp": receipt,
                            "provider_payload": raw,
                        })
                        continue
                    event["collector_receipt_timestamp"] = receipt
                    event["provider"] = "tradier"
                    if event.get("type") == "timesale":
                        last_trade_provider_ms = parse_epoch_ms(event.get("date"))
                    if session_open is not None:
                        valid = (event.get("type") == "heartbeat" or (
                            event.get("type") in {"quote", "timesale"}
                            and event.get("symbol") in symbols))
                        if valid and connection_ready_at is None:
                            connection_ready_at = observed_at
                            print(json.dumps({"event": "stream_ready", "at": observed_at.isoformat(),
                                              "before_open": observed_at <= session_open}), flush=True)
                        if observed_at < session_open:
                            if valid and event.get("type") == "quote":
                                stats.retain_quote(event, preopen=True)
                            result.preopen_events_discarded += 1
                            continue
                        if result.opening_stream_ready_at is None and valid:
                            result.opening_stream_ready_at = connection_ready_at
                        if event.get("type") == "timesale":
                            trade_ms = parse_epoch_ms(event.get("date"))
                            session = event.get("session")
                            reason = None
                            if trade_ms is None:
                                reason = "missing_or_invalid_provider_time"
                            elif not session_open.timestamp() * 1000 <= trade_ms < session_close.timestamp() * 1000:
                                reason = "outside_regular_session"
                            elif session not in (None, "", "normal"):
                                reason = "non_regular_session_label"
                            if reason:
                                result.excluded_timesales[reason] += 1
                                # Keep the original evidence, but never expose it
                                # as a regular timesale or feed it into trade stats.
                                spool.write({"type": "excluded_timesale", "reason": reason,
                                             "collector_receipt_timestamp": receipt,
                                             "provider_payload": event})
                                stats.observe_excluded(event, reason)
                                continue
                    observed = stats.observe(event)
                    spool.write(observed)
                    if recent is not None:
                        recent.observe(observed)
                if stop_requested() or now_et() >= session_close:
                    break
                raise requests.ConnectionError("Tradier stream ended cleanly before session close")
        except SpoolExhausted:
            result.stop_reason = "spool_exhausted"
            return result
        except (requests.RequestException, OSError) as exc:
            from market_data.diagnostics import exception_details
            diagnostics = exception_details(exc, (os.getenv("TRADIER_TOKEN", ""), locals().get("session_id", "")))
            print(json.dumps({"event": "stream_failure", "phase": phase,
                              "at": utc_now(), "error": diagnostics,
                              "retryable": is_retryable(exc),
                              "silence_seconds": round(monotonic() - last_good_at, 3),
                              "last_receipt_timestamp": last_receipt,
                              "last_trade_provider_timestamp_ms": last_trade_provider_ms,
                              "attempt": consecutive_failures + 1}), flush=True)
            if not is_retryable(exc):
                raise
            if not gap_open:
                gap_open = True
                gap_started_at = last_good_at
                consecutive_failures = 1
                disconnect = stats.observe({
                    "type": "gap",
                    "reason": "stream_disconnect",
                    "receipt_timestamp": utc_now(),
                    "error_type": type(exc).__name__,
                    "error": diagnostics,
                    "phase": phase,
                })
                spool.write(disconnect)
                if recent is not None:
                    recent.observe(disconnect)
            else:
                consecutive_failures += 1
            result.reconnects += 1
            if consecutive_failures > max_consecutive_reconnects:
                budget_exceeded = RuntimeError(
                    f"Tradier stream exceeded {max_consecutive_reconnects} consecutive reconnects"
                )
                budget_exceeded.reconnects = result.reconnects  # type: ignore[attr-defined]
                budget_exceeded.gap_seconds = (  # type: ignore[attr-defined]
                    result.gap_seconds + (monotonic() - gap_started_at)
                )
                raise budget_exceeded from exc
            delay = min(2 ** (consecutive_failures - 1), 15)
            print(json.dumps({"event": "stream_retry_scheduled", "attempt": consecutive_failures,
                              "delay_seconds": delay, "reconnects": result.reconnects}), flush=True)
            remaining = delay
            while remaining > 0 and not stop_requested():
                interval = min(0.5, remaining)
                sleeper(interval)
                remaining -= interval
    if lease_lost.is_set():
        result.stop_reason = "lease_lost"
    if gap_open and gap_started_at is not None:
        # Terminal stop/close while still disconnected -- preserve the
        # unresolved outage instead of silently discarding its duration.
        result.gap_seconds += monotonic() - gap_started_at
    return result
