"""Command line for the candle-cache backend.

Commands (all local unless ``--publish`` is given):

- ``seed --out <dir>`` — write the bundled AAPL seed sample
  (``sample/aapl-seed.json``) into ``<out>/nasdaq/candles/AAPL/``
  as the three base interval files plus the derived eight and the
  provenance sidecar. No network, no token.
- ``read --root <dir> --symbol AAPL --interval 1m`` — print one
  stored interval file.
- ``as-of --root <dir> --symbol AAPL --interval 1m --cutoff <ISO/ms>``
  — print the cache state as of a chosen time: selected bars,
  freshness, gaps and labels (see ``cache.read_as_of``).
- ``verify --root <dir> --symbol AAPL`` — check the ticker's files
  against its provenance sidecar.
- ``publish --root <dir> --symbol AAPL`` — upload one ticker to R2.
  Opt-in only; without ``--publish`` nothing is uploaded, and
  without the R2 environment (see ``store.py``) it fails closed.

Nothing here starts a collector, a schedule or a deployment.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, cache as cache_mod, resample as resample_mod
from .store import LocalDirectoryStore, R2Store, publish_symbol

SEED_PATH = Path(__file__).resolve().parent / "sample" / "aapl-seed.json"


def _load_seed():
    with open(SEED_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def cmd_seed(args):
    """Write the bundled AAPL seed sample into ``--out``."""
    seed = _load_seed()
    store = LocalDirectoryStore(args.out)
    bases = {interval: seed["intervals"][interval]["bars"]
             for interval in ("1m", "1h", "1d")}
    derived = resample_mod.derive_all(bases)
    provenance = cache_mod.write_symbol(
        store, "AAPL", derived,
        generated_at=seed["seed"]["generated_at"],
        origin=seed["seed"]["origin"],
        available_at=seed["seed"]["first_fetched_at"],
        requests={interval: seed["intervals"][interval].get("requests", 0)
                  for interval in ("1m", "1h", "1d")},
        pages={interval: seed["intervals"][interval].get("pages", 0)
               for interval in ("1m", "1h", "1d")},
        universe=seed["seed"].get("universe"),
        first_fetched_at=seed["seed"]["first_fetched_at"],
        seed_note=seed["seed"]["note"])
    print(json.dumps({"seeded": "AAPL",
                      "generated_at": seed["seed"]["generated_at"],
                      "origin": seed["seed"]["origin"],
                      "bars_total": provenance["coverage"]["bars_total"],
                      "out": str(args.out)}, indent=2))
    return 0


def cmd_read(args):
    store = LocalDirectoryStore(args.root)
    content = cache_mod.read_interval(store, args.symbol, args.interval)
    print(json.dumps(content, indent=2))
    return 0


def cmd_as_of(args):
    store = LocalDirectoryStore(args.root)
    result = cache_mod.read_as_of(store, args.symbol, args.interval,
                                  cutoff=args.cutoff,
                                  session=args.session,
                                  origin=args.origin,
                                  max_age_ms=args.max_age_ms)
    print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_verify(args):
    store = LocalDirectoryStore(args.root)
    result = cache_mod.verify_tree(store, args.symbol)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def cmd_publish(args):
    store = LocalDirectoryStore(args.root)
    bases = cache_mod.read_bases(store, args.symbol)
    if bases is None:
        print(f"no cached bases for {args.symbol} under {args.root}",
              file=sys.stderr)
        return 1
    derived = resample_mod.derive_all(bases)
    files = {interval: cache_mod.interval_file(
        args.symbol, interval, derived[interval],
        cache_mod._now_iso()) for interval in resample_mod.INTERVALS}
    provenance = cache_mod.read_provenance(store, args.symbol)
    if provenance is None:
        print(f"no provenance sidecar for {args.symbol}; refusing to "
              "publish without one", file=sys.stderr)
        return 1
    r2 = R2Store()
    result = publish_symbol(args.symbol, files, provenance, store=r2)
    print(json.dumps(result, indent=2))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="candle-cache-backend",
        description="Local cache reader/writer for R2 candle files.")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="write the bundled AAPL seed sample")
    seed.add_argument("--out", required=True)
    seed.set_defaults(func=cmd_seed)

    read = sub.add_parser("read", help="print one stored interval file")
    read.add_argument("--root", required=True)
    read.add_argument("--symbol", required=True)
    read.add_argument("--interval", required=True)
    read.set_defaults(func=cmd_read)

    as_of = sub.add_parser("as-of", help="cache state as of a chosen time")
    as_of.add_argument("--root", required=True)
    as_of.add_argument("--symbol", required=True)
    as_of.add_argument("--interval", required=True)
    as_of.add_argument("--cutoff", required=True,
                       help="epoch ms or offset-qualified ISO timestamp")
    as_of.add_argument("--session", default=None)
    as_of.add_argument("--origin", default="recorded")
    as_of.add_argument("--max-age-ms", type=int, default=15 * 60 * 1000)
    as_of.set_defaults(func=cmd_as_of)

    verify = sub.add_parser("verify", help="check files against provenance")
    verify.add_argument("--root", required=True)
    verify.add_argument("--symbol", required=True)
    verify.set_defaults(func=cmd_verify)

    publish = sub.add_parser("publish", help="upload one ticker to R2")
    publish.add_argument("--root", required=True)
    publish.add_argument("--symbol", required=True)
    publish.set_defaults(func=cmd_publish)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
