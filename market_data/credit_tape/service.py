"""OA-242 isolated credit-tape service: one slice-only collector + gated panel.

One process, one private persistent archive (CREDIT_TAPE_ARCHIVE, a mounted
volume). The collector thread runs the DTCC slice-index route only (ticker
output off). The panel/API/raw/export server is the GitHub-gated
serve_private handler; only /healthz and /healthz/collector answer without a
session, and they carry status, never trade content.

Fails closed: refuses to start unless the GitHub gate is fully configured
(CREDIT_TAPE_DEV_LOCAL is rejected here). If the collector thread dies the
process exits non-zero so the platform restarts it; publishing is crash-safe
(see slices.py), so a restart at any point loses nothing and duplicates nothing.

Usage: python -u -m market_data.credit_tape.service
Persistent storage: CREDIT_TAPE_ARCHIVE must be inside a mounted volume named by
CREDIT_TAPE_VOLUME (or Railway's RAILWAY_VOLUME_MOUNT_PATH); startup is refused
if the volume is absent or not a mount point (see volume.py).

Env: CREDIT_TAPE_ARCHIVE, CREDIT_TAPE_VOLUME, CREDIT_TAPE_PUBLIC_ORIGIN, CREDIT_TAPE_GITHUB_CLIENT_ID,
     CREDIT_TAPE_GITHUB_CLIENT_SECRET, CREDIT_TAPE_GITHUB_ALLOWED_IDS,
     PORT (default 8790), CREDIT_TAPE_INTERVAL (seconds, default 20).
"""
from __future__ import annotations

import os
import signal
import sys
import threading
import traceback
from http.server import ThreadingHTTPServer

from market_data.credit_tape import capture
from market_data.credit_tape import serve_private as sp
from market_data.credit_tape import treu, volume, storage


def config(env=os.environ, is_mount=os.path.ismount):
    if env.get("CREDIT_TAPE_DEV_LOCAL"):
        raise ValueError("CREDIT_TAPE_DEV_LOCAL is for local demos only; unset it for the service")
    settings = sp.Settings.from_env(env)  # raises on any missing/invalid gate setting
    # Production writes only into a mounted persistent volume; an absent volume refuses startup
    # instead of silently archiving into the container's temporary filesystem.
    volume.require_volume(str(settings.archive), env, is_mount)
    interval = float(env.get("CREDIT_TAPE_INTERVAL", "20"))
    if not 5 <= interval <= 300:
        raise ValueError("CREDIT_TAPE_INTERVAL must be 5..300 seconds")
    port = int(env.get("PORT", "8790"))
    return settings, interval, port


def main(env=os.environ, is_mount=os.path.ismount) -> int:
    try:
        settings, interval, port = config(env, is_mount)
    except (KeyError, ValueError) as exc:
        name = exc.args[0] if isinstance(exc, KeyError) else str(exc)
        print(f"credit-tape service not started: configuration incomplete or invalid ({name})", flush=True)
        return 2
    archive = str(settings.archive)
    stop = threading.Event()
    capture_stop = threading.Event()
    failed = {}
    root = volume.volume_root(env)
    paused = storage.status(root)["pause_writers"]
    # At capacity, even creating a probe can fail. Existing evidence is readable.
    if paused:
        ident = volume.read_identity(archive)
        if not ident:
            print("Low space and no readable archive identity", flush=True)
            return 2
    else:
        os.makedirs(archive, exist_ok=True)
        try:
            ident = volume.ensure_identity(archive)
        except ValueError as exc:
            print(f"credit-tape service not started: {exc}", flush=True)
            return 2

    def collect():
        try:
            capture.run(archive, interval, ticker=False, slices=True, stop=capture_stop)
        except BaseException:  # noqa: BLE001 - any collector death must take the process down
            failed["error"] = traceback.format_exc()

    srv = ThreadingHTTPServer(("0.0.0.0", port), sp.make_handler(settings))
    worker = threading.Thread(target=collect, name="credit-tape-collector", daemon=True)

    def on_term(*_):
        stop.set()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    web = threading.Thread(target=srv.serve_forever, name="credit-tape-panel", daemon=True)
    web.start()
    archiver = None
    bucket = env.get("CREDIT_TAPE_INDEX_BUCKET")
    if bucket:
        archiver = threading.Thread(target=storage.archive_days, args=(archive, ident["archive_id"], stop, bucket),
                                    name="credit-index-archiver", daemon=True)
        archiver.start()
    uk_worker = threading.Thread(target=treu.run, args=(settings.archive, capture_stop),
                                 name="tradeweb-treu", daemon=True)
    writers_started = not paused
    if writers_started:
        worker.start()
        uk_worker.start()
    print(f"credit-tape service: archive_id={ident['archive_id']}, volume={root}, paused={paused}", flush=True)
    while not stop.wait(1.0):
        space = storage.status(root)
        if paused:
            if space["available_bytes"] >= storage.RESUME:
                # Restart cleanly so all worker state is rebuilt through the normal recovery path.
                failed["error"] = "disk reserve restored; restarting capture"
                break
        elif space["pause_writers"]:
            paused = True
            capture_stop.set()
            print("Credit Tape capture paused at disk reserve; serving the retained tape", flush=True)
        elif not worker.is_alive() or not uk_worker.is_alive():
            failed.setdefault("error", "collector worker exited")
            break
    capture_stop.set()
    stop.set()
    if writers_started:
        worker.join(timeout=interval + 90)
        uk_worker.join(timeout=20)
    if archiver:
        archiver.join(timeout=10)
    srv.shutdown()
    srv.server_close()
    if failed or (not stop.is_set()):
        print("credit-tape collector stopped unexpectedly:\n" + failed.get("error", "thread exited"), flush=True)
        return 1
    print("credit-tape service stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
