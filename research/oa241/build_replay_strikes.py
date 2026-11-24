"""Build B's strike index from the same chain snapshots used by the replay.

python research/oa241/build_replay_strikes.py SNAPSHOT_ROOT site/order-flow-replay.json > site/order-flow-replay-strikes.json
"""
import csv
import json
from pathlib import Path
import sys


def strike_index(root, replay):
    session = replay["session"]
    opened, closed = (replay["coverage"][key] for key in ("open", "close"))
    snapshots = sorted(replay["snapshots"], key=lambda s: s["t"])
    before = [s for s in snapshots if s["t"] <= opened]
    changes = []
    for snap in before[-1:] + [s for s in snapshots if opened < s["t"] <= closed]:
        with (root / snap["key"]).open(newline="") as source:
            strikes = sorted({float(row["Strike"]) for row in csv.DictReader(source)
                              if row["Expiration"] == session and row["OptionSymbol"].startswith("QQQ")})
        if not changes or strikes != changes[-1][1]:
            changes.append([snap["t"], [int(k) if k.is_integer() else k for k in strikes]])
    return {"session": session, "source": replay["source"]["snapshots"], "changes": changes}


if __name__ == "__main__":
    print(json.dumps(strike_index(Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text())), separators=(",", ":")))
