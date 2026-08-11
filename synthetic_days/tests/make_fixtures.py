#!/usr/bin/env python3
"""Generate the small, deterministic, checked-in fixture this repo's CI runs
`day_generator.py`/`validate_stats.py`/`backtest_bridge.py` against, instead
of needing real R2 credentials at CI time.

**Pulled from the real, live `qqq-options-chain-data` R2 bucket, not
synthesized.** The prior version of this script generated the "real"
comparison fixture with its own stochastic-volatility process (EWMA vol +
Student-t innovations) -- flagged in review as not a credible gold standard:
that process could (and did) run away into an unstable vol regime, producing
a checked-in "real" 5-min-bar fixture with a 1.99% per-bar standard
deviation and a 21.7% average daily range, both roughly an order of
magnitude past anything QQQ has ever actually done intraday, while every
`validate_stats.py` acceptance check still passed against it. A generator
being validated against a fixture that is itself unrealistic proves nothing.

This script instead pulls a compact, real slice of each of the three real
sources `r2_sources.py` reads in production -- actual 5-min bars, actual EOD
0DTE chains, actual intraday collector snapshots -- and checks that slice in
verbatim. Requires R2 credentials to *run*; the checked-in output does not,
which is what keeps CI hermetic. Re-run this script to refresh the fixture
against a more recent real window; it is not deterministic run-to-run (real
market data isn't), which is fine -- `validate_stats.py`'s absolute sanity
bounds (see its module docstring) exist specifically so a corrupted or
truncated re-pull would fail loudly rather than silently passing.

    R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... \
        python tests/make_fixtures.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from r2_sources import R2Source  # noqa: E402

ET = ZoneInfo("America/New_York")
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "r2_cache"

FIVE_MIN_YEAR = 2026
N_DAYS = 25          # trailing real sessions kept from that year's bar file
N_DONOR_DAYS = 10     # real EOD 0DTE chain days checked in as smile/OI donors
N_INTRADAY_DAYS = 6    # real intraday collector days checked in -- needs >=2 so validate_stats.py can hold one out
                        # of calibration for an out-of-sample option-path check and still have enough real
                        # samples left in the remaining days to populate the narrow ATM moneyness bucket
INTRADAY_SNAPSHOTS_PER_DAY = 30  # matches day_generator.py's own default subsampling


def make_5min_bars(src: R2Source) -> None:
    """Trailing N_DAYS real trading sessions from the real bucket's
    multi-year 5-min bar file, trimmed to keep the checked-in fixture
    compact -- not re-synthesized."""
    raw = src._get_bytes(f"history/QQQ/5/{FIVE_MIN_YEAR}.json")
    if raw is None:
        print(f"No real history/QQQ/5/{FIVE_MIN_YEAR}.json in R2 -- set R2 credentials and retry.", file=sys.stderr)
        sys.exit(1)
    bars = json.loads(raw)
    t = bars["t"]

    # Real bars can include partial/after-hours prints; keep only the
    # regular 09:30-16:00 ET session's bars, same filter validate_stats.py's
    # split_into_sessions applies to the "real" comparison series, so the
    # fixture and the code that reads it agree on what counts as a session.
    def in_session(ts: int) -> bool:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(ET)
        return dt.weekday() < 5 and (9, 30) <= (dt.hour, dt.minute) < (16, 0)

    keep_idx = [i for i, ts in enumerate(t) if in_session(ts)]
    # Trailing N_DAYS sessions: find the boundary by distinct ET dates, not
    # just the last 78*N_DAYS bars, so a short/holiday session near the tail
    # doesn't silently shrink the window below N_DAYS real days.
    dates_seen: list[str] = []
    for i in keep_idx:
        d = datetime.fromtimestamp(t[i], tz=timezone.utc).astimezone(ET).date().isoformat()
        if not dates_seen or dates_seen[-1] != d:
            dates_seen.append(d)
    keep_dates = set(dates_seen[-N_DAYS:])
    final_idx = [
        i for i in keep_idx
        if datetime.fromtimestamp(t[i], tz=timezone.utc).astimezone(ET).date().isoformat() in keep_dates
    ]

    trimmed = {k: [bars[k][i] for i in final_idx] for k in ("t", "o", "h", "l", "c", "v")}
    out_dir = FIXTURE_DIR / "history" / "QQQ" / "5"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{FIVE_MIN_YEAR}.json").write_text(json.dumps(trimmed))
    print(f"wrote {len(keep_dates)} real trading sessions of 5-min bars ({len(final_idx)} bars)")


def make_options_0dte(src: R2Source) -> None:
    """N_DONOR_DAYS real EOD 0DTE chain days, pulled verbatim from the real
    manifest -- most recent days first, so the donor set stays close in time
    to the 5-min bar fixture above."""
    manifest = src.options_0dte_manifest("QQQ")
    days = sorted(manifest.get("days", {}))
    if not days:
        print("No real history/QQQ/options_0dte/manifest.json in R2.", file=sys.stderr)
        sys.exit(1)
    chosen = days[-N_DONOR_DAYS:]

    out_dir = FIXTURE_DIR / "history" / "QQQ" / "options_0dte"
    out_dir.mkdir(parents=True, exist_ok=True)
    fixture_manifest = {"symbol": "QQQ", "days": {}}
    for d in chosen:
        raw = src._get_bytes(f"history/QQQ/options_0dte/{d}.json")
        if raw is None:
            continue
        (out_dir / f"{d}.json").write_text(raw.decode())
        fixture_manifest["days"][d] = {"contracts": len(json.loads(raw).get("strike", []))}
    (out_dir / "manifest.json").write_text(json.dumps(fixture_manifest))
    print(f"wrote {len(fixture_manifest['days'])} real EOD donor chain days")


def make_intraday_days(src: R2Source) -> None:
    """N_INTRADAY_DAYS real intraday collector days, subsampled to
    INTRADAY_SNAPSHOTS_PER_DAY real snapshots each (roughly every 30 min
    across the real session) -- real chain rows, just fewer of them, to keep
    the checked-in fixture compact."""
    available = src.intraday_days_available()
    if not available:
        print("No real intraday/ days in R2.", file=sys.stderr)
        sys.exit(1)
    chosen_days = available[-N_INTRADAY_DAYS:]

    intraday_root = FIXTURE_DIR / "intraday"
    intraday_root.mkdir(parents=True, exist_ok=True)
    listing = []
    for yyyymmdd in chosen_days:
        snap_keys = src.intraday_snapshots(yyyymmdd)
        if not snap_keys:
            continue
        # Real filenames encode HHMMSSffffff in ET-naive collector wall
        # time; keep every Nth one, evenly spaced, rather than the first N
        # (which would all land pre-market).
        stride = max(1, len(snap_keys) // INTRADAY_SNAPSHOTS_PER_DAY)
        picked = snap_keys[::stride][:INTRADAY_SNAPSHOTS_PER_DAY]

        day_dir = intraday_root / yyyymmdd
        day_dir.mkdir(parents=True, exist_ok=True)
        day_listing = []
        for key in picked:
            raw = src._get_bytes(key)
            if raw is None:
                continue
            filename = key.rsplit("/", 1)[-1]
            (day_dir / filename).write_bytes(raw)
            local_key = f"intraday/{yyyymmdd}/{filename}"
            day_listing.append(local_key)
        (day_dir / "_listing.json").write_text(json.dumps(day_listing))
        listing.extend(day_listing)
    (intraday_root / "_listing.json").write_text(json.dumps(listing))
    print(f"wrote {len(chosen_days)} real intraday collector days ({len(listing)} real snapshots)")


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    # No local cache dir here -- pull straight from R2 so a refresh always
    # reflects the current live bucket rather than a stale prior fixture.
    src = R2Source(cache_dir=FIXTURE_DIR.parent / ".make_fixtures_scratch")
    if src.s3 is None:
        print(
            "R2 credentials not found in the environment "
            "(R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY). "
            "This script pulls real data to build the fixture and needs them to run "
            "-- the fixture it produces does not.",
            file=sys.stderr,
        )
        sys.exit(1)
    make_5min_bars(src)
    make_options_0dte(src)
    make_intraday_days(src)
    print(f"\nFixture written to {FIXTURE_DIR}")


if __name__ == "__main__":
    main()
