"""Cache public daily QQQ chain and bar CSVs for a bounded historical interval."""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def request(key):
    return urlopen(
        Request(f"{ORIGIN}/{key}", headers={"User-Agent": "curl/8.0"}), timeout=30
    )


ORIGIN = "https://pub-4d5c916b8cb74ffb8c0abd7dfadb02cf.r2.dev"


def fetch_day(args):
    root, day = args
    stamp = day.replace("-", "")
    for kind, keys in [
        ("chain", [f"raw/qqq_chain_{stamp}.csv", f"raw/opex/qqq_chain_{stamp}.csv"]),
        ("bars", [f"prices/qqq_intraday_{stamp}.csv"]),
    ]:
        dest = root / day / f"{kind}.csv"
        if dest.is_file():
            continue
        for key in keys:
            try:
                with request(key) as response:
                    body = response.read()
            except HTTPError as exc:
                if exc.code == 404:
                    continue
                raise
            dest.parent.mkdir(parents=True, exist_ok=True)
            temp = dest.with_suffix(".tmp")
            temp.write_bytes(body)
            temp.replace(dest)
            break
    return day


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cache", type=Path)
    parser.add_argument("--start", default="2026-04-01")
    parser.add_argument("--end", default="2026-09-18")
    args = parser.parse_args()
    with request("manifest.json") as response:
        dates = sorted(
            {d for d in json.load(response)["dates"] if args.start <= d <= args.end}
        )
    args.cache.mkdir(parents=True, exist_ok=True)
    (args.cache / "dates.json").write_text(json.dumps(dates))
    with ThreadPoolExecutor(max_workers=4) as pool:
        print(
            f"Checked {len(list(pool.map(fetch_day, ((args.cache, d) for d in dates))))} sessions"
        )
