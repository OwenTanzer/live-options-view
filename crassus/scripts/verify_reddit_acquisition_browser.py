#!/usr/bin/env python3
"""Real Chromium acquisition boundary; run in Docker with --network none.

No accounts, order endpoints or live source probes. A routed local DOM fixture
and an intentional cleanup hang exercise the actual production process owner.
"""
from pathlib import Path
import json
import os
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crassus.reddit_acquisition import _run, CLEANUP_TIMEOUT_S
from crassus.sentiment import RedditFetchError, _ACQUISITION_TIMEOUT_S
from crassus.supervisor import _subreaper, proc_visible, process_tree, reap_adopted

CHILD = '''
from unittest.mock import Mock
import time
from crassus import sentiment as s
from crassus.reddit_acquisition import main
original = s.RedditSentimentReader.__init__
def init(self, **kwargs):
    original(self, **kwargs)
    session = Mock()
    session.get.return_value.status_code = 503
    self._session_factory = lambda: session
    def browser():
        stack = s._default_browser_factory()
        stack[2].route('**/*', lambda route: route.fulfill(status=200, content_type='text/html',
            body='<shreddit-post id="fixture" post-title="QQQ good"></shreddit-post>'))
        return stack
    self._browser_factory = browser
s.RedditSentimentReader.__init__ = init
{injection}
main()
'''


def main():
    if not proc_visible():
        raise RuntimeError('requires real Linux process-tree visibility; run Docker CI')
    previous = _subreaper(1)
    baseline = set(process_tree(os.getpid()))
    config = dict(subreddits=['stocks'], keywords=['qqq'], post_limit=1, rate_limited_until=0)
    results = []
    try:
        for mode in ('success', 'hung_cleanup'):
            injection = '' if mode == 'success' else 's.RedditSentimentReader._close_browser = lambda self: time.sleep(60)'
            started = time.monotonic()
            try:
                result = _run(config, command=[sys.executable, '-c', CHILD.replace('{injection}', injection)])
                assert mode == 'success', result
                assert result['texts'] == ['QQQ good '], result
            except RedditFetchError as exc:
                assert mode == 'hung_cleanup' and 'cleanup_timeout' in str(exc), str(exc)
            elapsed = time.monotonic() - started
            assert elapsed < _ACQUISITION_TIMEOUT_S + CLEANUP_TIMEOUT_S + 1, elapsed
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                reap_adopted(None, baseline)
                remaining = set(process_tree(os.getpid())) - baseline
                if not remaining:
                    break
                time.sleep(.02)
            assert not remaining, remaining
            results.append(dict(mode=mode, elapsed_seconds=round(elapsed, 3), residual_descendants=0))
    finally:
        _subreaper(previous)
    print(json.dumps(results))

if __name__ == '__main__':
    main()
