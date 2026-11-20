"""OA-242 private credit tape panel.

Design follows directional-move-lab scripts/serve_private.py @2bdebbe
(identity-only GitHub sign-in with PKCE, numeric-ID allowlist, opaque
sessions, server-side checks on every data route). Nothing is shared with
the Lab: separate OAuth app, cookies and sessions.

Data routes (all require an allowed session):
  GET /api/trades?after=<seq>   newest publications (seq = events.jsonl line)
  GET /api/raw?ref=<raw_ref>    original source bytes as captured
  GET /api/export?from=&to=     raw events JSONL for an ISO first-seen interval

Unauthenticated status only (no trade content, any Host):
  GET /healthz                  process up and configured (200) or not (503)
  GET /healthz/collector        collector heartbeat, index age and per-channel
                                state for CFTC_CR and SEC_CR (200 ok / 503)

Local demo: CREDIT_TAPE_DEV_LOCAL=1 skips GitHub sign-in but only binds
127.0.0.1 and refuses any non-loopback Host header.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

STATIC = Path(__file__).resolve().parent / "web"
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"),
          "/index.html": ("index.html", "text/html; charset=utf-8"),
          "/panel.js": ("panel.js", "text/javascript; charset=utf-8"),
          "/panel.css": ("panel.css", "text/css; charset=utf-8")}
SESSION_COOKIE = "__Host-cds-session"
FLOW_COOKIE = "__Host-cds-flow"
GITHUB_ISSUER = "https://github.com/login/oauth"
RAW_REF = re.compile(r"^[a-z0-9-]{1,40}/raw/\d{4}-\d{2}-\d{2}/[0-9A-Za-z.+_-]{1,80}\.(json\.gz|zip)$")
# Slice CSV headers -> the ticker's field names used by the panel summary.
CSV_ALIASES = {"notionalAmountLeg1": "Notional amount-Leg 1", "notionalCurrencyLeg1": "Notional currency-Leg 1",
               "underlierIDLeg1": "Underlier ID-Leg 1", "underlyingAssetName": "Underlying Asset Name",
               "expirationDate": "Expiration Date", "fixedRateLeg1": "Fixed rate-Leg 1", "price": "Price",
               "platformIdentifier": "Platform identifier", "cleared": "Cleared",
               "blocktradeElectionIndicator": "Block trade election indicator",
               "uniqueProductIdentifierShortName": "UPI FISN", "productName": "Product name"}
MAX_ROWS = 500
EXPORT_LIMIT = 64 * 1024 * 1024


@dataclass(frozen=True)
class Settings:
    archive: Path
    origin: str = ""
    client_id: str = ""
    client_secret: str = ""
    allowed_ids: frozenset = frozenset()
    dev_local: bool = False

    def permits(self, user_id) -> bool:
        return type(user_id) is int and user_id in self.allowed_ids

    @classmethod
    def from_env(cls, env=os.environ) -> "Settings":
        archive = Path(env["CREDIT_TAPE_ARCHIVE"])
        if not archive.is_absolute() or archive.resolve().is_relative_to(STATIC.resolve()):
            raise ValueError("Archive must be absolute and outside static assets")
        if env.get("CREDIT_TAPE_DEV_LOCAL") == "1":
            return cls(archive, dev_local=True)
        origin = env["CREDIT_TAPE_PUBLIC_ORIGIN"]
        p = urlsplit(origin)
        if (p.scheme != "https" or not p.hostname or p.username or p.password or p.path
                or p.query or p.fragment or p.netloc != p.hostname):
            raise ValueError("Exact HTTPS origin without port/path required")
        raw = env["CREDIT_TAPE_GITHUB_ALLOWED_IDS"]
        if len(raw) > 256 or not re.fullmatch(r"[1-9][0-9]{0,19}(,[1-9][0-9]{0,19})*", raw):
            raise ValueError("Invalid GitHub allowlist")
        ids = [int(v) for v in raw.split(",")]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate allowlist entries")
        cid, secret = env["CREDIT_TAPE_GITHUB_CLIENT_ID"], env["CREDIT_TAPE_GITHUB_CLIENT_SECRET"]
        if not cid or not secret:
            raise ValueError("Incomplete OAuth configuration")
        return cls(archive, origin, cid, secret, frozenset(ids))


class Store:
    """Opaque in-memory sessions (8h) and signed ephemeral sign-in flows.

    Flow lifecycle follows directional-move-lab scripts/serve_private.py @2bdebbe:
    GET /auth/login allocates no server-side entry (signed HttpOnly cookie, reused
    while valid); a callback claims the flow as *pending* (at most 16 at once) and
    releases it on any failure; only a verified, allowed callback reserves a
    one-time replay record (at most 128, pruned at flow expiry). Failed or refused
    callbacks therefore cannot exhaust sign-in capacity, and an accepted flow can
    never be replayed.
    """

    MAX_PENDING, MAX_SPENT = 16, 128

    def __init__(self, clock=time.time):
        self.clock, self.lock = clock, threading.Lock()
        self.sessions: dict[str, tuple[float, int]] = {}
        self.spent_flows: dict[str, tuple[float]] = {}
        self.pending_flows: set[str] = set()
        self.key = secrets.token_bytes(32)

    def _prune(self):
        now = self.clock()
        for d in (self.sessions, self.spent_flows):
            for k in [k for k, v in d.items() if v[0] <= now]:
                del d[k]

    def _flow(self, token):
        if not isinstance(token, str) or len(token) > 512 or "." not in token:
            raise ValueError("Invalid flow")
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()):
            raise ValueError("Invalid flow")
        expiry, state, verifier = payload.split(".")
        if not self.clock() < int(expiry) <= self.clock() + 600:
            raise ValueError("Expired flow")
        return int(expiry), state, verifier

    def start_flow(self, current: str = ""):
        with self.lock:
            self._prune()
            try:
                _, state, verifier = self._flow(current)
                if current not in self.spent_flows and current not in self.pending_flows:
                    return current, state, verifier  # repeated login reuses one flow without extending expiry
            except (ValueError, TypeError):
                pass
            state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            payload = f"{int(self.clock()) + 600}.{state}.{verifier}"
            return payload + "." + hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest(), state, verifier

    def consume_flow(self, token, state):
        """Claim a flow for one callback; pair every successful claim with finish_flow."""
        with self.lock:
            self._prune()
            _, expected, verifier = self._flow(token)
            if (not hmac.compare_digest(expected, state) or token in self.spent_flows
                    or token in self.pending_flows or len(self.pending_flows) >= self.MAX_PENDING):
                raise ValueError("Invalid or busy sign-in state")
            self.pending_flows.add(token)
            return verifier

    def finish_flow(self, token, accepted=False):
        with self.lock:
            try:
                if accepted:
                    self._prune()
                    if len(self.spent_flows) >= self.MAX_SPENT:
                        raise ValueError("Sign-in completion capacity reached")
                    self.spent_flows[token] = (self._flow(token)[0],)  # one-time replay record
            finally:
                self.pending_flows.discard(token)

    def new_session(self, owner: int) -> str:
        with self.lock:
            self._prune()
            if len(self.sessions) >= 128:
                raise ValueError("Session capacity reached")
            tok = secrets.token_urlsafe(32)
            self.sessions[tok] = (self.clock() + 8 * 3600, owner)
            return tok

    def identity(self, token):
        with self.lock:
            self._prune()
            return self.sessions.get(token, (0, None))[1]

    def revoke(self, token):
        with self.lock:
            self.sessions.pop(token, None)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def github_identity(settings: Settings, code: str, verifier: str) -> int:
    """Fixed GitHub endpoints only; the access token is discarded after one /user call."""
    opener = build_opener(_NoRedirect)

    def fetch(req):
        with opener.open(req, timeout=10) as r:
            body = r.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError("Provider response too large")
        return json.loads(body)

    tok = fetch(Request("https://github.com/login/oauth/access_token", headers={"Accept": "application/json"},
                        data=urlencode({"client_id": settings.client_id, "client_secret": settings.client_secret,
                                        "code": code, "redirect_uri": settings.origin + "/auth/callback",
                                        "code_verifier": verifier}).encode()))
    if tok.get("scope") != "" or str(tok.get("token_type", "")).lower() != "bearer" or not tok.get("access_token"):
        raise ValueError("Unexpected OAuth token response")
    user = fetch(Request("https://api.github.com/user", headers={
        "Accept": "application/vnd.github+json", "User-Agent": "credit-tape-private",
        "Authorization": "Bearer " + tok["access_token"]}))
    if type(user.get("id")) is not int:
        raise ValueError("Invalid provider identity")
    return user["id"]


def cookie(name, value="", age=0):
    return f"{name}={value}; Path=/; Max-Age={age}; Secure; HttpOnly; SameSite=Lax"


# ---- archive readers -------------------------------------------------------

def read_events(archive: Path, after: int = 0, limit: int = MAX_ROWS):
    """Publication events after line `after`, newest last, without the bulky record body."""
    out, seq = [], 0
    path = archive / "events.jsonl"
    if not path.exists():
        return [], 0
    with path.open(encoding="utf-8") as fh:
        for seq, line in enumerate(fh, 1):
            if seq <= after:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                out.append({"seq": seq, "kind": "unreadable_event_line"})
                continue
            rec = ev.pop("record", None) or {}
            ev["seq"] = seq
            summary = {}
            for k, alias in CSV_ALIASES.items():
                v = rec.get(k, rec.get(alias))
                if v not in (None, ""):
                    summary[k] = v
            ev["summary"] = summary
            out.append(ev)
    return out[-limit:], seq


LIVE_STUCK_S = 900.0   # a live slice still pending after 15 min = channel backlog
INDEX_STALE_S = 600.0  # no successful index answer (200 or 304) for 10 min


def _age(iso, now_ts):
    if not iso:
        return None
    try:
        return round(now_ts - datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp(), 1)
    except ValueError:
        return None


def collector_health(archive: Path, now_ts: float | None = None):
    """Unauthenticated status for provider/uptime checks: states and ages only, no trade content.

    ok = collector heartbeat fresh, slice index answered recently, and for BOTH
    channels: listed in the last full index and no live slice stuck pending.
    Quiet markets are not failures: a channel with no new slices stays ok.
    """
    now_ts = time.time() if now_ts is None else now_ts
    try:
        h = json.loads((archive / "health.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, {"ok": False, "reason": "no collector heartbeat yet"}
    mode = h.get("collector") or {}
    interval = float(mode.get("interval_s") or 20.0)
    route = (h.get("sources") or {}).get("dtcc-slice-index") or {}
    beat = _age(h.get("t"), now_ts)
    index_age = _age(route.get("last_index_at"), now_ts)
    beat_ok = beat is not None and beat < max(3 * interval, 120.0)
    index_ok = index_age is not None and index_age < INDEX_STALE_S
    channels = {}
    for key in ("CFTC_CR", "SEC_CR"):
        c = (route.get("channels") or {}).get(key) or {}
        last = c.get("last_live_completed") or {}
        if not c.get("listed_in_index"):
            state = "missing_from_index"
        elif (c.get("oldest_live_pending_s") or 0) >= LIVE_STUCK_S:
            state = "live_backlog"
        else:
            state = "ok"
        channels[key] = {"state": state, "listed_slices": c.get("listed_slices"),
                         "listing_age_s": _age(c.get("listing_at"), now_ts),
                         "pending_live": c.get("pending_live"), "pending_backfill": c.get("pending_backfill"),
                         "max_attempts": c.get("max_attempts"),
                         "last_live_dissem_age_s": _age(last.get("dissemDTM"), now_ts),
                         "last_live_completed_age_s": _age(last.get("at"), now_ts),
                         "last_backfill_completed_age_s": _age((c.get("last_backfill_completed") or {}).get("at"), now_ts)}
    worker = (route.get("backfill_worker") or {}).get("state")
    ok = beat_ok and index_ok and worker != "failed" and all(c["state"] == "ok" for c in channels.values())
    return ok, {"ok": ok, "slice_only": mode.get("ticker") is False and mode.get("slices") is True,
                "heartbeat_age_s": beat, "index_age_s": index_age, "index_polls_err": route.get("polls_err"),
                "backfill_worker": worker, "channels": channels}


def read_raw(archive: Path, ref: str) -> bytes:
    if not RAW_REF.fullmatch(ref or "") or ".." in ref:
        raise ValueError("Invalid reference")
    target = (archive / ref).resolve()
    if not target.is_relative_to(archive.resolve()) or target.is_symlink():
        raise ValueError("Invalid reference")
    if ref.endswith(".zip"):
        return target.read_bytes()  # slice zip exactly as published
    with gzip.open(target, "rb") as fh:
        return fh.read()


def export_events(archive: Path, start: str, end: str) -> bytes:
    if not (re.fullmatch(r"\d{4}-\d{2}-\d{2}T[\d:.]+(\+00:00|Z)?", start)
            and re.fullmatch(r"\d{4}-\d{2}-\d{2}T[\d:.]+(\+00:00|Z)?", end)):
        raise ValueError("Use ISO UTC times")
    chunks, size = [], 0
    with (archive / "events.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            try:
                fs = json.loads(line).get("first_seen", "")
            except ValueError:
                continue
            if start <= fs < end:
                size += len(line)
                if size > EXPORT_LIMIT:
                    raise ValueError("Interval too large")
                chunks.append(line)
    return "".join(chunks).encode()


# ---- HTTP ------------------------------------------------------------------

def make_handler(settings: Settings | None, store: Store | None = None, identity=github_identity):
    store = store or Store()

    class Handler(BaseHTTPRequestHandler):
        server_version = "CreditTapePrivate"

        def log_message(self, *a):  # default logging would record OAuth codes
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def reply(self, status, body=b"", kind="text/plain; charset=utf-8", headers=()):
            self.send_response(status)
            for k, v in [("Content-Type", kind), ("Content-Length", str(len(body))),
                         ("Cache-Control", "no-store, private"), ("Vary", "Cookie"),
                         ("X-Content-Type-Options", "nosniff"), ("Referrer-Policy", "no-referrer"),
                         ("X-Frame-Options", "DENY"),
                         ("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                          "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"),
                         *headers]:
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def cookies(self):
            try:
                return {k: v.value for k, v in SimpleCookie(self.headers.get("Cookie", "")).items()}
            except CookieError:
                return {}

        def host_ok(self):
            host = self.headers.get("Host", "")
            if settings.dev_local:
                return re.fullmatch(r"(127\.0\.0\.1|localhost)(:\d+)?", host) is not None
            return host == urlsplit(settings.origin).netloc

        def authorized(self):
            if settings.dev_local:
                return True
            return settings.permits(store.identity(self.cookies().get(SESSION_COOKIE, "")))

        def do_HEAD(self):
            self.do_GET()

        def do_POST(self):
            if (not settings or settings.dev_local or not self.host_ok() or self.path != "/auth/logout"
                    or self.headers.get("Origin") != settings.origin):
                return self.reply(403, b"Forbidden")
            store.revoke(self.cookies().get(SESSION_COOKIE, ""))
            return self.reply(204, headers=[("Set-Cookie", cookie(SESSION_COOKIE)), ("Set-Cookie", cookie(FLOW_COOKIE))])

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/healthz":
                return self.reply(200 if settings else 503, b'{"status":"ok"}' if settings else b'{"status":"unconfigured"}',
                                  "application/json")
            if url.path == "/healthz/collector":
                if not settings:
                    return self.reply(503, b'{"ok":false,"reason":"unconfigured"}', "application/json")
                ok, body = collector_health(settings.archive)
                return self.reply(200 if ok else 503, json.dumps(body).encode(), "application/json")
            if not settings:
                return self.reply(503, b"Not configured.")
            if not self.host_ok():
                return self.reply(403, b"Forbidden")
            if not settings.dev_local and url.path == "/auth/login":
                try:
                    flow, state, verifier = store.start_flow(self.cookies().get(FLOW_COOKIE, ""))
                except ValueError:
                    return self.reply(429, b"Please retry sign-in later.")
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                loc = "https://github.com/login/oauth/authorize?" + urlencode({
                    "client_id": settings.client_id, "redirect_uri": settings.origin + "/auth/callback",
                    "scope": "", "state": state, "code_challenge": challenge,
                    "code_challenge_method": "S256", "allow_signup": "false"})
                return self.reply(302, headers=[("Location", loc), ("Set-Cookie", cookie(FLOW_COOKIE, flow, 600))])
            if not settings.dev_local and url.path == "/auth/callback":
                flow = self.cookies().get(FLOW_COOKIE, "")
                claimed = False
                try:
                    q = parse_qs(url.query, strict_parsing=True, keep_blank_values=True, max_num_fields=10)
                    # GitHub documents a callback carrying `code` and `state`; when it also sends the
                    # RFC 9207 `iss`, it must be GitHub's exact issuer. Blank, repeated or unknown
                    # fields are refused.
                    if (set(q) not in ({"state", "code"}, {"state", "code", "iss"})
                            or any(len(v) != 1 or not v[0] for v in q.values())
                            or ("iss" in q and q["iss"][0] != GITHUB_ISSUER)
                            or len(q["state"][0]) > 128 or len(q["code"][0]) > 1024):
                        raise ValueError("Invalid callback")
                    verifier = store.consume_flow(flow, q["state"][0])
                    claimed = True
                    owner = identity(settings, q["code"][0], verifier)
                    if not settings.permits(owner):
                        raise ValueError("Account is not allowed")
                    store.finish_flow(flow, accepted=True)  # one-time: this flow can never be replayed
                    claimed = False
                    store.revoke(self.cookies().get(SESSION_COOKIE, ""))
                    session = store.new_session(owner)
                except Exception:
                    if claimed:
                        store.finish_flow(flow)  # failure cleanup: release the pending claim
                    return self.reply(403, b"Sign-in was not accepted. Return to / and try again.",
                                      headers=[("Set-Cookie", cookie(FLOW_COOKIE))])
                return self.reply(303, headers=[("Location", "/"), ("Set-Cookie", cookie(FLOW_COOKIE)),
                                               ("Set-Cookie", cookie(SESSION_COOKIE, session, 8 * 3600))])
            if not self.authorized():
                if url.path in ("/", "/index.html"):
                    return self.reply(200, b'<!doctype html><title>Sign in</title><h1>Credit tape</h1>'
                                           b'<p><a href="/auth/login">Sign in with GitHub</a></p>', "text/html; charset=utf-8")
                return self.reply(401, b"Sign in required.")
            q = parse_qs(url.query)
            try:
                if url.path == "/api/trades":
                    after = int(q.get("after", ["0"])[0])
                    rows, last = read_events(settings.archive, max(after, 0))
                    health = {}
                    hp = settings.archive / "health.json"
                    if hp.exists():
                        health = json.loads(hp.read_text(encoding="utf-8"))
                    return self.reply(200, json.dumps({"last_seq": last, "rows": rows, "health": health}).encode(),
                                      "application/json")
                if url.path == "/api/raw":
                    ref = q.get("ref", [""])[0]
                    blob = read_raw(settings.archive, ref)
                    is_zip = ref.endswith(".zip")
                    return self.reply(200, blob, "application/zip" if is_zip else "application/json", headers=[
                        ("X-Original-SHA256", hashlib.sha256(blob).hexdigest()),
                        ("Content-Disposition", f'{"attachment" if is_zip else "inline"}; filename="{Path(ref).name if is_zip else Path(ref).name[:-3]}"')])
                if url.path == "/api/export":
                    blob = export_events(settings.archive, q.get("from", [""])[0], q.get("to", [""])[0])
                    return self.reply(200, blob, "application/x-ndjson", headers=[
                        ("Content-Disposition", 'attachment; filename="credit-tape-export.jsonl"')])
            except (ValueError, OSError):
                return self.reply(400, b"Bad request.")
            if url.path not in ASSETS:
                return self.reply(404, b"Not found")
            name, kind = ASSETS[url.path]
            try:
                return self.reply(200, (STATIC / name).read_bytes(), kind)
            except OSError:
                return self.reply(404, b"Not found")

    return Handler


def main():
    try:
        settings = Settings.from_env()
    except (KeyError, ValueError):
        print("Credit tape panel configuration incomplete; access denied.", flush=True)
        settings = None
    host = "127.0.0.1" if (settings is None or settings.dev_local) else "0.0.0.0"
    srv = ThreadingHTTPServer((host, int(os.environ.get("PORT", "8790"))), make_handler(settings))
    print(f"credit tape panel on http://{host}:{srv.server_port} dev_local={bool(settings and settings.dev_local)}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
