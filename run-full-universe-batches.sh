#!/bin/bash
# Full-universe replay for #130: 14 bounded batches covering the union of all
# selected archived underlyings for Oct 5-7, 2026 (661 underlyings).
# Each batch is at most 50 underlyings (MAX_UNDERLYINGS bound).
# Source blobs are reused from the read-through cache (replay-cache/).
# Output: /tmp/replay-130-full/batch-XX/ (one replay per batch).
# This script is idempotent: a batch that already produced output is skipped.
set -u
cd ~/workspace/goals/make-live-options-view-readable/work-130 || exit 1
OUT="$PWD/replay-130-full"
CACHE="$PWD/replay-cache"
mkdir -p "$OUT"
python3 - << 'PYEOF'
import json, subprocess, sys, os
batches = json.load(open('replay-130-batches.json'))
out = os.path.join(os.getcwd(), 'replay-130-full')
cache = os.path.join(os.getcwd(), 'replay-cache')
for i, batch in enumerate(batches, 1):
    tag = f'batch-{i:02d}'
    dest = os.path.join(out, tag)
    summary = os.path.join(dest, 'replay_summary.json')
    if os.path.exists(summary):
        print(f'{tag}: already done, skipping', flush=True)
        continue
    cmd = [os.path.expanduser('~/workspace/.tools/182-venv/bin/python'),
           'market_data/options_scanner/windows_replay.py',
           '--days', '2026-10-05', '2026-10-06', '2026-10-07',
           '--underlyings'] + batch + [
           '--out', dest, '--cache-dir', cache]
    print(f'{tag}: {len(batch)} underlyings, starting', flush=True)
    r = subprocess.run(cmd, env={**os.environ, 'PYTHONPATH': '.'},
                       capture_output=True, text=True)
    print(r.stdout.strip(), flush=True)
    if r.returncode != 0:
        print(f'{tag}: FAILED (exit {r.returncode})', flush=True)
        print(r.stderr[-2000:], flush=True)
        sys.exit(1)
    print(f'{tag}: done', flush=True)
print('ALL BATCHES DONE', flush=True)
PYEOF
