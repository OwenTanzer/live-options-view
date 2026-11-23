"""CLI for the NASDAQ-100 collector.

``collect`` is the hourly run: fetch the 101 listings, the NDX index,
per-ticker news and candles, build ``nasdaq/latest.json`` and print a
coverage summary. It writes local files only with ``--out`` and publishes
to R2 only with ``--publish`` (opt-in; see ``publish.py`` for the env vars,
which are unconfirmed against live service config). ``universe`` prints the
embedded constituent list without touching any provider.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .collector import collect
from .provenance import build_provenance
from .publish import publish_snapshot, read_config
from .symbols import EXPECTED_LISTINGS, SOURCE_AS_OF, SOURCE_LABEL, symbols, validate


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="market_data.nasdaq_collector",
        description="NASDAQ-100 snapshot collector (public sources only).")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    uni = sub.add_parser("universe", help="print the embedded 101-listing universe (no provider calls)")
    uni.add_argument("--json", action="store_true", help="print as JSON")

    col = sub.add_parser("collect", help="fetch quotes/news/candles and build the snapshot")
    col.add_argument("--out", type=Path, default=None,
                     help="write latest.json + latest.provenance.json into this directory")
    col.add_argument("--publish", action="store_true",
                     help="opt-in: publish both artifacts to R2 (env: R2_ENDPOINT_URL, "
                          "R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, NASDAQ_R2_BUCKET)")
    col.add_argument("--symbols", default=None,
                     help="comma-separated subset of tickers (default: all 101)")
    col.add_argument("--no-news", action="store_true", help="skip Google News RSS")
    col.add_argument("--no-candles", action="store_true", help="skip Yahoo Finance candles")
    col.add_argument("--max-workers", type=int, default=10)

    args = parser.parse_args(argv)
    if args.command == "universe":
        validate()
        if args.json:
            print(json.dumps({"source": SOURCE_LABEL, "as_of": SOURCE_AS_OF,
                              "listings": EXPECTED_LISTINGS, "symbols": symbols()}))
        else:
            print(f"# NASDAQ-100 universe: {EXPECTED_LISTINGS} listings "
                  f"(source: {SOURCE_LABEL}, as of {SOURCE_AS_OF})")
            for symbol in symbols():
                print(symbol)
        return 0

    selected = None
    if args.symbols:
        selected = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        unknown = sorted(set(selected) - set(symbols()))
        if unknown:
            print(f"unknown NASDAQ-100 symbols: {', '.join(unknown)}", file=sys.stderr)
            return 2
    snapshot, stats = collect(symbols=selected, include_news=not args.no_news,
                              include_candles=not args.no_candles,
                              max_workers=args.max_workers)
    coverage = snapshot["coverage"]
    print(json.dumps({"as_of": snapshot["as_of"],
                      "index": snapshot["index"]["symbol"],
                      "index_status": snapshot["index"]["status"],
                      "market_status": snapshot["index"]["market_status"],
                      "coverage": coverage, "stats": stats}, indent=2))
    provenance = build_provenance(
        snapshot,
        publication_status="not_published",
        publication_reason="local run: --publish not given")
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "latest.json").write_text(json.dumps(snapshot, indent=2))
        (args.out / "latest.provenance.json").write_text(json.dumps(provenance, indent=2))
        print(f"wrote {args.out / 'latest.json'} and {args.out / 'latest.provenance.json'}")
    if args.publish:
        try:
            config = read_config()
        except RuntimeError as exc:
            print(f"publish refused: {exc}", file=sys.stderr)
            return 2
        published_at = datetime.now(timezone.utc).isoformat()
        provenance = build_provenance(
            snapshot, publication_status="published",
            publication_reason=None, latest_published_at=published_at)
        result = publish_snapshot(snapshot, provenance, config=config)
        print(f"published to R2 bucket {result['bucket']}: "
              f"{', '.join(result['keys'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
