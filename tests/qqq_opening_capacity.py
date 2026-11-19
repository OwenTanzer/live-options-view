"""Offline opening-burst measurement using the collector's HTTP reader and archive.

Run from the repository root with PYTHONPATH=.:tests. All prices, times, sizes
and symbols are synthetic. Storage verification is against an in-memory sink.
"""
import argparse
import gzip
import json
import statistics
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests

from test_moo144_tradier_collector import FakeR2
from market_data.tradier import capture
from market_data.tradier.recent import RecentPrints
from market_data.tradier.spool import SegmentSpool, Uploader
from market_data.tradier.stats import BoundedStats


def measure(strikes, rate, seconds):
    count = rate * seconds
    symbols = [f"QQQ261119{side}{strike * 1000:08d}"
               for strike in range(500, 500 + strikes) for side in "CP"]
    start = datetime.now(timezone.utc)
    stop = threading.Event()
    lag = []
    sent_at = []
    receipt_lag = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        protocol_version = "HTTP/1.1"

        def do_GET(self):
            requested = parse_qs(urlsplit(self.path).query)["symbols"][0].split(",")
            assert requested == ["QQQ", *symbols]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            began = time.monotonic()
            for i in range(count):
                target = began + i / rate
                delay = target - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                symbol = symbols[(i // 2) % len(symbols)]
                timestamp = int(time.time() * 1000)
                event = dict(symbol=symbol, fixture_index=i)
                if i % 2:
                    event.update(type="timesale", date=timestamp, last=1.02, size=2,
                                 seq=i // (2 * len(symbols)) + 1, session="normal")
                else:
                    event.update(type="quote", biddate=timestamp, askdate=timestamp,
                                 bid=1.01, ask=1.03)
                sent_at.append(time.monotonic())
                data = json.dumps(event).encode() + b"\n"
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")

    class Archive(SegmentSpool):
        def write(self, event):
            if "fixture_index" not in event:
                raise RuntimeError(f"unexpected collector record: {event}")
            i = event["fixture_index"]
            super().write(event)
            archived = time.monotonic()
            lag.append(archived - sent_at[i])
            receipt_lag.append(time.time() - datetime.fromisoformat(event["collector_receipt_timestamp"]).timestamp())
            if i == count - 1:
                stop.set()

    class Client:
        session = requests.Session()
        def create_market_session(self):
            return "fixture"

    storage = FakeR2()
    recent = RecentPrints()
    with tempfile.TemporaryDirectory() as tmp, HTTPServer(("127.0.0.1", 0), Provider) as server:
        upload = Uploader(storage, "bucket", "test", 512 * 1024**2)
        spool = Archive(Path(tmp), "test", upload, 2)
        upload.start()
        worker = threading.Thread(target=server.handle_request, daemon=True)
        worker.start()
        began = time.monotonic()
        with patch.object(capture, "STREAM", f"http://127.0.0.1:{server.server_port}/events"):
            capture.capture_session(Client(), ["QQQ", *symbols], spool, BoundedStats(),
                                    start + timedelta(minutes=5), 1, stop,
                                    session_open=start, recent=recent)
        read_seconds = time.monotonic() - began
        spool.close()
        drain_at = time.monotonic()
        upload.drain_and_stop()
        drain_seconds = time.monotonic() - drain_at
        Client.session.close()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert upload.fully_drained()
    raw = [json.loads(line) for key in sorted(storage.objects)
           for line in gzip.decompress(storage.objects[key]).splitlines()]
    assert recent.session.prints == count // 2
    indices = [r["fixture_index"] for r in raw]
    assert indices == list(range(count))
    return dict(symbols=len(symbols)+1, events=count, target_rate=rate,
                sent_per_second=round((count-1)/(sent_at[-1]-sent_at[0])),
                received_per_second=round(count/read_seconds),
                wire_to_spool_p95_ms=round(statistics.quantiles(lag, n=100)[94]*1000, 1),
                receipt_to_spool_p95_ms=round(statistics.quantiles(receipt_lag, n=100)[94]*1000, 1),
                peak_spool_bytes=upload.peak_spool_bytes,
                archive_bytes=sum(map(len, storage.objects.values())), drain_seconds=round(drain_seconds,3),
                missing=0, duplicate=0)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("strikes", type=int)
    parser.add_argument("rate", type=int)
    parser.add_argument("seconds", type=int)
    args = parser.parse_args()
    print(json.dumps(measure(args.strikes, args.rate, args.seconds)))
