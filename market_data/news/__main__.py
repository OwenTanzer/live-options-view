"""News archive CLI.

  python -m market_data.news collect-once [--fixtures DIR] [--store DIR] [--spool DIR]
  python -m market_data.news enrich       [--store DIR] [--spool DIR]
  python -m market_data.news verify       [--store DIR]
  python -m market_data.news replay       [--store DIR]
  python -m market_data.news rebuild-index [--store DIR]
  python -m market_data.news query --as-of 2026-11-09T10:07:00Z [--mode collector|archive|publication]
                                   [--stream oil] [--start T] [--end T] [--limit N]
  python -m market_data.news join --as-of T --tolerance-s 120 [--mode collector|archive|publication]
                                  [--prices FILE] [--instrument etf.qqq] [--series underlying_quote|bar_1m|bar_1d]
                                  [--stream oil] [--start T]
  python -m market_data.news build-views --as-of T --tolerance-s 120 --label "SYNTHETIC fixture data" --out DIR
                                  [--prices FILE] [--instrument etf.qqq] [--series ...] [--date YYYY-MM-DD]
                                  [--rows-per-page N] [--page-bytes N] [--max-pages N] (limits only lower the defaults)
  python -m market_data.news sources
  python -m market_data.news prices-from-sidecars (--dir MIRROR | --date YYYYMMDD) --out FILE
      read-only: collector provenance sidecars -> last-trade underlying_quote records (midpoints excluded);
      --date lists/reads intraday/YYYYMMDD/ only, with PRICE_SIDECAR_R2_* read/list credentials.
  python -m market_data.news pilot-cycle   [--config FILE] [--private DIR] [--public DIR] [--spool DIR] [--clock T] [--live]
  python -m market_data.news publish-status|publish-stop --reason R|publish-resume|publish-withdraw|publish-reseal
                                           [--config FILE] [--private DIR] [--public DIR] [--live]
  python -m market_data.news publish-restore --gen GEN_ID [--config FILE] [--private DIR] [--public DIR] [--live]
      GEN_ID is the logical generation (YYYYMMDDTHHMMSSZ-<16 hex>), not a page namespace: the restore
      reuses a still-pinned incarnation or writes a fresh one from the snapshot.
      publish-reseal rewrites the live index with a fresh nonce (same pages): it closes open index commits.
      publish-withdraw may report withdrawal_unconfirmed; run it again until a tombstone is confirmed.

Defaults are inert: fixture feeds from tests/fixtures/news/feeds and a local
directory store. Live feeds need --network; the bucket needs --bucket plus
NEWS_ALLOW_BUCKET_WRITES=1 and the R2_* variables. No service is started.

The pilot/publish commands use a private and a public local directory by
default. --live (live feeds and the two buckets) is refused unless the pilot
config says active: true and NEWS_ALLOW_BUCKET_WRITES=1 is set.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .adapters.rss import FetchResponse, http_fetch
from .archive import (ArchiveError, LocalObjectStore, R2ObjectStore, collect_once, enrich_pending,
                      identity_state, scan_history)
from .normalize import parse_iso
from .join import SERIES, join
from .prices import PriceRecordError, load_price_file
from .reader import as_of, load_archive, rebuild_indexes
from .views import LIMITS, BuildError, build_views, limits_from, write_views
from . import checkpoint as history_checkpoint
from . import publish as pub

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "news" / "feeds"
DEFAULT_PILOT = Path(__file__).resolve().parent / "deploy" / "news-pilot.json"
DEFAULT_PRICES = REPO_ROOT / "tests" / "fixtures" / "news" / "prices" / "qqq_2026-11-09.synthetic.jsonl"


def fixture_fetcher(directory: Path):
    """Serve <source_id>.xml for a source URL; a missing file is a recorded 404."""
    by_url = {s.url: s.id for s in config.SOURCES.values()}

    def fetch(url: str, timeout: float) -> FetchResponse:
        source_id = by_url.get(url)
        path = directory / f"{source_id}.xml" if source_id else None
        if path is None or not path.is_file():
            return FetchResponse(404, b"")
        return FetchResponse(200, path.read_bytes())

    return fetch


def fixed_or_real_clock(value: str | None):
    if value:
        fixed = parse_iso(value)
        return lambda: fixed
    return lambda: datetime.now(timezone.utc)


def build_store(args):
    if args.bucket:
        import os

        if os.environ.get("NEWS_ALLOW_BUCKET_WRITES") != "1":
            raise ArchiveError("--bucket requires NEWS_ALLOW_BUCKET_WRITES=1 (explicit opt-in)")
        return R2ObjectStore.from_env()
    return LocalObjectStore(args.store)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m market_data.news", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, writes=False):
        sp.add_argument("--store", default="./news-archive-local", help="local object-store directory (default)")
        sp.add_argument("--bucket", action="store_true", help="use the R2 bucket (needs explicit opt-in env)")
        if writes:
            sp.add_argument("--spool", default="./news-spool")
            sp.add_argument("--clock", help="fixed UTC time for reproducible fixture runs")
            sp.add_argument("--break-stale-lease", action="store_true")

    c = sub.add_parser("collect-once", help="one bounded collection cycle")
    common(c, writes=True)
    c.add_argument("--fixtures", default=str(DEFAULT_FIXTURES))
    c.add_argument("--network", action="store_true", help="fetch live feeds instead of fixtures")
    c.add_argument("--streams", default=",".join(config.STREAMS))
    c.add_argument("--enable-source", action="append", default=[], help="enable a configured-but-disabled source id")
    c.add_argument("--full-replay", action="store_true", help="read every committed run; do not use or save the checkpoint")
    hc = sub.add_parser("checkpoint", help="history checkpoint: status, compare with full replay, or rebuild")
    common(hc, writes=True)
    hc.add_argument("action", choices=("status", "compare", "rebuild"))
    e = sub.add_parser("enrich", help="classify committed revisions lacking a current rules classification")
    common(e, writes=True)
    for name in ("verify", "replay", "rebuild-index"):
        common(sub.add_parser(name), writes=False)
    q = sub.add_parser("query", help="as-of read")
    common(q)
    q.add_argument("--as-of", required=True)
    q.add_argument("--mode", choices=("collector", "archive", "publication"), default="collector")
    q.add_argument("--stream")
    q.add_argument("--start")
    q.add_argument("--end")
    q.add_argument("--limit", type=int, default=50)
    jn = sub.add_parser("join", help="as-of join of news revisions to price records (read-only)")
    common(jn)
    jn.add_argument("--as-of", required=True)
    jn.add_argument("--mode", choices=("collector", "archive", "publication"), default="collector")
    jn.add_argument("--tolerance-s", type=float, required=True, help="max age of a matched price (no default)")
    jn.add_argument("--prices", default=str(DEFAULT_PRICES), help="price JSONL (default: the synthetic QQQ fixture)")
    jn.add_argument("--instrument", default="etf.qqq")
    jn.add_argument("--series", choices=tuple(SERIES), default="underlying_quote")
    jn.add_argument("--stream")
    jn.add_argument("--start")
    bv = sub.add_parser("build-views", help="write the builder's unqualified news/v1/views/ pages to a local directory "
                                            "(never the published news/v2 form)")
    common(bv)
    bv.add_argument("--as-of", required=True)
    bv.add_argument("--tolerance-s", type=float, required=True, help="max age of a matched price (no default)")
    bv.add_argument("--label", required=True, help="data label shown on every page, e.g. 'SYNTHETIC fixture data'")
    bv.add_argument("--out", required=True, help="new or empty output directory")
    bv.add_argument("--prices", default=str(DEFAULT_PRICES))
    bv.add_argument("--instrument", default="etf.qqq")
    bv.add_argument("--series", choices=tuple(SERIES), default="underlying_quote")
    bv.add_argument("--date", action="append", help="UTC date (repeatable; default: every date up to --as-of)")
    for flag in ("rows-per-page", "page-bytes", "max-pages", "segments-per-page", "index-views", "index-bytes"):
        bv.add_argument(f"--{flag}", type=int)
    sub.add_parser("sources", help="list configured sources and streams")
    sp = sub.add_parser("prices-from-sidecars", help="last-trade price records from collector provenance "
                                                   "sidecars (read-only, intraday/ only)")
    src = sp.add_mutually_exclusive_group(required=True)
    src.add_argument("--dir", help="local mirror laid out as <dir>/intraday/YYYYMMDD/*.provenance.json")
    src.add_argument("--date", action="append", help="YYYYMMDD; list+read intraday/YYYYMMDD/ (repeatable)")
    sp.add_argument("--out", required=True, help="price JSONL (header + records) for join/build-views --prices")

    def pilot_common(sp):
        sp.add_argument("--config", default=str(DEFAULT_PILOT))
        sp.add_argument("--private", default="./news-private-local", help="private store directory (raw archive + publish state)")
        sp.add_argument("--public", default="./news-public-local", help="public store directory (views only)")
        sp.add_argument("--live", action="store_true", help="live feeds and buckets (refused while the config is inactive)")
        sp.add_argument("--clock", help="fixed UTC time for reproducible local runs")
        sp.add_argument("--break-stale-lease", action="store_true",
                        help="take over an EXPIRED lease after confirming no writer or publisher is running")
    pc = sub.add_parser("pilot-cycle", help="collect the pilot sources, build views, publish (local by default)")
    pilot_common(pc)
    pc.add_argument("--spool", default="./news-spool")
    pc.add_argument("--fixtures", default=str(DEFAULT_FIXTURES))
    for name in ("publish-status", "publish-resume", "publish-withdraw", "publish-reseal"):
        pilot_common(sub.add_parser(name))
    ps = sub.add_parser("publish-stop", help="stop cycles and publication (views stay as last published)")
    pilot_common(ps)
    ps.add_argument("--reason", required=True)
    pr = sub.add_parser("publish-restore", help="republish a retained generation from its snapshot, into a "
                                                 "still-pinned or fresh incarnation (requires publish-stop)")
    pilot_common(pr)
    pr.add_argument("--gen", required=True)
    # P1 research ledger: local stores only (no --bucket option; nothing fetches or publishes).
    ri = sub.add_parser("research-import", help="import a prepared research bundle (captures + annotations) "
                                                "into a local research ledger")
    ri.add_argument("--store", required=True, help="local object-store directory")
    ri.add_argument("--bundle", required=True)
    ri.add_argument("--clock", help="fixed UTC time (annotated_at is this import's time)")
    ri.add_argument("--commit-clock", help="fixed UTC time for the run's commit record")
    ri.add_argument("--defer-commit", action="store_true", help="leave the run uncommitted (research-settle later)")
    ri.add_argument("--break-stale-lease", action="store_true")
    rs = sub.add_parser("research-settle", help="record commits for research runs that have none, stamped now")
    rs.add_argument("--store", required=True)
    rs.add_argument("--clock")
    rs.add_argument("--break-stale-lease", action="store_true")
    rv = sub.add_parser("research-view", help="write the sanitized research view at a cutoff (read-only)")
    rv.add_argument("--store", required=True)
    rv.add_argument("--cutoff", required=True)
    rv.add_argument("--mode", default="research", choices=["collector", "archive", "publication", "research"])
    rv.add_argument("--observations-store", help="local news archive whose collected revisions annotations may target")
    rv.add_argument("--out", required=True)
    args = p.parse_args(argv)

    if args.cmd == "sources":
        for s in config.SOURCES.values():
            print(f"{'ENABLED ' if s.enabled else 'disabled'}  {s.id:40s} {','.join(s.streams):18s} {s.url}")
        print(f"config_digest={config.config_digest()} rules_digest={config.rules_digest()}")
        return 0

    if args.cmd == "prices-from-sidecars":
        from .adapters import sidecar_prices as sc
        try:
            if args.dir:
                items, source = sc.local_sidecars(args.dir), f"local mirror {Path(args.dir).name}"
            else:
                for d in args.date:
                    sc.check_prefix(f"intraday/{d}/")
                bucket = sc.BucketSidecars.from_env()
                items = (kv for d in args.date for kv in bucket.sidecars(d))
                source = "bucket " + ",".join(f"intraday/{d}/" for d in args.date)
            result = sc.adapt(items, sc.SessionDeriver())
            sc.write_jsonl(result, args.out, source)
            load_price_file(args.out)  # the output must satisfy the prices.py contract as written
        except (sc.SidecarAccessError, PriceRecordError) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(result.header(source), indent=1))
        return 0

    if args.cmd.startswith("research-"):
        try:
            return _research_main(args)
        except (ArchiveError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    if args.cmd.startswith(("pilot-", "publish-")):
        try:
            return _pilot_main(args)
        except (ArchiveError, PriceRecordError, BuildError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    try:
        store = build_store(args)
        if args.cmd == "collect-once":
            fetch = http_fetch if args.network else fixture_fetcher(Path(args.fixtures))
            streams = [s for s in args.streams.split(",") if s]
            unknown = set(streams) - set(config.STREAMS)
            if unknown:
                raise ArchiveError(f"unknown streams: {sorted(unknown)}")
            report = collect_once(store, args.spool, fetch, fixed_or_real_clock(args.clock), streams=streams,
                                  extra_sources=args.enable_source, break_stale_lease=args.break_stale_lease,
                                  use_checkpoint=not args.full_replay)
            m = report.manifest
            print(json.dumps({"run_id": report.run_id, "status": m["status"], "committed_at": m["committed_at"],
                              "counts": m["counts"], "enrichment": {k: m["enrichment"][k] for k in ("status", "classified", "failed")},
                              "recovered_runs": report.recovered, "availability_settled": report.settled,
                              "index_error": report.index_error, "history": report.checkpoint,
                              "sources": [{k: s.get(k) for k in ("source_id", "status", "entries", "new_items", "new_revisions",
                                                                 "unchanged", "failed_entries", "entry_failures", "error")}
                                          for s in m["sources"] if s["status"] != "disabled"]}, indent=1))
            return 0 if m["status"] != "failed" else 1
        if args.cmd == "enrich":
            report = enrich_pending(store, args.spool, fixed_or_real_clock(args.clock), break_stale_lease=args.break_stale_lease)
            print(json.dumps({"run_id": report.run_id, "enrichment": report.manifest["enrichment"]} if report else {"pending": 0}, indent=1))
            return 0
        if args.cmd == "checkpoint":
            if args.action == "status":
                out = history_checkpoint.status(store)
            elif args.action == "compare":
                out = history_checkpoint.compare(store)
            else:
                out = history_checkpoint.rebuild(store, fixed_or_real_clock(args.clock),
                                                 break_stale_lease=args.break_stale_lease)
            print(json.dumps(out, indent=1))
            return 0 if args.action != "compare" or out["equal"] else 1
        if args.cmd == "verify":
            scan = scan_history(store)
            print(json.dumps({"committed": [{"run_id": r.manifest["run_id"], "kind": r.manifest["run_kind"],
                                             "status": r.manifest["status"], "committed_at": r.committed_at,
                                             "manifest_sha256": r.manifest_sha256}
                                            for r in scan.runs],
                              "availability_pending": scan.availability_pending,
                              "corrupt": scan.problems, "uncommitted": scan.uncommitted}, indent=1))
            return 1 if scan.problems else 0
        if args.cmd == "replay":
            scan = scan_history(store)
            state = identity_state(scan.runs)
            print(json.dumps({"committed_runs": len(scan.runs), "corrupt_runs": len(scan.problems),
                              "uncommitted_runs": len(scan.uncommitted),
                              "observations": sum(len(r.observations) for r in scan.runs),
                              "classifications": sum(len(r.classifications) for r in scan.runs),
                              "identities": len(state),
                              "objects": [{"key": m["key"], "bytes": m["bytes"], "sha256": m["sha256"]}
                                          for r in scan.runs for m in r.manifest["objects"].values()]}, indent=1))
            return 1 if scan.problems else 0
        if args.cmd == "rebuild-index":
            print(json.dumps(rebuild_indexes(store, fixed_or_real_clock(None)), indent=1))
            return 0
        if args.cmd == "query":
            print(json.dumps(as_of(load_archive(store), args.as_of, args.mode, args.stream, args.start, args.end,
                                   args.limit), indent=1, ensure_ascii=False))
            return 0
        if args.cmd == "join":
            print(json.dumps(join(load_archive(store), load_price_file(args.prices), args.as_of, args.mode,
                                  tolerance_s=args.tolerance_s, instrument_id=args.instrument, series=args.series,
                                  stream=args.stream, start=args.start), indent=1, ensure_ascii=False))
            return 0
        if args.cmd == "build-views":
            limits = limits_from(rows_per_page=args.rows_per_page, page_bytes=args.page_bytes,
                                 pages_per_view=args.max_pages, segments_per_page=args.segments_per_page,
                                 index_views=args.index_views, index_bytes=args.index_bytes)
            for field, value in vars(limits).items():
                if not 0 < value <= getattr(LIMITS, field):
                    raise BuildError(f"{field} must be between 1 and {getattr(LIMITS, field)}")
            files = build_views(load_archive(store), load_price_file(args.prices), as_of=args.as_of,
                                tolerance_s=args.tolerance_s, data_label=args.label, instrument_id=args.instrument,
                                series=args.series, dates=args.date, limits=limits)
            write_views(args.out, files)
            index = json.loads(files["news/v1/views/index.json"])
            print(json.dumps({"out": args.out, "files": len(files), "views_total": index["views_total"],
                              "views_omitted": index["views_omitted"], "complete": index["complete"],
                              "incomplete_views": [f"{v['mode']}/{v['stream']}/{v['date']}" for v in index["views"]
                                                   if not v["complete"]]}, indent=1))
            return 0
    except (ArchiveError, PriceRecordError, BuildError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 1


def _pilot_stores(args, cfg):
    import os

    if not args.live:
        return LocalObjectStore(args.private), pub.PublicViewStore(LocalObjectStore(args.public))
    if not cfg.active:
        raise ArchiveError(f"{args.config} is inactive (active: false); --live is refused")
    if os.environ.get("NEWS_ALLOW_BUCKET_WRITES") != "1":
        raise ArchiveError("--live requires NEWS_ALLOW_BUCKET_WRITES=1 (explicit opt-in)")
    st = cfg["storage"]
    return (pub.bucket_store_from_env(os.environ, st["private"]["env_prefix"]),
            pub.PublicViewStore(pub.bucket_store_from_env(os.environ, st["public"]["env_prefix"])))


def _research_main(args) -> int:
    from . import research

    store = LocalObjectStore(args.store)
    if args.cmd == "research-import":
        bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
        commit = fixed_or_real_clock(args.commit_clock) if args.commit_clock else None
        out = research.import_bundle(store, bundle, fixed_or_real_clock(args.clock), commit_clock=commit,
                                     defer_commit=args.defer_commit, break_stale_lease=args.break_stale_lease)
    elif args.cmd == "research-settle":
        out = {"settled": research.settle_research(store, fixed_or_real_clock(args.clock),
                                                   break_stale_lease=args.break_stale_lease)}
    else:
        ledger = research.load_research(store)
        if ledger.problems:
            raise ArchiveError(f"research ledger has corrupt runs: {ledger.problems}")
        observations = load_archive(LocalObjectStore(args.observations_store)).observations if args.observations_store else []
        labels = sorted({r.manifest["bundle_label"] for r in ledger.runs})
        view = research.project(research.research_view(ledger, args.cutoff, args.mode, observations),
                                " + ".join(labels) or "no research records")
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(view, indent=1, ensure_ascii=False), encoding="utf-8")
        out = {"out": args.out, "mode": view["mode"], "cutoff": view["cutoff"], "items": len(view["items"])}
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return 0


def _pilot_main(args) -> int:
    cfg = pub.load_pilot_config(args.config)
    private, public = _pilot_stores(args, cfg)
    pub.assert_separate(private, public)
    clock = fixed_or_real_clock(getattr(args, "clock", None))
    out, code = None, 0
    if args.cmd == "pilot-cycle":
        fetch = http_fetch if args.live else fixture_fetcher(Path(args.fixtures))
        out = pub.pilot_cycle(private, public, args.spool, fetch, clock, cfg,
                              break_stale_lease=args.break_stale_lease)
        code = 1 if out["status"] == "published" and out["run"]["status"] == "failed" else 0
    elif args.cmd == "publish-status":
        out = pub.status(private, public, clock, interval_s=cfg["schedule"]["interval_s"],
                         budget_s=cfg["cycle_budget_s"], policy=pub.policy_from(cfg))
        code = 0 if out["healthy"] else 1
    elif args.cmd == "publish-stop":
        out = {"stopped": pub.stop(private, clock, args.reason)}
    elif args.cmd == "publish-resume":
        out = {"resumed": pub.resume(private)}
    elif args.cmd == "publish-restore":
        out = pub.restore(private, public, args.gen, clock, policy=pub.policy_from(cfg),
                          break_stale_lease=args.break_stale_lease).summary()
    elif args.cmd == "publish-withdraw":
        out = pub.withdraw(private, public, clock, break_stale_lease=args.break_stale_lease)
        code = 1 if out["withdrawal_unconfirmed"] else 0
    elif args.cmd == "publish-reseal":
        out = pub.reseal(private, public, clock, break_stale_lease=args.break_stale_lease)
    print(json.dumps(out, indent=1, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
