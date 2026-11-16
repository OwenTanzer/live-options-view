import http.client
import hashlib
import hmac
import os
import re
import secrets
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

token = Path("release-invite").read_text().strip()
cookie = Path("release-cookie").read_text().strip()
keyfile = Path("browser-signing-key")
if not keyfile.exists():
    with os.fdopen(os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
        out.write(secrets.token_bytes(32))
signing_key = keyfile.read_bytes()
if len(signing_key) != 32:
    raise RuntimeError("Invalid browser signing key")
legacy_file = Path("legacy-workspaces")
legacy_workspaces = set(legacy_file.read_text().splitlines()) if legacy_file.exists() else set()

def signed_browser(identity):
    return identity + "." + hmac.digest(signing_key, identity.encode(), "sha256").hex()

landing = b"""<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1"><title>Options Trainer</title><p id="status">Opening Options Trainer...</p><script>
const token=location.hash.slice(1);
history.replaceState(null,"","/open");
if (!token) location.replace("/"); else fetch("/unlock",{method:"POST",body:token}).then(()=>location.replace("/")).catch(()=>document.getElementById("status").textContent="Connection failed. Reopen the invitation link.");
</script>"""

class Gateway(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def reply(self,status,body=b"",mime="text/plain",authenticated=False):
        self.send_response(status)
        self.send_header("Content-Type",mime)
        self.send_header("Content-Length",str(len(body)))
        self.send_header("Cache-Control","no-store")
        self.send_header("Referrer-Policy","no-referrer")
        self.send_header("X-Frame-Options","DENY")
        if authenticated:
            self.send_header("Set-Cookie","__Host-trainer="+cookie+"; Path=/; Secure; HttpOnly; SameSite=Strict")
        if getattr(self, "new_browser", None):
            self.send_header("Set-Cookie", "__Host-practice="+self.new_browser+
                             "; Max-Age=31536000; Path=/; Secure; HttpOnly; SameSite=Lax")
        self.end_headers()
        self.wfile.write(body)

    def dispatch(self):
        post=self.command=="POST"
        if post and self.headers.get("Origin") and urlsplit(self.headers["Origin"]).netloc != self.headers.get("Host"):
            return self.reply(403)
        try:
            length=int(self.headers.get("Content-Length","0"))
        except ValueError:
            return self.reply(400)
        if not 0<=length<=8192:
            return self.reply(413)
        body=self.rfile.read(length) if post else None
        if self.path=="/open" and not post:
            return self.reply(200,landing,"text/html; charset=utf-8")
        if self.path=="/unlock" and post:
            valid=secrets.compare_digest(body,token.encode())
            return self.reply(200 if valid else 403,authenticated=valid)
        c=SimpleCookie()
        try:
            c.load(self.headers.get("Cookie",""))
        except ValueError:
            return self.reply(403)
        headers = {"Content-Type": "application/json"}
        legacy = c.get("__Host-trainer")
        if not legacy or not secrets.compare_digest(legacy.value, cookie):
            browser = c.get("__Host-practice")
            value = browser.value if browser else ""
            identity = value.split(".", 1)[0]
            valid_id = re.fullmatch(r"[0-9a-f]{64}", identity)
            if valid_id and re.fullmatch(r"[0-9a-f]{64}\.[0-9a-f]{64}", value) and secrets.compare_digest(value, signed_browser(identity)):
                pass
            elif valid_id and value == identity and hashlib.sha256(identity.encode()).hexdigest() in legacy_workspaces:
                self.new_browser = signed_browser(identity)
            elif not post and self.path in ("/", "/spy/"):
                identity = secrets.token_hex(32)
                self.new_browser = signed_browser(identity)
            else:
                return self.reply(403)
            headers["X-Practice-Workspace"] = hashlib.sha256(identity.encode()).hexdigest()
        spy = self.path.startswith("/spy/")
        path = self.path[4:] if spy else self.path
        if not (path in ("/", "/shared/format.js") or path.startswith("/api/")):
            return self.reply(404)
        conn=http.client.HTTPConnection("127.0.0.1",8744 if spy else 8737,timeout=100)
        try:
            conn.request(self.command,path,body=body,headers=headers)
            res=conn.getresponse()
            self.reply(res.status,res.read(),res.getheader("Content-Type","application/octet-stream"))
        except OSError:
            self.reply(502)
        finally:
            conn.close()

    do_GET=dispatch
    do_POST=dispatch

ThreadingHTTPServer(("127.0.0.1",8743),Gateway).serve_forever()
