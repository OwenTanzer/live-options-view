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
import re
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import archive as A  # noqa: E402
from engine import (DEFAULT_FEE, DwellError, NotSavedError, ReplayError, ReplaySession,  # noqa: E402
                    StaleError, hhmmss, label)
from store import ConflictError, StoreError, TrainerStore, body_digest  # noqa: E402

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
        self.locks: dict[str, threading.RLock] = {}

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

    # -- session wiring -----------------------------------------------------

    def _make(self, pack: dict, *, start_s=None, session_id=None, fee=None) -> ReplaySession:
        store = self.store

        def on_fill(sess, fill, state, new_trade, request=None, fault=None):
            return store.record_fill(session_id=sess.id, trade_id=fill["trade_id"], new_trade=new_trade,
                                     attempt=len(sess.attempts) + 1, fill=fill, decision_state=state,
                                     request=request, fault=fault)

        def on_state(sess, event, request):
            if event == "reset":
                return store.log_reset(sess.id, len(sess.attempts) + 1, hhmmss(sess.furthest),
                                       sess.start_s, request=request)
            return store.save_clock(sess.id, sess.clock, sess.furthest, len(sess.attempts) + 1)

        def on_void(sess, fill, request):
            return store.void_fill(session_id=sess.id, fill_id=fill["saved"]["fill_id"], reason="undo",
                                   clock=hhmmss(sess.clock), request=request)

        return ReplaySession(pack, rng=self.rng, fee_per_contract_side=self.fee if fee is None else fee,
                             start_s=start_s, session_id=session_id,
                             journal_dir=self.arc.cache_dir / "journals",
                             raw_lookup=lambda key, c: A.raw_quote(self.arc, key, c),
                             on_fill=on_fill if store else None,
                             on_reveal=(lambda sess: store.mark_revealed(sess.id)) if store else None,
                             on_state=on_state if store else None,
                             on_void=on_void if store else None)

    def new(self) -> ReplaySession:
        pack = self._pick_pack()
        session = self._make(pack, start_s=self.force_start)
        if self.store:
            self.store.add_session(session.id, pack["day"], session.start_s, session.fee)
        with self.lock:
            self.sessions[session.id] = session
            self.locks[session.id] = threading.RLock()
        self.prefetch()
        return session

    def resume(self, sid: str) -> ReplaySession:
        """Rebuild a session from the trainer store (after a restart or in a fresh browser)."""
        row = self.store.session_row(sid) if self.store else None
        if row is None:
            raise ReplayError("unknown session")
        pack = self._load(row["day"])
        if pack is None:
            raise ReplayError("this session's day can no longer be loaded")
        session = self._make(pack, start_s=row["start_s"], session_id=sid, fee=row["fee"])
        stored = self.store.session_fills(sid)
        current = row["attempt"] or 1
        logs = {r["attempt"]: r for r in self.store.attempt_rows(sid)}
        attempts = []
        for a in range(1, current):
            fills = [fill_from_row(r, i + 1) for i, r in enumerate(x for x in stored if x["attempt"] == a)]
            live = [f for f in fills if not f["voided"]]
            attempts.append({
                "attempt": a,
                "realized_net": round(sum(f["net_pnl"] or 0 for f in live if f["side"] != "buy"), 2),
                "fees_paid": round(sum(f["fee"] for f in live), 2),
                "fill_count": len([f for f in live if f["kind"] == "fill"]),
                "open_contracts": sum(f["qty"] if f["side"] == "buy" else -f["qty"] for f in live),
                "seen_until": logs[a]["seen_until"] if a in logs else "?",
                "fills": fills})
        fills = [fill_from_row(r, i + 1) for i, r in enumerate(x for x in stored if x["attempt"] == current)]
        clock = row["clock_s"] if row["clock_s"] is not None else row["start_s"]
        # never resume behind recorded activity (stores written before the clock was persisted)
        def secs(hms):
            h, m, x = (int(v) for v in hms.split(":"))
            return h * 3600 + m * 60 + x
        latest = max((secs(r["decision_clock"]) for r in stored if r["attempt"] == current), default=clock)
        clock = max(clock, latest)
        session.restore(clock=clock, furthest=row["furthest_s"] or clock, attempts=attempts, fills=fills,
                        revealed=row["revealed_at"] is not None)
        if clock != row["clock_s"] and not session.revealed:
            self.store.save_clock(sid, session.clock, session.furthest, current)
        if session.clock >= session.close_s and not session.expired and not session.revealed:
            session._expire()            # settle anything left open if the close was reached
        with self.lock:
            if sid in self.sessions:     # another request resumed it first
                return self.sessions[sid]
            self.sessions[sid] = session
            self.locks.setdefault(sid, threading.RLock())
        return session

    def get(self, sid: str) -> ReplaySession:
        with self.lock:
            s = self.sessions.get(sid)
        if s is None:
            s = self.resume(sid)         # reconnect after a restart: transparent resume
        return s

    def lock_for(self, sid: str):
        with self.lock:
            return self.locks.setdefault(sid, threading.RLock())


EXPIRY_NOTE = ("expired at the close; settled at intrinsic value vs the last "
               "parity-consistent recorded underlying (not a market fill)")


def fill_from_row(r: dict, n: int) -> dict:
    f = {"n": n, "kind": r["kind"], "side": r["side"], "contract": r["contract"], "label": label(r["contract"]),
         "qty": r["qty"], "price": r["price"], "fee": r["fee"], "time_of_day": r["decision_clock"],
         "quote_time": r["quote_time"], "quote_age_s": r["quote_age_s"], "bid": r["bid"], "ask": r["ask"],
         "cash_flow": r["cash_flow"], "net_pnl": r["net_pnl"], "trade_id": r["trade_id"],
         "source_key": r["source_key"],
         "saved": {"trade_id": r["trade_id"], "fill_id": r["fill_id"], "seq": r["seq"],
                   "saved_at": r["created_at"], "decision_state_sha256": r["decision_state_sha256"]},
         "voided": r.get("void_id") is not None}
    if f["voided"]:
        f["void"] = {"void_id": r["void_id"], "fill_id": r["fill_id"], "saved_at": r.get("voided_at")}
    if r["kind"] == "expiry":
        f["note"] = EXPIRY_NOTE
    return f


REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def make_handler(games: Games, allow_faults: bool = False):
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

        def _fault(self) -> str | None:
            f = self.headers.get("X-Replay-Fault")
            return f if (allow_faults and f in ("fail_write", "drop_response")) else None

        def _request(self, data: dict, kind: str, fields: tuple) -> dict | None:
            rid = data.get("request_id")
            if rid is None:
                return None
            if not isinstance(rid, str) or not REQUEST_ID.match(rid):
                raise ReplayError("request_id must be 8-64 characters of [A-Za-z0-9_-]")
            body = {"kind": kind, **{k: data.get(k) for k in fields}}
            return {"id": rid, "body_sha256": body_digest(body)}

        def _replayed(self, req: dict | None, kind: str) -> dict | None:
            """The stored acknowledgment if this request id was already committed."""
            if req is None or games.store is None:
                return None
            prior = games.store.get_request(req["id"])
            if prior is None:
                return None
            if prior["kind"] != kind or prior["body_sha256"] != req["body_sha256"]:
                raise ReplayError("request_id was already used for a different request")
            return prior

        def do_GET(self):
            url = urlparse(self.path)
            qs = parse_qs(url.query)
            try:
                if url.path in ("/", "/index.html"):
                    return self._send(200, page, "text/html; charset=utf-8")
                if url.path == "/api/view":
                    return self._send(200, games.get(qs.get("s", [""])[0]).view())
                if url.path in ("/api/records", "/api/trade", "/api/sessions") and games.store is None:
                    return self._send(503, {"error": "no trainer store"})
                if url.path == "/api/trade":
                    recs = games.store.trades(trade_id=qs.get("id", [""])[0], include_state=True)
                    return self._send(200 if recs else 404, recs[0] if recs else {"error": "unknown trade"})
                if url.path == "/api/records":
                    state = qs.get("state", ["0"])[0] == "1"
                    sid = qs.get("s", [None])[0]
                    return self._send(200, {"store": str(games.store.path), "trades":
                                            games.store.trades(session_id=sid, include_state=state)})
                if url.path == "/api/sessions":
                    with games.lock:
                        live = set(games.sessions)
                    return self._send(200, {"sessions": [{**x, "in_memory": x["session_id"] in live}
                                                         for x in games.store.sessions()]})
                if url.path == "/api/health":
                    return self._send(200, {"ok": True, "pool": len(games.pool),
                                            "refused": len(games.refused), "faults": allow_faults})
                return self._send(404, {"error": "not found"})
            except ReplayError as e:
                return self._send(404, {"error": str(e)})

        def do_POST(self):
            url = urlparse(self.path)
            # same-origin only: the page is served from this server
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "")
            if origin and urlparse(origin).netloc != host:
                return self._send(403, {"error": "cross-origin request refused"})
            s = None
            fault = self._fault()
            try:
                data = self._body()
                if url.path == "/api/note":
                    if games.store is None:
                        return self._send(503, {"error": "no trainer store"})
                    req = self._request(data, "note", ("trade_id", "text", "base_rev"))
                    prior = self._replayed(req, "note")
                    if prior:
                        return self._send(200, {**prior["ack"], "replayed": True})
                    try:
                        base = data.get("base_rev")
                        ack = games.store.add_note_revision(str(data.get("trade_id", "")), data.get("text"),
                                                            None if base is None else int(base), request=req)
                    except ConflictError as e:
                        return self._send(409, {"error": str(e), "conflict": True, "current": e.current})
                    except StoreError as e:
                        return self._send(400, {"error": str(e)})
                    if fault == "drop_response":
                        self.close_connection = True
                        return
                    return self._send(200, ack)
                if url.path == "/api/new":
                    return self._send(200, games.new().view())
                sid = str(data.get("s", ""))
                s = games.get(sid)
                with games.lock_for(sid):
                    if url.path == "/api/resume":
                        return self._send(200, s.view())
                    if url.path == "/api/reveal":
                        return self._send(200, s.reveal())
                    if url.path in ("/api/order", "/api/undo", "/api/reset"):
                        kind = url.path.rsplit("/", 1)[1]
                        fields = {"order": ("s", "side", "contract", "qty", "expected_clock"),
                                  "undo": ("s", "fill_id", "expected_clock"),
                                  "reset": ("s", "expected_clock")}[kind]
                        req = self._request(data, kind, fields)
                        prior = self._replayed(req, kind)
                        if prior:   # committed before (lost response, double click, stale retry)
                            return self._send(200, {**s.view(), "request": {"id": req["id"], "replayed": True,
                                                                            "ack": prior["ack"]}})
                        s.check_expected(data.get("expected_clock"))
                        if kind == "order":
                            v = s.order(str(data.get("side")), str(data.get("contract")), data.get("qty", 1),
                                        request=req, fault=fault if fault == "fail_write" else None)
                            ack = s.fills[-1]["saved"]
                        elif kind == "undo":
                            v = s.undo(str(data.get("fill_id")), request=req)
                            ack = next(f for f in s.fills if (f.get("saved") or {}).get("fill_id")
                                       == data.get("fill_id")).get("void")
                        else:
                            v = s.reset(request=req)
                            ack = {"new_attempt": len(s.attempts) + 1}
                        if fault == "drop_response":     # committed, but the client never hears back
                            self.close_connection = True
                            return
                        return self._send(200, {**v, "request": {"id": req["id"] if req else None,
                                                                 "replayed": False, "ack": ack}})
                    s.check_expected(data.get("expected_clock"))
                    if url.path == "/api/advance":
                        bars = data.get("max_bars", 1)
                        return self._send(200, s.advance(data.get("seconds", 60),
                                                         None if bars is None else int(bars)))
                    if url.path == "/api/step":
                        return self._send(200, s.step_bar())
                return self._send(404, {"error": "not found"})
            except StaleError as e:
                return self._send(409, {"error": str(e), "stale": True, "view": s.view() if s else None})
            except ReplayError as e:
                return self._send(e.status, {"error": str(e), "dwell": isinstance(e, DwellError),
                                             "not_saved": isinstance(e, NotSavedError)})
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
    ap.add_argument("--allow-fault-injection", action="store_true",
                    help="testing only: honour X-Replay-Fault: fail_write | drop_response")
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
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(games, args.allow_fault_injection))
    print(f"Options Replay: http://{args.host}:{args.port}/  ({len(pool)} qualified days, "
          f"fee ${args.fee:.2f}/contract/side, trainer store {store.path})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
