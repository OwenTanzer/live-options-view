#!/usr/bin/env python3
"""A local stand-in for the Options View Worker.

The account half of the real API is not present on the deployed Worker (see
P0 findings), so this implements the documented contract locally. It exists to
prove the runtime's integrity invariants -- idempotent fills, restart
recovery, ambiguous-execution reconciliation, margin-call handling -- which
otherwise could not be exercised at all until the deployment is fixed.

It deliberately reproduces the real Worker's awkward properties: no
account_id, no positions endpoint, no server P&L, a dropped `reason` field,
and settlement that deletes an insolvent user outright.

Fault injection lets the recovery paths be tested on demand:

    --fault timeout   hang past the client timeout, but still record the fill
    --fault 500       record the fill, then return a 500
    --fault 429       reply 429 with Retry-After
    --fault 410       delete the account mid-flight (margin call)

`timeout` and `500` are the interesting ones: both leave the server holding a
trade the client never got confirmation of, which is exactly the ambiguity the
persisted execution_request_id is meant to resolve.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STARTING_CASH = 10_000.0

USERS: dict[str, dict] = {}
SESSIONS: dict[str, str] = {}
LOCK = threading.Lock()
FAULT = {"mode": None, "remaining": 0}
QUOTES: dict[str, dict] = {}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def take_fault() -> str | None:
    with LOCK:
        if FAULT["mode"] and FAULT["remaining"] > 0:
            FAULT["remaining"] -= 1
            return FAULT["mode"]
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print(f"    mock-worker: {fmt % args}")

    # -- helpers ----------------------------------------------------------

    def _json(self, code: int, payload, headers: dict | None = None):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def _session_user(self) -> str | None:
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            if part.strip().startswith("sid="):
                return SESSIONS.get(part.strip()[4:])
        return None

    # -- routes -----------------------------------------------------------

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/me":
            user = self._session_user()
            if not user:
                return self._json(403, {"error": "not_authenticated"})
            with LOCK:
                if user not in USERS:
                    # Liquidated: the Worker deletes the user outright.
                    return self._json(410, {"error": "account_liquidated"})
                u = USERS[user]
                return self._json(
                    200,
                    {"username": user, "balance_cash": round(u["cash"], 2), "trades": list(u["trades"])},
                )

        if path == "/api/live-quotes":
            from urllib.parse import parse_qs, urlparse

            symbols = parse_qs(urlparse(self.path).query).get("symbols", [""])[0]
            symbols = [s for s in symbols.split(",") if s]
            if not symbols:
                return self._json(400, {"error": "symbols required"})
            ts = now()
            return self._json(
                200,
                {
                    "quotes": [
                        {
                            "kind": "option",
                            "symbol": s,
                            "bid": QUOTES.get(s, {}).get("bid", 8.20),
                            "ask": QUOTES.get(s, {}).get("ask", 8.34),
                            "quote_ts": ts,  # always fresh, unlike premarket reality
                            "strike": 702.0,
                            "type": "call",
                            "exp": "2026-07-22",
                        }
                        for s in symbols
                    ],
                    "requested": len(symbols),
                    "returned": len(symbols),
                    "server_ts": ts,
                    "health": {"state": "live", "last_quote_at": ts, "server_ts": ts},
                },
            )

        return self._json(404, {"error": "not_found"})

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/register":
            body = self._body()
            username = body.get("username")
            with LOCK:
                if not username or not body.get("password"):
                    return self._json(400, {"error": "username and password required"})
                if username in USERS:
                    return self._json(409, {"error": "already_exists"})
                USERS[username] = {"password": body["password"], "cash": STARTING_CASH, "trades": []}
            return self._issue_session(username, 201)

        if path == "/api/login":
            body = self._body()
            username = body.get("username")
            with LOCK:
                user = USERS.get(username)
                if not user or user["password"] != body.get("password"):
                    return self._json(401, {"error": "invalid_credentials"})
            return self._issue_session(username, 200)

        if path == "/api/logout":
            return self._json(200, {"ok": True})

        if path == "/api/paper-trade":
            return self._paper_trade()

        return self._json(404, {"error": "not_found"})

    def _issue_session(self, username: str, code: int):
        sid = uuid.uuid4().hex
        SESSIONS[sid] = username
        body = json.dumps({"username": username}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", f"sid={sid}; Path=/; HttpOnly")
        self.end_headers()
        self.wfile.write(body)

    def _paper_trade(self):
        user = self._session_user()
        if not user:
            return self._json(403, {"error": "not_authenticated"})

        body = self._body()
        request_id = body.get("execution_request_id")
        sym, side, qty = body.get("sym"), body.get("side"), body.get("qty")
        if not all([request_id, sym, side, qty]):
            return self._json(400, {"error": "invalid_intent"})

        fault = take_fault()
        if fault == "429":
            return self._json(429, {"error": "rate_limited"}, {"Retry-After": "2"})

        with LOCK:
            if user not in USERS:
                return self._json(410, {"error": "account_liquidated"})
            u = USERS[user]

            if fault == "410":
                del USERS[user]
                return self._json(410, {"error": "account_liquidated"})

            # Idempotent on execution_request_id: a replay returns the original
            # fill rather than doubling the position.
            existing = next((t for t in u["trades"] if t["execution_request_id"] == request_id), None)
            if existing:
                return self._json(200, {"trade": existing, "replayed": True, "balance_cash": round(u["cash"], 2)})

            price = QUOTES.get(sym, {}).get("ask" if side == "buy" else "bid", 8.34 if side == "buy" else 8.20)
            trade = {
                "execution_request_id": request_id,
                "sym": sym,
                "side": side,
                "qty": qty,
                "price": price,
                "ts": now(),
                # Note: no `reason` field -- the real Worker drops it silently.
            }
            u["trades"].append(trade)
            u["cash"] += (-1 if side == "buy" else 1) * price * qty * 100

        if fault == "timeout":
            # The fill is already recorded; the client will never hear about it.
            time.sleep(30)
            return
        if fault == "500":
            return self._json(500, {"error": "internal"})

        with LOCK:
            cash = round(USERS[user]["cash"], 2) if user in USERS else 0.0
        return self._json(200, {"trade": trade, "balance_cash": cash})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--fault", choices=["timeout", "500", "429", "410"])
    ap.add_argument("--fault-count", type=int, default=1)
    args = ap.parse_args()

    if args.fault:
        FAULT["mode"], FAULT["remaining"] = args.fault, args.fault_count

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"mock worker on http://127.0.0.1:{args.port}"
          + (f" (fault={args.fault} x{args.fault_count})" if args.fault else ""))
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
