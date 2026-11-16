#!/usr/bin/env python3
"""Read saved Replay trades and notes straight from the trainer store (no server needed).

    python research/options_replay/records.py list
    python research/options_replay/records.py show tr_0123456789ab [--state]
    python research/options_replay/records.py list --store path/to/trainer.sqlite3

Dates, weekdays and source snapshot keys stay masked for sessions that have
not been revealed, exactly as in the page.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from archive import DEFAULT_CACHE  # noqa: E402
from store import TrainerStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["list", "show"])
    ap.add_argument("trade_id", nargs="?")
    ap.add_argument("--store", default=str(Path(DEFAULT_CACHE) / "trainer.sqlite3"))
    ap.add_argument("--state", action="store_true", help="include the decision-time visible state")
    args = ap.parse_args()
    path = Path(args.store)
    if not path.exists():
        print(f"no trainer store at {path}", file=sys.stderr)
        return 2
    store = TrainerStore(path)
    if args.cmd == "show":
        recs = store.trades(trade_id=args.trade_id, include_state=args.state)
        if not recs:
            print("unknown trade", file=sys.stderr)
            return 1
        print(json.dumps(recs[0], indent=1))
        return 0
    print(f"store: {path}")
    for r in store.trades(include_state=False):
        note = r["note"]["current"] if r["note"] else None
        print(f"{r['trade_id']}  session {r['session_id']}  attempt {r['attempt']}  {r['contract']}  "
              f"{r['status']}  fills {len(r['fills'])}  gross {r['gross_premium']}  fees {r['fees']}  "
              f"net {r['net']}  day {r['day'] or '(hidden)'}  "
              f"note {'rev ' + str(note['rev']) if note else '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
