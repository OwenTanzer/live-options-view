"""OA-242 private credit tape panel.

Design follows directional-move-lab scripts/serve_private.py @2bdebbe
(identity-only GitHub sign-in with PKCE, numeric-ID allowlist, opaque
sessions, server-side checks on every data route). Nothing is shared with
the Lab: separate OAuth app, cookies and sessions.

Data routes (all require an allowed session):
  GET /api/trades?after=<seq>   newest publications (seq = events.jsonl line)
  GET /api/raw?ref=<raw_ref>    original source bytes as captured
  GET /api/export?from=&to=     raw events JSONL for an ISO first-seen interval

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
RAW_REF = re.compile(r"^[a-z0-9-]{1,40}/raw/\d{4}-\d{2}-\d{2}/[0-9A-Za-z.+_-]{1,80}\.json\.gz$")
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
    """Opaque in-memory sessions (8h) and signed ephemeral sign-in flows."""

    def __init__(self, clock=time.time):
        self.clock, self.lock = clock, threading.Lock()
        self.sessions: dict[str, tuple[float, int]] = {}
        self.spent: dict[str, float] = {}
        self.key = secrets.token_bytes(32)

    def _prune(self):
        now = self.clock()
        for d in (self.sessions, self.spent):
            for k in [k for k, v in d.items() if (v[0] if isinstance(v, tuple) else v) <= now]:
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

    def start_flow(self):
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        payload = f"{int(self.clock()) + 600}.{state}.{verifier}"
        return payload + "." + hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest(), state, verifier

    def consume_flow(self, token, state):
        with self.lock:
            self._prune()
            expiry, expected, verifier = self._flow(token)
            if not hmac.compare_digest(expected, state) or token in self.spent or len(self.spent) >= 128:
                raise ValueError("Invalid sign-in state")
            self.spent[token] = expiry
            return verifier

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
            ev["summary"] = {k: rec.get(k) for k in (
                "notionalAmountLeg1", "notionalCurrencyLeg1", "underlierIDLeg1", "underlyingAssetName",
                "expirationDate", "fixedRateLeg1", "price", "platformIdentifier", "cleared",
                "blocktradeElectionIndicator", "uniqueProductIdentifierShortName", "productName")
                if rec.get(k) not in (None, "")}
            out.append(ev)
    return out[-limit:], seq


def read_raw(archive: Path, ref: str) -> bytes:
    if not RAW_REF.fullmatch(ref or "") or ".." in ref:
        raise ValueError("Invalid reference")
    target = (archive / ref).resolve()
    if not target.is_relative_to(archive.resolve()) or target.is_symlink():
        raise ValueError("Invalid reference")
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
            return self.reply(204, headers=[("Set-Cookie", cookie(SESSION_COOKIE))])

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/healthz":
                return self.reply(200 if settings else 503, b'{"status":"ok"}' if settings else b'{"status":"unconfigured"}',
                                  "application/json")
            if not settings:
                return self.reply(503, b"Not configured.")
            if not self.host_ok():
                return self.reply(403, b"Forbidden")
            if not settings.dev_local and url.path == "/auth/login":
                flow, state, verifier = store.start_flow()
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                loc = "https://github.com/login/oauth/authorize?" + urlencode({
                    "client_id": settings.client_id, "redirect_uri": settings.origin + "/auth/callback",
                    "scope": "", "state": state, "code_challenge": challenge,
                    "code_challenge_method": "S256", "allow_signup": "false"})
                return self.reply(302, headers=[("Location", loc), ("Set-Cookie", cookie(FLOW_COOKIE, flow, 600))])
            if not settings.dev_local and url.path == "/auth/callback":
                try:
                    q = parse_qs(url.query, strict_parsing=True, keep_blank_values=True, max_num_fields=10)
                    if set(q) != {"state", "code", "iss"} or any(len(v) != 1 or not v[0] for v in q.values()) \
                            or q["iss"][0] != GITHUB_ISSUER or len(q["state"][0]) > 128 or len(q["code"][0]) > 1024:
                        raise ValueError("Invalid callback")
                    verifier = store.consume_flow(self.cookies().get(FLOW_COOKIE, ""), q["state"][0])
                    owner = identity(settings, q["code"][0], verifier)
                    if not settings.permits(owner):
                        raise ValueError("Account is not allowed")
                    store.revoke(self.cookies().get(SESSION_COOKIE, ""))
                    session = store.new_session(owner)
                except Exception:
                    return self.reply(403, b"Sign-in was not accepted.", headers=[("Set-Cookie", cookie(FLOW_COOKIE))])
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
                    return self.reply(200, blob, "application/json", headers=[
                        ("X-Original-SHA256", hashlib.sha256(blob).hexdigest()),
                        ("Content-Disposition", f'inline; filename="{Path(ref).name[:-3]}"')])
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
