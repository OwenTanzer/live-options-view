#!/usr/bin/env python3
"""Local Replay server (OA-237 S1): random hidden historical day, buy/watch/sell.

    python research/options_replay/qualify.py   # once: build the day pool (read-only R2)
    python research/options_replay/server.py    # then open http://127.0.0.1:8737/

R2 read credentials come from the usual R2_* environment variables (or a
dotenv file named by OPTIONS_REPLAY_ENV_FILE). Snapshots, day packs, the day
pool and session journals all live under OPTIONS_REPLAY_CACHE
(default research/options_replay/.cache/, gitignored) -- temporary storage.

The server binds to 127.0.0.1 by default. It is self-contained: it imports
nothing from backend/ or crassus' trading paths and has no write path to R2,
KV, D1 or the Worker. The clock and the paper ledger live server-side, so the
page only ever receives data at or before the replay clock and no date until
reveal.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import archive as A  # noqa: E402
from engine import DEFAULT_FEE, ReplayError, ReplaySession  # noqa: E402

MAX_BODY = 4096


class Games:
    def __init__(self, arc: A.Archive, pool: list[str], fee: float, seed: int | None):
        self.arc = arc
        self.pool = list(pool)
        self.fee = fee
        self.rng = random.Random(seed)
        self.sessions: dict[str, ReplaySession] = {}
        self.lock = threading.Lock()
        self.index = None
        self.refused: dict[str, str] = {}
        self._prefetched: tuple[str, dict] | None = None
        self._prefetching = False

    def _keys(self, yyyymmdd: str) -> list[str]:
        if self.index is None:
            self.index = self.arc.snapshot_index()
        return self.index.get(yyyymmdd, [])

    def _pick_pack(self) -> dict:
        for _ in range(8):
            with self.lock:
                if self._prefetched:
                    day, pack = self._prefetched
                    self._prefetched = None
                    return pack
                candidates = [d for d in self.pool if d not in self.refused]
                if not candidates:
                    raise ReplayError("no qualified day available")
                day = self.rng.choice(candidates)
            pack = self._load(day)
            if pack is not None:
                return pack
        raise ReplayError("could not load a qualified day")

    def _load(self, iso_day: str) -> dict | None:
        yyyymmdd = iso_day.replace("-", "")
        pack = A.load_or_build_pack(self.arc, yyyymmdd, self._keys(yyyymmdd))
        ok, why = A.pack_qualifies(pack)
        if not ok:
            with self.lock:
                self.refused[iso_day] = why
            print(f"[replay] refused a day at load: {why}", flush=True)  # no date in logs either
            return None
        return pack

    def prefetch(self):
        with self.lock:
            if self._prefetched or self._prefetching:
                return
            self._prefetching = True
            candidates = [d for d in self.pool if d not in self.refused]
            day = self.rng.choice(candidates) if candidates else None

        def run():
            try:
                pack = self._load(day) if day else None
                with self.lock:
                    if pack is not None:
                        self._prefetched = (day, pack)
            finally:
                with self.lock:
                    self._prefetching = False
        threading.Thread(target=run, daemon=True).start()

    def new(self) -> ReplaySession:
        pack = self._pick_pack()
        session = ReplaySession(pack, rng=self.rng, fee_per_contract_side=self.fee,
                                journal_dir=self.arc.cache_dir / "journals")
        with self.lock:
            self.sessions[session.id] = session
        self.prefetch()
        return session

    def get(self, sid: str) -> ReplaySession:
        with self.lock:
            s = self.sessions.get(sid)
        if s is None:
            raise ReplayError("unknown session")
        return s


def make_handler(games: Games):
    page = (HERE / "static" / "index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "OptionsReplay/1"

        def log_message(self, fmt, *args):  # keep request paths (no dates in them) terse
            sys.stderr.write("[replay] " + fmt % args + "\n")

        def _send(self, status: int, payload, ctype="application/json"):
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ReplayError("body too large")
            raw = self.rfile.read(n) if n else b"{}"
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                raise ReplayError("invalid JSON")
            if not isinstance(data, dict):
                raise ReplayError("invalid body")
            return data

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                return self._send(200, page, "text/html; charset=utf-8")
            if url.path == "/api/view":
                try:
                    sid = parse_qs(url.query).get("s", [""])[0]
                    return self._send(200, games.get(sid).view())
                except ReplayError as e:
                    return self._send(404, {"error": str(e)})
            if url.path == "/api/health":
                return self._send(200, {"ok": True, "pool": len(games.pool),
                                        "refused": len(games.refused)})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            url = urlparse(self.path)
            # same-origin only: the page is served from this server
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            if origin and urlparse(origin).netloc != host:
                return self._send(403, {"error": "cross-origin request refused"})
            try:
                data = self._body()
                if url.path == "/api/new":
                    return self._send(200, games.new().view())
                s = games.get(str(data.get("s", "")))
                if url.path == "/api/advance":
                    return self._send(200, s.advance(data.get("seconds", 60)))
                if url.path == "/api/order":
                    return self._send(200, s.order(str(data.get("side")), str(data.get("contract")),
                                                   data.get("qty", 1)))
                if url.path == "/api/reveal":
                    return self._send(200, s.reveal())
                return self._send(404, {"error": "not found"})
            except ReplayError as e:
                return self._send(HTTPStatus.CONFLICT, {"error": str(e)})

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8737)
    ap.add_argument("--cache", default=str(A.DEFAULT_CACHE))
    ap.add_argument("--fee", type=float, default=DEFAULT_FEE,
                    help="USD per contract per side (default %(default)s)")
    ap.add_argument("--seed", type=int, default=None, help="fix day/start choice (testing)")
    args = ap.parse_args()
    arc = A.Archive(args.cache)
    pool_file = Path(args.cache) / "qualified_days.json"
    if not pool_file.exists():
        print("No day pool yet: run research/options_replay/qualify.py first.", file=sys.stderr)
        return 2
    pool = json.loads(pool_file.read_text())["qualified"]
    games = Games(arc, pool, args.fee, args.seed)
    games.prefetch()
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(games))
    print(f"Options Replay: http://{args.host}:{args.port}/  ({len(pool)} qualified days, "
          f"fee ${args.fee:.2f}/contract/side)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
