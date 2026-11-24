"""Install an exact Git revision on both hosts with the minute timers stopped."""
import argparse
import base64
from datetime import date
from hashlib import sha256
import io
from pathlib import Path
import os
import subprocess
import zipfile

parser = argparse.ArgumentParser()
parser.add_argument("revision")
parser.add_argument("--calendar-python", required=True)
args = parser.parse_args()
revision = subprocess.check_output(["git", "rev-parse", "--verify", args.revision + "^{commit}"], text=True).strip()
if args.revision != revision:
    parser.error("Supply the full verified commit")
root = Path.home() / ".local/share/optionsview-panel-minute"
hourly = root.parent / "optionsview-panel-publishers"
units = Path.home() / ".config/systemd/user"
os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=" + os.environ["XDG_RUNTIME_DIR"] + "/bus")
for feed in ("crypto", "nasdaq"):
    for kind in ("timer", "service"):
        name = f"optionsview-minute@{feed}.{kind}"
        if subprocess.run(["systemctl", "--user", "is-active", "--quiet", name]).returncode == 0:
            raise SystemExit("Stop the minute units before changing their revision")
        if kind == "timer" and subprocess.run(["systemctl", "--user", "is-enabled", "--quiet", name],
                                               stderr=subprocess.DEVNULL).returncode == 0:
            raise SystemExit("Disable the minute timers before staging")

paths = ["market_data/__init__.py", "market_data/panel_minute.py", "market_data/panel_prices.py",
         "market_data/crypto_collector", "market_data/nasdaq_collector", "ops/panel-minute", "site/js/panel-minute.js"]
archive = subprocess.check_output(["git", "archive", "--format=zip", revision, "--", *paths])
digest = sha256(archive).hexdigest()
root.mkdir(parents=True, exist_ok=True, mode=0o700)
app = root / "releases" / revision
with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
    bundle.extractall(app)
installer = """
from pathlib import Path
import sys, base64, io, zipfile, hashlib
root=Path.home()/'AppData/Local/OptionsViewMinute'
data=base64.b64decode(sys.stdin.buffer.read())
assert hashlib.sha256(data).hexdigest()==DIGEST
zipfile.ZipFile(io.BytesIO(data)).extractall(root/'releases'/REVISION)
print(REVISION)
""".replace("DIGEST",repr(digest)).replace("REVISION",repr(revision))
def pycmd(source):
    encoded = base64.b64encode(source.encode()).decode()
    return 'C:\\Python314\\python.exe -c "import base64;exec(base64.b64decode(\'' + encoded + '\'))"'
receipt = subprocess.run([str(hourly/"ssh-msi"),pycmd(installer)],input=base64.b64encode(archive),
                         capture_output=True,check=True,timeout=45)
if receipt.stdout.decode().strip() != revision:
    raise RuntimeError("MSI staging receipt mismatch")
launcher = """
import os,sys
from pathlib import Path
root=Path.home()/'AppData/Local/OptionsViewMinute'
os.chdir(root/'releases'/REVISION)
from market_data.panel_minute import main
sys.argv=['panel_minute','capture','crypto','--root',str(root),'--baseline',str(root.parent/'OptionsViewCryptoCapture/data/crypto/latest.json')]
main()
""".replace("REVISION",repr(revision))
(root / "msi-command.txt").write_text(pycmd(launcher))
today = date.today()
with (root / "sessions.tmp").open("w") as target:
    subprocess.run([args.calendar_python, str(app / "ops/panel-minute/build_sessions.py"),
                    f"{today.year}-01-01", f"{today.year+2}-12-31"],stdout=target,check=True)
(root / "sessions.tmp").replace(root / "sessions.json")
link = root / "app.next"
link.unlink(missing_ok=True)
link.symlink_to(app, target_is_directory=True)
link.replace(root / "app")
for file in ("optionsview-minute@.timer","optionsview-minute@.service"):
    (units / file).write_bytes((app / "ops/panel-minute" / file).read_bytes())
subprocess.run(["systemctl","--user","daemon-reload"],check=True)
print(f"Staged {revision} on Linux and MSI; archive SHA256 {digest}")
