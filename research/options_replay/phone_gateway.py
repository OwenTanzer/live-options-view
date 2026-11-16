import http.client
import secrets
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

token = Path("release-invite").read_text().strip()
cookie = Path("release-cookie").read_text().strip()
landing = b"""<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1"><title>Options Trainer</title><p id="status">Opening Options Trainer...</p><script>
const token=location.hash.slice(1);
history.replaceState(null,"","/open");
fetch("/unlock",{method:"POST",body:token}).then(r=>{if(r.ok)location.replace("/");else document.getElementById("status").textContent="Open the full invitation link to enter."}).catch(()=>document.getElementById("status").textContent="Connection failed. Reopen the invitation link.");
</script>"""

signed_out = b"""<!doctype html><html lang="en"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Options Trainer</title><style>body{max-width:32em;margin:12vh auto;padding:1rem;font:17px/1.5 system-ui}input,button{font:inherit;padding:.5em}input{max-width:90%}</style>
<a href="https://options.moopertonic.net/">Options View</a><h1>Options Trainer</h1>
<p>Open your private trainer invitation on this device, or paste it below.</p>
<form><label>Invitation link<br><input type="password" autocomplete="off" required></label> <button>Open trainer</button></form>
<p role="status"></p><script>document.querySelector('form').onsubmit=e=>{e.preventDefault();try{let u=new URL(document.querySelector('input').value.trim());
if(u.origin!==location.origin||u.pathname!='/open'||!u.hash)throw Error();location.replace('/open'+u.hash)}
catch{document.querySelector('[role=status]').textContent='Use the private trainer invitation link.'}};</script></html>"""

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
        if "__Host-trainer" not in c or not secrets.compare_digest(c["__Host-trainer"].value,cookie):
            if not post and self.path in ("/", "/spy/"):
                return self.reply(200,signed_out,"text/html; charset=utf-8")
            return self.reply(403)
        spy = self.path.startswith("/spy/")
        path = self.path[4:] if spy else self.path
        if not (path in ("/", "/shared/format.js") or path.startswith("/api/")):
            return self.reply(404)
        conn=http.client.HTTPConnection("127.0.0.1",8744 if spy else 8737,timeout=100)
        try:
            conn.request(self.command,path,body=body,headers={"Content-Type":"application/json"})
            res=conn.getresponse()
            self.reply(res.status,res.read(),res.getheader("Content-Type","application/octet-stream"))
        except OSError:
            self.reply(502)
        finally:
            conn.close()

    do_GET=dispatch
    do_POST=dispatch

ThreadingHTTPServer(("127.0.0.1",8743),Gateway).serve_forever()
