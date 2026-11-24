"""Optional read-only delta transport. The archive owns the only provider stream."""
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import islice
import json
import logging
import os
import secrets
import threading
import uuid
import time
from urllib.parse import parse_qs, urlsplit

from .intraminute import safe_project


class Tape:
    def __init__(self, recent, limit=100000):
        self.recent = recent
        self.rows = deque(maxlen=limit)
        self.end = 0
        self.started = time.time_ns() // 1000000
        self.epoch = uuid.uuid4().hex
        self.lock = threading.Lock()

    def observe(self, event):
        signed = self.recent.observe(event)
        row = safe_project(event, signed)
        if row:
            with self.lock:
                self.rows.append(row)
                self.end += 1

    def read(self, cursor, epoch):
        with self.lock:
            first = self.end - len(self.rows)
            reset = epoch != self.epoch or not first <= cursor <= self.end
            if reset:
                cursor = first
            last = min(cursor+4096, self.end)
            if self.end-last < cursor-first:
                rows = list(islice(reversed(self.rows), self.end-last, self.end-cursor))
                rows.reverse()
            else:
                rows = list(islice(self.rows, cursor-first, last-first))
            return dict(epoch=self.epoch, started=self.started, cursor=cursor+len(rows), reset=reset,
                        truncated=first > 0 and reset, more=cursor+len(rows)<self.end, rows=rows)


def start(recent):
    port, key = os.getenv("QQQ_TAPE_PORT"), os.getenv("QQQ_TAPE_KEY")
    if not port or not key:
        return recent, None
    tape = Tape(recent)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer "+key):
                self.send_error(403)
                return
            url = urlsplit(self.path)
            if url.path != "/tape":
                self.send_error(404)
                return
            query = parse_qs(url.query)
            cursor = query.get("cursor", ["0"])[0]
            if not cursor.isascii() or not cursor.isdigit() or len(cursor) > 15:
                self.send_error(400)
                return
            payload = tape.read(int(cursor), query.get("epoch", [""])[0])
            body = json.dumps(payload, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    try:
        server = ThreadingHTTPServer(("0.0.0.0", int(port)), Handler)
    except (OSError, ValueError) as exc:
        logging.warning("QQQ tape unavailable: %s", type(exc).__name__)
        return recent, None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return tape, server
