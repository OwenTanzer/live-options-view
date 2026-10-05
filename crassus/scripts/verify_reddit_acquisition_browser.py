#!/usr/bin/env python3
"""Real Chromium acquisition boundary; run in Docker with --network none.

No accounts, order endpoints or live source probes. A routed local DOM fixture
and an intentional cleanup hang exercise the actual production process owner.
"""
from pathlib import Path
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crassus.reddit_acquisition import _run, CLEANUP_TIMEOUT_S
from crassus.sentiment import RedditFetchError, _ACQUISITION_TIMEOUT_S
from crassus import sentiment as s
from crassus.supervisor import _subreaper, proc_visible, process_tree, reap_adopted, _stop_group

CHILD = '''
from unittest.mock import Mock
import time
from crassus import sentiment as s
from crassus.reddit_acquisition import main
FIXTURE_MODE = 'full'
original = s.RedditSentimentReader.__init__
def init(self, **kwargs):
    original(self, **kwargs)
    session = Mock()
    session.get.return_value.status_code = 503
    self._session_factory = lambda: session
    def browser():
        stack = s._default_browser_factory()
        def post(i):
            title = 'QQQ good' if i == 0 else f'QQQ good {i}'
            return f'<shreddit-post id="fixture-{i}" post-title="{title}"><div slot="text-body">QQQ body</div></shreddit-post>'
        html = ''.join(post(i) for i in range(kwargs['post_limit']))
        if FIXTURE_MODE == 'gradual':
            html = post(0) + """<script>
            let n = 1;
            addEventListener('wheel', () => {
                let p = document.createElement('shreddit-post');
                p.id = 'fixture-' + n++;
                p.setAttribute('post-title', 'QQQ good');
                p.innerHTML = '<div slot="text-body">QQQ body</div>';
                document.body.appendChild(p);
            });
            </script>"""
        stack[2].route('**/*', lambda route: route.fulfill(status=200, content_type='text/html',
            body=html))
        return stack
    self._browser_factory = browser
s.RedditSentimentReader.__init__ = init
{injection}
main()
'''


def nested_caller(config, baseline, *, hang_cleanup):
    """Match supervisor(1) -> caller(0) -> worker/browser adoption topology."""
    code = '''
import json, sys
from scripts.verify_reddit_acquisition_browser import CHILD
from crassus.reddit_acquisition import _run
from crassus.sentiment import RedditFetchError
from crassus.supervisor import _subreaper
before = _subreaper()
try:
    result = _run({config}, timeout_s={timeout},
        command=[sys.executable, '-c', CHILD.replace('{injection}', {fixture_injection})])
except RedditFetchError as exc:
    result = {'error': str(exc)}
result.update(caller_subreaper_before=before, caller_subreaper_after=_subreaper())
print(json.dumps(result), flush=True)
'''
    injection = 's.RedditSentimentReader._close_browser = lambda self: time.sleep(60)' if hang_cleanup else ''
    code = code.replace('{config}', repr(config)).replace('{timeout}', '5' if hang_cleanup else '20')
    code = code.replace('{fixture_injection}', repr(injection))
    child = subprocess.Popen([sys.executable, '-c', code],
        cwd=Path(__file__).resolve().parent.parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True)
    deadline = time.monotonic() + (9 if hang_cleanup else 24)
    try:
        while True:
            try:
                output, errors = child.communicate(timeout=1)  # Existing supervisor poll cadence.
                break
            except subprocess.TimeoutExpired:
                reap_adopted(child.pid, baseline)
                assert time.monotonic() < deadline, 'nested acquisition exceeded bound'
        reap_adopted(child.pid, baseline)
        assert child.returncode == 0, errors.decode()
        result = json.loads(output.splitlines()[-1])
        assert result['caller_subreaper_before'] == result['caller_subreaper_after'] == 0, result
        if hang_cleanup:
            assert 'cleanup_timeout' in result.get('error', ''), result
        else:
            assert result['texts'] == ['QQQ good QQQ body'], result
        return result
    finally:
        if child.poll() is None:
            _stop_group(child, .1, baseline)
        child.stdout.close()
        child.stderr.close()


def main():
    if not proc_visible():
        raise RuntimeError('requires real Linux process-tree visibility; run Docker CI')
    previous = _subreaper(1)
    baseline = set(process_tree(os.getpid()))
    config = dict(subreddits=['stocks'], keywords=['qqq'], post_limit=1, rate_limited_until=0)
    results = []
    try:
        for mode in ('success', 'default_success', 'default_budget_exhausted', 'hung_cleanup', 'owner_crash'):
            injection = {
                'success': '',
                'default_success': '',
                'default_budget_exhausted': "FIXTURE_MODE = 'gradual'",
                'hung_cleanup': 's.RedditSentimentReader._close_browser = lambda self: time.sleep(60)',
                'owner_crash': 'import os; orphan_stack = s._default_browser_factory(); os._exit(1)',
            }[mode]
            started = time.monotonic()
            case_config = (dict(subreddits=list(s.DEFAULT_SUBREDDITS), keywords=list(s.DEFAULT_KEYWORDS),
                                post_limit=s.DEFAULT_POST_LIMIT, rate_limited_until=0)
                           if mode.startswith('default_') else config)
            try:
                result = _run(case_config, command=[sys.executable, '-c', CHILD.replace('{injection}', injection)])
                assert mode in ('success', 'default_success'), result
                assert len(result['texts']) == len(case_config['subreddits']) * case_config['post_limit'], result
                if mode == 'success':
                    assert result['texts'] == ['QQQ good QQQ body'], result
            except RedditFetchError as exc:
                assert mode not in ('success', 'default_success'), str(exc)
                if mode == 'default_budget_exhausted':
                    assert 'browser_timeout' in str(exc), str(exc)
                if mode == 'hung_cleanup':
                    assert 'cleanup_timeout' in str(exc), str(exc)
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
        for hang_cleanup in (False, True):
            started = time.monotonic()
            result = nested_caller(config, baseline, hang_cleanup=hang_cleanup)
            deadline = time.monotonic() + 2
            while set(process_tree(os.getpid())) - baseline and time.monotonic() < deadline:
                reap_adopted(None, baseline)
                time.sleep(.02)
            assert not set(process_tree(os.getpid())) - baseline, 'nested caller residual descendants'
            results.append(dict(mode='nested_hung_cleanup' if hang_cleanup else 'nested_success',
                                elapsed_seconds=round(time.monotonic() - started, 3),
                                caller_subreaper=0, supervisor_poll_seconds=1,
                                residual_descendants=0, error=result.get('error')))
    finally:
        _subreaper(previous)
    print(json.dumps(results))

if __name__ == '__main__':
    main()
