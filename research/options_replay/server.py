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
from engine import DEFAULT_FEE, DwellError, ReplayError, ReplaySession  # noqa: E402
from store import StoreError, TrainerStore  # noqa: E402

MAX_BODY = 4096


class Games:
    def __init__(self, arc: A.Archive, pool: list[str], fee: float, seed: int | None,
                 force_day: str | None = None, force_start: int | None = None,
                 store: TrainerStore | None = None):
        self.store = store
        self.force_day = force_day
        self.force_start = force_start
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
        if self.force_day:
            pack = self._load(self.force_day)
            if pack is None:
                raise ReplayError("the forced day does not qualify")
            return pack
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
        if self.force_day:
            return
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
        store = self.store

        def on_fill(sess, fill, state, new_trade):
            return store.record_fill(session_id=sess.id, trade_id=fill["trade_id"], new_trade=new_trade,
                                     attempt=len(sess.attempts) + 1, fill=fill, decision_state=state)

        session = ReplaySession(pack, rng=self.rng, fee_per_contract_side=self.fee,
                                start_s=self.force_start,
                                journal_dir=self.arc.cache_dir / "journals",
                                raw_lookup=lambda key, c: A.raw_quote(self.arc, key, c),
                                on_fill=on_fill if store else None,
                                on_reveal=(lambda sess: store.mark_revealed(sess.id)) if store else None)
        if store:
            store.add_session(session.id, pack["day"], session.start_s, self.fee)
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
            if url.path in ("/api/records", "/api/trade"):
                if games.store is None:
                    return self._send(503, {"error": "no trainer store"})
                qs = parse_qs(url.query)
                state = qs.get("state", ["0"])[0] == "1"
                if url.path == "/api/trade":
                    recs = games.store.trades(trade_id=qs.get("id", [""])[0], include_state=True)
                    return self._send(200 if recs else 404, recs[0] if recs else {"error": "unknown trade"})
                sid = qs.get("s", [None])[0]
                return self._send(200, {"store": str(games.store.path), "trades":
                                        games.store.trades(session_id=sid, include_state=state)})
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
                if url.path == "/api/note":
                    if games.store is None:
                        return self._send(503, {"error": "no trainer store"})
                    try:
                        ack = games.store.add_note_revision(str(data.get("trade_id", "")), data.get("text"))
                    except StoreError as e:
                        return self._send(400, {"error": str(e)})
                    return self._send(200, ack)
                if url.path == "/api/new":
                    return self._send(200, games.new().view())
                s = games.get(str(data.get("s", "")))
                if url.path == "/api/advance":
                    bars = data.get("max_bars", 1)
                    return self._send(200, s.advance(data.get("seconds", 60),
                                                     None if bars is None else int(bars)))
                if url.path == "/api/step":
                    return self._send(200, s.step_bar())
                if url.path == "/api/reset":
                    return self._send(200, s.reset())
                if url.path == "/api/order":
                    return self._send(200, s.order(str(data.get("side")), str(data.get("contract")),
                                                   data.get("qty", 1)))
                if url.path == "/api/reveal":
                    return self._send(200, s.reveal())
                return self._send(404, {"error": "not found"})
            except ReplayError as e:
                return self._send(e.status, {"error": str(e), "dwell": isinstance(e, DwellError)})
            except (TypeError, ValueError) as e:
                return self._send(HTTPStatus.BAD_REQUEST, {"error": f"bad request: {e}"})

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8737)
    ap.add_argument("--cache", default=str(A.DEFAULT_CACHE))
    ap.add_argument("--fee", type=float, default=DEFAULT_FEE,
                    help="USD per contract per side (default %(default)s)")
    ap.add_argument("--seed", type=int, default=None, help="fix day/start choice (testing)")
    ap.add_argument("--day", default=None,
                    help="operator reproduction only: play this YYYY-MM-DD (the operator then knows the date)")
    ap.add_argument("--start", default=None, help="operator reproduction only: landing time HH:MM (ET)")
    ap.add_argument("--store", default=None,
                    help="trainer SQLite file (default <cache>/trainer.sqlite3); isolated from live trading")
    args = ap.parse_args()
    arc = A.Archive(args.cache)
    pool_file = Path(args.cache) / "qualified_days.json"
    if not pool_file.exists():
        print("No day pool yet: run research/options_replay/qualify.py first.", file=sys.stderr)
        return 2
    pool = json.loads(pool_file.read_text())["qualified"]
    start = None
    if args.start:
        hh, mm = (int(x) for x in args.start.split(":"))
        start = hh * 3600 + mm * 60
    store = TrainerStore(args.store or Path(args.cache) / "trainer.sqlite3")
    games = Games(arc, pool, args.fee, args.seed, args.day, start, store)
    games.prefetch()
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(games))
    print(f"Options Replay: http://{args.host}:{args.port}/  ({len(pool)} qualified days, "
          f"fee ${args.fee:.2f}/contract/side, trainer store {store.path})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
