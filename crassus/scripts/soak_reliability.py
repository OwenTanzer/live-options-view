#!/usr/bin/env python3
"""Real Chromium regression and driver-death injection on local fixture HTML.

No accounts, trading endpoints, production files or live Reddit requests.
Run in the production Docker image; exits nonzero when prerequisites are absent.
"""
from __future__ import annotations
import argparse
import gc
import json
import os
from pathlib import Path
import signal
import statistics
import sys
import time
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crassus import sentiment
from crassus.observability import configure_logging
from crassus.supervisor import memory_snapshot, proc_visible, process_tree, supervise

MIB = 1024 * 1024
HTML = '<html><body>' + ''.join(
    f'<shreddit-post id="post-{i}" post-title="QQQ fixture {i}"><div slot="text-body">QQQ fixture text</div></shreddit-post>'
    for i in range(12)
) + '</body></html>'


def fixture_browser():
    stack = sentiment._default_browser_factory()
    stack[2].route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=HTML))
    return stack


def driver_pid(playwright) -> int:
    # Test-only access to the exact transport process in our pinned Playwright
    # version. Node can change its /proc/comm title, so do not infer identity
    # from that mutable process name.
    pid = playwright._impl_obj._connection._transport._proc.pid
    assert pid in process_tree(os.getpid()), 'driver is not our descendant'
    return pid


def inject_driver_failure(evidence: Path):
    stack = fixture_browser()
    page = stack[2].new_page()
    page.goto('https://fixture.invalid/')
    os.kill(driver_pid(stack[0]), signal.SIGKILL)
    evidence.write_text("driver SIGKILL injected\n")
    try:
        page.title()  # Real synchronous Playwright call after fatal driver death.
    except Exception:
        pass
    # Model the historical half-alive Python worker even if this version
    # propagates the driver exception promptly. Supervisor must end it.
    time.sleep(60)


def soak(cycles: int) -> dict:
    class Analyzer:
        def polarity_scores(self, text):
            return {'compound': 0.0}
    reader = sentiment.RedditSentimentReader(
        browser_factory=fixture_browser, session_factory=lambda: None,
        analyzer_factory=Analyzer, subreddits=('one', 'two'), post_limit=12)
    original_collect = reader._collect_texts
    active = []
    def collect():
        yield from original_collect()
        active.append(memory_snapshot(os.getpid()))
    reader._collect_texts = collect
    resting = []
    with patch.object(sentiment, '_fetch_listing_json', side_effect=sentiment.RedditFetchError('fixture fallback')):
        for cycle in range(cycles):
            snapshot = reader.read(force=True)
            assert snapshot.sample_size == 24, snapshot.sample_size
            gc.collect()
            deadline = time.monotonic() + 2
            sample = memory_snapshot(os.getpid())
            while sample['descendant_count'] and time.monotonic() < deadline:
                time.sleep(.02)
                sample = memory_snapshot(os.getpid())
            assert sample['descendant_count'] == 0, sample
            resting.append(sample['worker_rss_bytes'])
            assert active[-1]['descendant_rss_bytes'] < 1024 * MIB, active[-1]
            print(json.dumps({'cycle': cycle + 1, 'active': active[-1], 'after_cleanup': sample}), flush=True)
    warm = resting[5:]
    window = max(3, len(warm) // 3)
    growth = statistics.median(warm[-window:]) - statistics.median(warm[:window])
    assert growth <= 64 * MIB, f'resting RSS growth {growth} exceeds 64 MiB'
    return dict(cycles=cycles, posts=cycles * 24, resting_rss_growth_bytes=growth,
                peak_descendant_rss_bytes=max(s['descendant_rss_bytes'] for s in active),
                residual_descendants=0, samples=active, resting_rss_bytes=resting)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cycles', type=int, default=30)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--injection-evidence', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--inject-driver-failure', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    configure_logging()
    if not proc_visible():
        parser.error('requires Linux /proc in the current PID namespace; use the production Docker image')
    if args.inject_driver_failure:
        inject_driver_failure(args.injection_evidence)
        return 1
    if args.cycles < 15:
        parser.error('at least 15 cycles required for warm-up and trend windows')
    # Bound launch + injected blocked-call time. Existing production default
    # is 300 s; shortening to 10 s exercises the same watchdog safely here.
    with tempfile.TemporaryDirectory() as tmp:
        evidence = Path(tmp) / 'driver-killed.txt'
        result = supervise([sys.executable, __file__, '--inject-driver-failure',
                            '--injection-evidence', str(evidence)], 10,
                           shutdown_grace_s=1, memory_interval_s=1)
        assert result == 75, f'unexpected supervisor exit {result}'
        assert evidence.read_text() == "driver SIGKILL injected\n", 'driver injection did not run'
    assert memory_snapshot(os.getpid())['descendant_count'] == 0, 'orphaned browser after failure'
    report = soak(args.cycles)  # New processes advance actual successful reads after the injected failure.
    report['fatal_driver_failure_exit'] = result
    report['historical_heap_leak_reproduced'] = False
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'result': 'PASS', **{k: v for k, v in report.items() if k not in ('samples', 'resting_rss_bytes')}}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
