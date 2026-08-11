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

    --fault timeout      hang past the client timeout, but still record the fill
    --fault 500          record the fill, then return a 500
    --fault 429          reply 429 with Retry-After on /api/paper-trade
    --fault 410          delete the account mid-flight (margin call)
    --fault quote_429    reply 429 with Retry-After on /api/live-quotes
    --fault unreachable  hang past the client timeout; records NOTHING (unlike
                         `timeout`) -- simulates the request never reaching
                         the server at all, not "processed but unconfirmed"
    --me-fault 503       independent of --fault: GET /api/me also returns 503
                         for --me-fault-count calls, so a paper-trade fault
                         and a simultaneously-unavailable /api/me can be
                         combined to construct a genuinely unresolvable
                         execution (see verify_invariants.py's
                         scenario_ambiguous_stays_pending_until_reconciled)

`timeout` and `500` leave the server holding a trade the client never got
confirmation of -- exactly the ambiguity the persisted execution_request_id
is meant to resolve, and on its own a retry against the same id immediately
discovers the fill via the idempotent-replay path below. `unreachable` is
different: paired with `--me-fault`, neither the execution nor the
reconciliation read can tell the client anything, so the ambiguity survives
every retry instead of self-resolving on the second attempt.
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Mirrors the deployed Worker's USERNAME_RE (worker.js) -- a username that
# passes here but fails in production is exactly the mismatch that let
# crassus_trumpwhisperer (22 chars) sail through local verification while
# every real registration attempt 400'd.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")

STARTING_CASH = 10_000.0
DEFAULT_SNAPSHOT_FIXTURE = Path(__file__).parent / "fixtures" / "snapshot.json"

USERS: dict[str, dict] = {}
SESSIONS: dict[str, str] = {}
LOCK = threading.Lock()

# Mirrors the deployed Worker's BOT_REGISTRATION_KEY secret. Registering with a
# matching X-Bot-Registration-Key marks the account as a bot; registering
# without the header creates an ordinary human account that never appears in
# /api/bots. A *wrong* key is rejected outright rather than downgraded, so a
# typo in the runner's config fails loudly instead of silently burning a
# username on a non-bot account.
BOT_REGISTRATION_KEY = "mock-operator-key"
# Mirrors worker.js's new machine-auth secrets for the Crassus AI override
# channel (see crassus/crassus/overrides_client.py, crassus/crassus/policy.py).
CRASSUS_AI_KEY = "mock-crassus-ai-key"
CRASSUS_OPERATOR_KEY = "mock-crassus-operator-key"
FAULT = {"mode": None, "remaining": 0}
# Independent of FAULT: lets /api/me be unavailable *at the same time* as a
# paper-trade fault, which is what a genuinely unresolvable execution needs --
# see --fault unreachable and --me-fault below.
ME_FAULT = {"mode": None, "remaining": 0, "skip": 0}
QUOTES: dict[str, dict] = {}
SNAPSHOT_BYTES: bytes = b"{}"

# Crassus AI override channel (mirrors worker.js's D1/KV storage locally, for
# tests only -- see crassus/README.md's "Crassus AI overrides" section).
OVERRIDES: dict[str, dict] = {}
KILL_SWITCH = {"enabled": False}
FREEZES: dict[str, bool] = {}
LEDGER_MIRROR: list[dict] = []


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def take_fault(*expected_modes: str) -> str | None:
    """Consume the injected fault, but only if it applies to this route.

    Scoped by mode so that, e.g., the readiness probe against
    /api/live-quotes during test harness startup cannot silently steal a
    /api/paper-trade fault (or vice versa) before the scenario's own
    request ever sees it.
    """
    with LOCK:
        if FAULT["mode"] in expected_modes and FAULT["remaining"] > 0:
            FAULT["remaining"] -= 1
            return FAULT["mode"]
    return None


def take_me_fault() -> bool:
    """Independent fault axis for GET /api/me -- see ME_FAULT above.

    `skip` lets the first N calls (e.g. the initial login/reconcile reads
    that happen before an order is even placed) succeed normally, so the
    fault applies only to the reconciliation reads that come later.
    """
    with LOCK:
        if ME_FAULT["skip"] > 0:
            ME_FAULT["skip"] -= 1
            return False
        if ME_FAULT["mode"] and ME_FAULT["remaining"] > 0:
            ME_FAULT["remaining"] -= 1
            return True
    return False


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
        if path == "/intraday/latest.json":
            # Serves the durable snapshot from a local fixture instead of
            # the real R2 bucket, so the invariant suite never depends on
            # network access to a production host it doesn't control.
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(SNAPSHOT_BYTES)))
            self.end_headers()
            self.wfile.write(SNAPSHOT_BYTES)
            return

        if path == "/api/bots":
            # Public, unauthenticated. Only accounts flagged is_bot appear, and
            # only whitelisted fields -- never the password.
            with LOCK:
                bots = [
                    {
                        "username": name,
                        "alias": u.get("alias") or name,
                        "strategy_id": u.get("strategy_id"),
                        "balance_cash": u["cash"],
                        "starting_balance": STARTING_CASH,
                        "trade_count": len(u["trades"]),
                    }
                    for name, u in sorted(USERS.items())
                    if u.get("is_bot")
                ]
            return self._json(200, {"bots": bots})

        if path == "/api/me":
            if take_me_fault():
                return self._json(503, {"error": "temporarily_unavailable"})

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

            if take_fault("quote_429") == "quote_429":
                return self._json(429, {"error": "rate_limited"}, {"Retry-After": "1"})

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

        if path.startswith("/api/crassus/overrides/"):
            if self.headers.get("X-Bot-Registration-Key") != BOT_REGISTRATION_KEY:
                return self._json(403, {"error": "invalid_bot_key"})
            alias = path.rsplit("/", 1)[-1]
            with LOCK:
                candidates = [
                    o for o in OVERRIDES.values()
                    if o["account_alias"] == alias and o["status"] == "accepted" and o["expires_utc"] > now()
                ]
            if not candidates:
                return self._json(404, {"error": "not_found"})
            latest = max(candidates, key=lambda o: o["created_utc"])
            return self._json(200, latest)

        if path == "/api/crassus/kill-switch":
            return self._json(200, {"enabled": KILL_SWITCH["enabled"]})

        if path.startswith("/api/crassus/freeze/"):
            if self.headers.get("X-Bot-Registration-Key") != BOT_REGISTRATION_KEY:
                return self._json(403, {"error": "invalid_bot_key"})
            alias = path.rsplit("/", 1)[-1]
            return self._json(200, {"frozen": FREEZES.get(alias, False)})

        return self._json(404, {"error": "not_found"})

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/register":
            body = self._body()
            username = body.get("username")
            bot_key = self.headers.get("X-Bot-Registration-Key")
            is_bot = False
            if bot_key is not None:
                if bot_key != BOT_REGISTRATION_KEY:
                    return self._json(403, {"error": "Invalid bot registration key"})
                is_bot = True
            with LOCK:
                if not username or not body.get("password"):
                    return self._json(400, {"error": "username and password required"})
                if not USERNAME_RE.match(username):
                    return self._json(400, {"error": "Username must be 3-20 characters: letters, numbers, underscore"})
                if username in USERS:
                    return self._json(409, {"error": "already_exists"})
                USERS[username] = {
                    "password": body["password"],
                    "cash": STARTING_CASH,
                    "trades": [],
                    "is_bot": is_bot,
                    "alias": body.get("alias") or username,
                    "strategy_id": body.get("strategy_id"),
                }
            return self._issue_session(username, 201)

        if path == "/api/bot-metadata":
            bot_key = self.headers.get("X-Bot-Registration-Key")
            if bot_key != BOT_REGISTRATION_KEY:
                return self._json(403, {"error": "Invalid bot registration key"})
            body = self._body()
            username = body.get("username")
            if not isinstance(username, str) or not USERNAME_RE.match(username):
                return self._json(400, {"error": "username must be a valid username"})
            with LOCK:
                user = USERS.get(username)
                if user is None:
                    return self._json(404, {"error": "No such account"})
                # A human account is never editable into the public roster.
                if not user.get("is_bot"):
                    return self._json(409, {"error": "Not a bot account"})
                if "strategy_id" in body:
                    user["strategy_id"] = body["strategy_id"]
                if "alias" in body:
                    user["alias"] = body["alias"]
                return self._json(200, {
                    "username": username,
                    "alias": user["alias"],
                    "strategy_id": user["strategy_id"],
                })

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

        if path == "/api/crassus/overrides":
            if self.headers.get("X-Crassus-Ai-Key") != CRASSUS_AI_KEY:
                return self._json(403, {"error": "invalid_crassus_ai_key"})
            body = self._body()
            override_id = uuid.uuid4().hex
            # Mirrors worker.js: the client sends expires_in_minutes, the
            # server computes expires_utc -- a client can propose a duration,
            # never a fixed timestamp.
            expires_utc = (
                datetime.now(timezone.utc)
                + timedelta(minutes=float(body.get("expires_in_minutes") or 0))
            ).isoformat()
            with LOCK:
                OVERRIDES[override_id] = {
                    "id": override_id,
                    "account_alias": body.get("account_alias"),
                    "status": "proposed",
                    "previous_params": body.get("previous_params"),
                    "proposed_params": body.get("proposed_params"),
                    "rationale": body.get("rationale"),
                    "evidence_refs": body.get("evidence_refs"),
                    "model": body.get("model"),
                    "created_utc": now(),
                    "expires_utc": expires_utc,
                    "accepted_utc": None,
                    "accepted_by": None,
                    "rollback_target": body.get("rollback_target"),
                    "schema_version": "crassus_override.v1",
                }
            return self._json(201, {"id": override_id, "status": "proposed"})

        if path.startswith("/api/crassus/overrides/") and path.endswith(("/accept", "/reject")):
            if self.headers.get("X-Crassus-Operator-Key") != CRASSUS_OPERATOR_KEY:
                return self._json(403, {"error": "invalid_operator_key"})
            parts = path.split("/")
            override_id, action = parts[-2], parts[-1]
            with LOCK:
                row = OVERRIDES.get(override_id)
                if not row:
                    return self._json(404, {"error": "not_found"})
                row["status"] = "accepted" if action == "accept" else "rejected"
                if action == "accept":
                    row["accepted_utc"] = now()
                    row["accepted_by"] = "mock-operator"
            return self._json(200, {"id": override_id, "status": row["status"]})

        if path == "/api/crassus/kill-switch":
            if self.headers.get("X-Crassus-Operator-Key") != CRASSUS_OPERATOR_KEY:
                return self._json(403, {"error": "invalid_operator_key"})
            body = self._body()
            KILL_SWITCH["enabled"] = bool(body.get("enabled"))
            return self._json(200, {"enabled": KILL_SWITCH["enabled"]})

        if path == "/api/crassus/freeze":
            if self.headers.get("X-Crassus-Operator-Key") != CRASSUS_OPERATOR_KEY:
                return self._json(403, {"error": "invalid_operator_key"})
            body = self._body()
            alias = body.get("account_alias")
            FREEZES[alias] = bool(body.get("frozen"))
            return self._json(200, {"account_alias": alias, "frozen": FREEZES[alias]})

        if path == "/api/crassus/ledger":
            if self.headers.get("X-Bot-Registration-Key") != BOT_REGISTRATION_KEY:
                return self._json(403, {"error": "invalid_bot_key"})
            with LOCK:
                LEDGER_MIRROR.append(self._body())
            return self._json(200, {"ok": True})

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

        fault = take_fault("timeout", "500", "429", "410", "unreachable")
        if fault == "429":
            return self._json(429, {"error": "rate_limited"}, {"Retry-After": "2"})

        if fault == "unreachable":
            # Unlike `timeout`, nothing is recorded here at all -- this
            # simulates the request never reaching the server (a network
            # partition), not "processed but the response was lost". A
            # replay of the same execution_request_id will find no trade
            # and genuinely retry, rather than discovering an idempotent
            # fill on the very next attempt.
            time.sleep(30)
            return

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
    ap.add_argument("--fault", choices=["timeout", "500", "429", "410", "quote_429", "unreachable"])
    ap.add_argument("--fault-count", type=int, default=1)
    ap.add_argument("--me-fault", choices=["503"], help="Independent fault for GET /api/me -- see module docstring")
    ap.add_argument("--me-fault-count", type=int, default=1)
    ap.add_argument(
        "--me-fault-skip", type=int, default=0,
        help="Let this many /api/me calls succeed before the fault kicks in",
    )
    ap.add_argument(
        "--snapshot", type=Path, default=DEFAULT_SNAPSHOT_FIXTURE,
        help="Local JSON fixture served at /intraday/latest.json (default: bundled fixture)",
    )
    args = ap.parse_args()

    if args.fault:
        FAULT["mode"], FAULT["remaining"] = args.fault, args.fault_count
    if args.me_fault:
        ME_FAULT["mode"], ME_FAULT["remaining"], ME_FAULT["skip"] = (
            args.me_fault, args.me_fault_count, args.me_fault_skip,
        )

    global SNAPSHOT_BYTES
    SNAPSHOT_BYTES = args.snapshot.read_bytes()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"mock worker on http://127.0.0.1:{args.port}"
          + (f" (fault={args.fault} x{args.fault_count})" if args.fault else "")
          + (f" (me-fault={args.me_fault} x{args.me_fault_count})" if args.me_fault else ""))
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
