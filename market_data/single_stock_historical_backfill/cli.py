"""CLI for the single-stock historical backfill collector.

``backfill`` is the first fill: fetch the three Tiingo bases for the
live NASDAQ-100 listings (or a ``--symbols`` subset), derive the other eight
intervals, write the per-ticker interval files plus provenance
sidecar into ``--out`` (the root of the ``nasdaq/candles/`` R2 key
tree), and publish to R2 only with ``--publish`` (opt-in; see
``publish.py`` for the env vars, which are unconfirmed against live
service config). ``update`` is the incremental run: only bars newer
than the stored ones are fetched. ``status`` reads the checkpoint
only — no provider calls. ``universe`` re-fetches Nasdaq's official
list-type endpoint and prints the live constituent list with its
membership changes (falling back to the embedded 101 on a bad
fetch).

A full backfill is 606 Tiingo requests (6 per listing) and takes
~12 hours at the free-Starter 50 requests/hour budget. The
checkpoint (``<out>/.backfill-checkpoint.json``) records per-symbol
progress, so a run can be stopped and resumed; completed symbols are
skipped. ``--max-requests`` caps a run (smoke runs, budget safety):
the run stops cleanly once the cap is reached, checkpoint intact.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .collector import live_universe, resolve_universe, run as run_collector
from .pacing import CheckpointStore, RateLimiter
from .publish import publish_symbol, read_config
from .symbols import EXPECTED_LISTINGS, SOURCE_AS_OF, SOURCE_LABEL, symbols, validate
from .tiingo import TiingoError, TiingoRateLimited, read_token


def _parse_symbols(value):
    if not value:
        return None
    selected = [s.strip().upper() for s in value.split(",") if s.strip()]
    unknown = sorted(set(selected) - set(symbols()))
    if unknown:
        print(f"unknown NASDAQ-100 symbols: {', '.join(unknown)}", file=sys.stderr)
        return None
    return selected


def _publish_hook_factory(config, out_dir):
    """Build the per-symbol publish callback for ``--publish`` runs.

    Rebuilds the provenance sidecar as ``published`` only after the
    R2 writes for the ticker succeed, then rewrites the local sidecar
    so the local tree agrees with what was published.
    """
    from .collector import ticker_dir

    def on_symbol_done(symbol, files, provenance):
        result = publish_symbol(symbol, files, provenance, config=config)
        directory = ticker_dir(Path(out_dir), symbol)
        for interval, body in result["files"].items():
            (directory / f"{interval}.json").write_bytes(body)
        (directory / "provenance.json").write_text(
            json.dumps(result["provenance"], indent=2))
        print(f"published {symbol} to R2 bucket {result['bucket']}: "
              f"{len(result['keys'])} keys")
    return on_symbol_done


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="market_data.single_stock_historical_backfill",
        description="Single-stock historical candle backfill (Tiingo).")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    uni = sub.add_parser("universe", help="print the live NASDAQ-100 universe (re-fetched; falls back to the embedded 101 on a bad fetch)")
    uni.add_argument("--json", action="store_true", help="print as JSON")

    parsers = {}
    for name, help_text in (
            ("backfill", "fetch the three Tiingo bases, derive the other intervals, write files"),
            ("update", "fetch only bars newer than the stored ones, re-derive, rewrite files")):
        cmd = sub.add_parser(name, help=help_text)
        parsers[name] = cmd
        cmd.add_argument("--out", type=Path, default=Path("backfill-out"),
                         help="local output root (default: backfill-out)")
        cmd.add_argument("--state", type=Path, default=None,
                         help="checkpoint file (default: <out>/.backfill-checkpoint.json)")
        cmd.add_argument("--publish", action="store_true",
                         help="opt-in: publish each ticker to R2 as it completes "
                              "(env: R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, "
                              "R2_SECRET_ACCESS_KEY, TIINGO_R2_BUCKET)")
        cmd.add_argument("--symbols", default=None,
                         help="comma-separated subset of tickers (default: all 101)")
        cmd.add_argument("--pages-1m", type=int, default=3,
                         help="1-minute pages to walk back (default 3, ~75 trading days)")
        cmd.add_argument("--max-requests", type=int, default=None,
                         help="stop cleanly after this many Tiingo requests (smoke runs)")
    parsers["backfill"].add_argument(
        "--force", action="store_true",
        help="re-fetch symbols the checkpoint already marks complete")

    status = sub.add_parser("status", help="show checkpoint progress and rate budget (no provider calls)")
    status.add_argument("--out", type=Path, default=Path("backfill-out"),
                        help="local output root (default: backfill-out)")
    status.add_argument("--state", type=Path, default=None,
                        help="checkpoint file (default: <out>/.backfill-checkpoint.json)")

    args = parser.parse_args(argv)
    if args.command == "universe":
        resolution = live_universe()
        live_symbols = [s for s, _ in resolution["universe"]]
        if args.json:
            print(json.dumps({"source": SOURCE_LABEL, "as_of": SOURCE_AS_OF,
                              "membership_source": resolution["source"],
                              "resolution_as_of": resolution["as_of"],
                              "listings": len(live_symbols),
                              "symbols": live_symbols,
                              "added": [{"symbol": s, "name": n}
                                        for s, n in resolution["added"]],
                              "removed": [{"symbol": s, "name": n}
                                          for s, n in resolution["removed"]],
                              "error": resolution["error"]}))
        else:
            print(f"# NASDAQ-100 universe: {len(live_symbols)} listings "
                  f"(source: {SOURCE_LABEL}, as of {SOURCE_AS_OF}; "
                  f"membership: {resolution['source']})")
            for symbol in live_symbols:
                print(symbol)
            if resolution["added"] or resolution["removed"]:
                print(f"# added: {', '.join(s for s, _ in resolution['added']) or 'none'}; "
                      f"removed: {', '.join(s for s, _ in resolution['removed']) or 'none'}")
            if resolution["error"]:
                print(f"# fallback: {resolution['error']}", file=sys.stderr)
        return 0

    if args.command == "status":
        state_path = args.state or (args.out / ".backfill-checkpoint.json")
        checkpoint = CheckpointStore(state_path)
        checkpoint.load()
        report = {
            "checkpoint": str(state_path),
            "budget": checkpoint.limiter.budget_report(),
            "progress": checkpoint.summary(),
        }
        print(json.dumps(report, indent=2))
        return 0

    selected = _parse_symbols(args.symbols)
    if args.symbols and selected is None:
        return 2
    state_path = args.state or (args.out / ".backfill-checkpoint.json")

    # The token and (for --publish) the R2 config are validated before
    # any fetching starts, so a run never burns rate budget and then
    # fails on credentials.
    try:
        read_token()
    except RuntimeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    config = None
    on_symbol_done = None
    if args.publish:
        try:
            config = read_config()
        except RuntimeError as exc:
            print(f"publish refused: {exc}", file=sys.stderr)
            return 2
        on_symbol_done = _publish_hook_factory(config, args.out)

    checkpoint = CheckpointStore(state_path)
    checkpoint.load()
    limiter = checkpoint.limiter
    if args.max_requests is not None:
        # A request cap is enforced by wrapping the limiter: once this
        # run has spent this many requests, further acquires raise a
        # clean stop instead of sleeping for the next hour.
        original_acquire = limiter.acquire
        spent = {"count": 0}

        def capped_acquire():
            if spent["count"] >= args.max_requests:
                raise _RequestCapReached(
                    f"request cap reached ({args.max_requests}); stopping cleanly")
            waited = original_acquire()
            spent["count"] += 1
            return waited

        limiter.acquire = capped_acquire

    try:
        # Membership is dynamic: unless an explicit --symbols subset
        # is given, the live NASDAQ-100 list is re-fetched first and
        # only its members are collected; a bad list fetch falls back
        # to the embedded 101 and records that fallback in every
        # sidecar's universe block.
        if selected is None:
            resolution = resolve_universe()
            selected = [s for s, _ in resolution["universe"]]
            if resolution["error"]:
                print(f"universe fallback: embedded 101 "
                      f"({resolution['error']})", file=sys.stderr)
            elif resolution["added"] or resolution["removed"]:
                print(f"universe changes: "
                      f"+{', '.join(s for s, _ in resolution['added']) or 'none'} "
                      f"-{', '.join(s for s, _ in resolution['removed']) or 'none'}",
                      file=sys.stderr)
        else:
            resolution = None
        summary = run_collector(
            selected, args.out, mode=args.command,
            checkpoint=checkpoint, pages_1m=args.pages_1m,
            on_symbol_done=on_symbol_done,
            force=getattr(args, "force", False),
            universe_resolution=resolution)
    except _RequestCapReached as exc:
        checkpoint.save()
        print(f"stopped: {exc} (checkpoint saved; resume with the same command)",
              file=sys.stderr)
        return 0
    except TiingoRateLimited:
        # The run already retried once after the server's Retry-After
        # and aborted with the checkpoint saved (see collector.run).
        # A traceback would add nothing: the remedy is to wait out the
        # hourly budget and resume with the same command.
        print("stopped: Tiingo rate limit hit twice (HTTP 429); checkpoint "
              "saved — wait for the hourly budget to reset, then resume "
              "with the same command", file=sys.stderr)
        return 1
    except TiingoError as exc:
        print(f"stopped: {exc} (checkpoint saved; resume with the same "
              "command)", file=sys.stderr)
        return 1
    print(json.dumps(summary, indent=2))
    return 0


class _RequestCapReached(RuntimeError):
    """Internal: the --max-requests cap was reached; stop cleanly."""


if __name__ == "__main__":
    sys.exit(main())
