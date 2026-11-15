"""Fetch specified public archives. Run audit.py separately for offline verification."""
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import requests
import json

BASE = "https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev/"
RUNS = {"2026-09-25": "2fe15b0976d1", "2026-09-29": "64d13a1597e1", "2026-10-01": "66314af0dcf4"}

def fetch(key):
    path = Path(sys.argv[1]) / key
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with requests.get(BASE + key, stream=True, timeout=90) as response:
            response.raise_for_status()
            data = response.raw.read()
        path.write_bytes(data)
    return path

if __name__ == "__main__":
    for day, owner in RUNS.items():
        manifest = fetch(f"moo144/tradier/{day}/manifest-{owner}.json")
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(fetch, [a["key"] for a in json.loads(manifest.read_bytes())["artifacts"]]))
