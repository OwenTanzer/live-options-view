#!/usr/bin/env python3
"""OA-133 hermetic regression: no live source, account login, or orders."""
from __future__ import annotations
import sys
from pathlib import Path
import unittest
import time
import threading
import json
import subprocess
import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crassus import sentiment as s

class FailureWindow(unittest.TestCase):
    def reader(self):
        return s.RedditSentimentReader(session_factory=Mock, analyzer_factory=Mock,
                                       browser_factory=Mock())

    def test_six_accounts_share_one_failed_acquisition(self):
        reader = self.reader()
        elapsed = [0.0]
        def failure():
            elapsed[0] += 20
            raise s.RedditFetchError('browser_timeout')
        reader._collect_texts = Mock(side_effect=failure)
        with patch.object(s.time, 'monotonic', side_effect=lambda: 1000 + elapsed[0]):
            for _ in range(6):
                with self.assertRaises(s.RedditFetchError):
                    reader.read()
        print(f'REPRO six accounts: acquisitions={reader._collect_texts.call_count}, simulated_seconds={elapsed[0]}')
        self.assertEqual(reader._collect_texts.call_count, 1)
        self.assertEqual(elapsed[0], 20)

    def test_denial_never_launches_browser(self):
        reader = self.reader()
        reader._session = Mock()
        reader._session.get.return_value.status_code = 403
        with self.assertRaisesRegex(s.RedditFetchError, 'source_denied'):
            reader._fetch_listing('wallstreetbets')
        reader._browser_factory.assert_not_called()

    def test_changed_child_shape_is_explicit_parse_error(self):
        session = Mock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = {'data': {'children': [{}]}}
        with self.assertRaisesRegex(s.RedditFetchError, 'parsing_failure'):
            s._fetch_listing_json(session, 'stocks', limit=50)

    def test_concurrent_failure_is_single_flight(self):
        reader = self.reader()
        entered, release = threading.Event(), threading.Event()
        def fail():
            entered.set()
            self.assertTrue(release.wait(2))
            raise s.RedditFetchError('browser_timeout')
        reader._collect_texts = Mock(side_effect=fail)
        def read():
            try:
                reader.read()
            except s.RedditFetchError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(read) for _ in range(6)]
            self.assertTrue(entered.wait(2))
            release.set()
            self.assertEqual([f.result(2) for f in futures], ['browser_timeout'] * 6)
        self.assertEqual(reader._collect_texts.call_count, 1)

    def test_expiry_recovers_and_failure_never_serves_old_success(self):
        reader = self.reader()
        reader._analyzer_factory = lambda: Mock(polarity_scores=lambda text: {'compound': .2})
        reader._collect_texts = Mock(side_effect=[['QQQ'], s.RedditFetchError('source_denied'), ['QQQ recovered']])
        now = [1000.0]
        with patch.object(s.time, 'monotonic', side_effect=lambda: now[0]):
            first = reader.read()
            now[0] = 1299
            self.assertIs(reader.read(), first)
            now[0] = 1300
            with self.assertRaisesRegex(s.RedditFetchError, 'source_denied'):
                reader.read()
            now[0] = 1599
            with self.assertRaises(s.RedditFetchError):
                reader.read(force=True)
            self.assertIsNone(reader._cached)
            now[0] = 1600
            self.assertIsNot(reader.read(), first)
        self.assertEqual(reader._collect_texts.call_count, 3)

    def test_stale_or_malformed_source_cannot_use_browser(self):
        for payload, headers, kind in [
            ({'data': {'children': []}}, {'Age': '301'}, 'stale_data'),
            ({'data': {'children': []}}, {'Date': 'Mon, 01 Jan 2001 00:00:00 GMT'}, 'freshness_unverifiable'),
            ({'data': {'children': []}}, {'Age': 'nan'}, 'parsing_failure'),
            ({'data': {'children': {}}}, {}, 'parsing_failure'),
            ({'data': {'children': [{'data': {'title': 123}}]}}, {}, 'parsing_failure'),
            ({'data': {'children': [{'data': {'title': 'QQQ', 'selftext': []}}]}}, {}, 'parsing_failure'),
        ]:
            with self.subTest(kind=kind, payload=payload, headers=headers):
                reader = self.reader()
                response = Mock(status_code=200, headers=headers)
                response.json.return_value = payload
                reader._session = Mock(get=Mock(return_value=response))
                with self.assertRaisesRegex(s.RedditFetchError, kind):
                    reader._fetch_listing('stocks')
                reader._browser_factory.assert_not_called()

    def test_six_strategy_calls_share_read_but_keep_thresholds(self):
        from crassus.strategies import reddit_sentiment as rs
        from verify_reddit_sentiment import make_ctx, fresh_quote, CALL_ROW
        reader = self.reader()
        reader._collect_texts = Mock(return_value=['QQQ'] * 5)
        reader._analyzer_factory = lambda: Mock(polarity_scores=lambda text: {'compound': .2})
        quote_map = {CALL_ROW['OptionSymbol']: fresh_quote(CALL_ROW['OptionSymbol'])}
        contexts = [make_ctx(quote_map=quote_map, params=p) for p in
                    [{}, {'min_sample_size': 6}, {'bullish_threshold': .3}] * 2]
        with patch.object(rs, '_reader', reader):
            decisions = [rs._decide(ctx) for ctx in contexts]
        self.assertEqual([d.action for d in decisions], ['buy', 'no_trade', 'no_trade'] * 2)
        self.assertEqual(reader._collect_texts.call_count, 1)
        self.assertEqual(decisions[1].metadata['sample_size'], 5)
        self.assertIn('need 6', decisions[1].reason)

    def test_denied_timeout_empty_are_safe_without_changing_exit_semantics(self):
        from crassus.strategies import reddit_sentiment as rs
        from verify_reddit_sentiment import make_ctx, fresh_quote, CALL_ROW
        from crassus.client import Position
        for failure in ['source_denied', 'browser_timeout', 'stale_data', 'parsing_failure', None]:
            reader = self.reader()
            reader._collect_texts = Mock(side_effect=s.RedditFetchError(failure)) if failure else Mock(return_value=[])
            reader._analyzer_factory = Mock
            flat = make_ctx()
            held = make_ctx(quote_map={CALL_ROW['OptionSymbol']: fresh_quote(CALL_ROW['OptionSymbol'])})
            held.book.positions[CALL_ROW['OptionSymbol']] = Position(CALL_ROW['OptionSymbol'], 1, 1.0)
            with patch.object(rs, '_reader', reader):
                self.assertEqual(rs._decide(flat).action, 'no_trade')
                self.assertEqual(rs._decide(held).action, 'sell')
            self.assertEqual(reader._collect_texts.call_count, 1)

    def test_cancelled_owner_releases_lock_and_does_not_trigger_retry_storm(self):
        reader = self.reader()
        reader._collect_texts = Mock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            reader.read()
        with self.assertRaisesRegex(s.RedditFetchError, 'source_cancelled'):
            reader.read()
        self.assertEqual(reader._collect_texts.call_count, 1)

    def test_real_process_deadline_includes_launch_http_browser_and_cleanup(self):
        from crassus.reddit_acquisition import _run
        for phase in ('http', 'browser', 'cleanup'):
            with self.subTest(phase=phase):
                code = "import json,time; print(json.dumps({'phase': '" + phase + "'}),flush=True); time.sleep(60)"
                started = time.monotonic()
                with self.assertRaisesRegex(s.RedditFetchError, phase + '_timeout'):
                    _run({}, timeout_s=.15, command=[sys.executable, '-c', code])
                elapsed = time.monotonic() - started
                print(f'PROCESS {phase}: {elapsed:.3f}s (fixture budget .15s + cleanup 1s)')
                self.assertLess(elapsed, 1.15)

    def test_worker_denial_and_timeout_run_real_transport_selection_offline(self):
        from crassus.reddit_acquisition import _run
        config = dict(subreddits=['stocks'], keywords=['qqq'], post_limit=50, rate_limited_until=0)
        for status in (403, 503):
            code = f"""
import json
from unittest.mock import Mock
from crassus import sentiment as s
from crassus.reddit_acquisition import main
session = Mock()
session.get.return_value.status_code = {status}
session.get.return_value.headers = {{}}
original = s.RedditSentimentReader.__init__
def init(self, **kwargs):
    original(self, **kwargs)
    self._session_factory = lambda: session
    def factory():
        print(json.dumps({{'fallback_attempt': True}}), flush=True)
        context = Mock()
        context.new_page.return_value.goto.side_effect = TimeoutError('sensitive payload must not escape')
        return Mock(), Mock(), context
    self._browser_factory = factory
s.RedditSentimentReader.__init__ = init
main()
"""
            result = _run(config, command=[sys.executable, '-c', code])
            self.assertIn('source_denied' if status == 403 else 'browser_timeout', result['error'])
            self.assertNotIn('sensitive payload', result['error'])

    def test_transport_result_and_cancellation_cleanup(self):
        from crassus.reddit_acquisition import _run
        with self.assertRaisesRegex(s.RedditFetchError, 'parsing_failure'):
            _run({}, command=[sys.executable, '-c', "print('not-json')"])
        child = Mock()
        child.communicate.side_effect = KeyboardInterrupt
        child.poll.return_value = None
        with patch('crassus.reddit_acquisition.subprocess.Popen', return_value=child), patch('crassus.reddit_acquisition._stop') as stop, patch('crassus.reddit_acquisition.proc_visible', return_value=True):
            with self.assertRaises(KeyboardInterrupt):
                _run({})
        stop.assert_called_once()
        child.stdout.close.assert_called_once()

    def test_parameterizations_do_not_share_or_relabel_snapshots(self):
        readers = [s.RedditSentimentReader(symbol=symbol, keywords=(keyword,),
                   subreddits=(subreddit,), session_factory=Mock, browser_factory=Mock,
                   analyzer_factory=lambda: Mock(polarity_scores=lambda text: {'compound': .3}))
                   for symbol, keyword, subreddit in [('QQQ', 'qqq', 'stocks'), ('SPY', 'spy', 'options')]]
        for reader in readers:
            reader._fetch_listing = Mock(return_value=[{'title': 'QQQ up'}, {'title': 'SPY down'}])
        snapshots = [reader.read() for reader in readers]
        self.assertEqual([s.symbol for s in snapshots], ['QQQ', 'SPY'])
        self.assertEqual([s.subreddits for s in snapshots], [('stocks',), ('options',)])
        self.assertEqual([s.sample_size for s in snapshots], [1, 1])
        self.assertNotEqual(snapshots[0].items, snapshots[1].items)

    def test_real_detached_descendant_cleanup_preserves_unrelated_child(self):
        import tempfile
        from crassus.reddit_acquisition import _run
        from crassus.supervisor import proc_visible, _subreaper, reap_adopted
        if not proc_visible():
            self.skipTest('PID namespace hides process tree; exercised by Linux CI')
        previous = _subreaper(1)
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pidfile = Path(tmp) / 'pid'
                code = ("import subprocess,sys,time; from pathlib import Path; "
                        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], start_new_session=True); "
                        f"Path({str(pidfile)!r}).write_text(str(p.pid)); time.sleep(60)")
                with self.assertRaises(s.RedditFetchError):
                    _run({}, timeout_s=.3, command=[sys.executable, '-c', code])
                pid = int(pidfile.read_text())
                deadline = time.monotonic() + 1
                while Path(f'/proc/{pid}').exists() and time.monotonic() < deadline:
                    reap_adopted(None, {unrelated.pid})
                    time.sleep(.01)
                self.assertFalse(Path(f'/proc/{pid}').exists())
                self.assertIsNone(unrelated.poll())
        finally:
            unrelated.kill()
            unrelated.wait(1)
            _subreaper(previous)

    def test_exited_owner_detached_descendant_cleanup(self):
        import tempfile
        from crassus.reddit_acquisition import _run
        from crassus.supervisor import proc_visible, _subreaper, reap_adopted
        if not proc_visible():
            self.skipTest('requires Linux process-tree visibility')
        previous = _subreaper(1)
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        try:
            for exit_code, inherited in ((0, False), (0, True), (1, False), (1, True)):
                with self.subTest(exit_code=exit_code, inherited_stdout=inherited), tempfile.TemporaryDirectory() as tmp:
                    pidfile = Path(tmp) / 'pid'
                    code = ("import subprocess,sys; from pathlib import Path; "
                            f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], "
                            f"start_new_session=True, stdout={'None' if inherited else 'subprocess.DEVNULL'}); "
                            f"Path({str(pidfile)!r}).write_text(str(p.pid)); "
                            "print('{\"texts\": [], \"rate_limited_until\": 0}', flush=True); "
                            f"sys.exit({exit_code})")
                    started = time.monotonic()
                    if exit_code == 0 and not inherited:
                        self.assertEqual(_run({}, timeout_s=.3, command=[sys.executable, '-c', code])['texts'], [])
                    else:
                        with self.assertRaises(s.RedditFetchError):
                            _run({}, timeout_s=.3, command=[sys.executable, '-c', code])
                    self.assertLess(time.monotonic() - started, 1.3)
                    pid = int(pidfile.read_text())
                    deadline = time.monotonic() + 1
                    while Path(f'/proc/{pid}').exists() and time.monotonic() < deadline:
                        reap_adopted(None, {unrelated.pid})
                        time.sleep(.01)
                    self.assertFalse(Path(f'/proc/{pid}').exists())
                    self.assertIsNone(unrelated.poll())
        finally:
            unrelated.kill()
            unrelated.wait(1)
            _subreaper(previous)

    def test_provider_deadline_survives_worker_cleanup_failure(self):
        from crassus.reddit_acquisition import _run
        from crassus.supervisor import proc_visible
        if not proc_visible():
            self.skipTest('requires POSIX acquisition worker')
        code = '''
import json, time
from unittest.mock import Mock
from crassus import sentiment as s
from crassus.reddit_acquisition import main
original = s.RedditSentimentReader.__init__
def init(self, **kwargs):
    original(self, **kwargs)
    response = Mock(status_code=429, headers={'Retry-After': '3600'})
    self._session_factory = lambda: Mock(get=lambda *args, **kwargs: response)
    self._close_browser = lambda: time.sleep(60)
s.RedditSentimentReader.__init__ = init
main()
'''
        config = dict(subreddits=['stocks'], keywords=['qqq'], post_limit=1,
                      min_interval_s=300, rate_limited_until=0)
        for cleanup in ('lambda: time.sleep(60)',
                        "lambda: (_ for _ in ()).throw(RuntimeError('cleanup failed'))",
                        "lambda: __import__('os')._exit(1)"):
            with self.subTest(cleanup=cleanup):
                started = time.monotonic()
                with self.assertRaises(s.RedditFetchError) as raised:
                    _run(config, timeout_s=.3, command=[sys.executable, '-c',
                         code.replace('lambda: time.sleep(60)', cleanup)])
                self.assertGreater(raised.exception.rate_limited_until, started + 3500)
                self.assertLess(time.monotonic() - started, 1.3)

    def test_provider_deadline_survives_browser_page_cleanup_hang(self):
        from crassus.reddit_acquisition import _run
        from crassus.supervisor import proc_visible
        if not proc_visible():
            self.skipTest('requires POSIX acquisition worker')
        code = '''
import time
from unittest.mock import Mock
from crassus import sentiment as s
from crassus.reddit_acquisition import main
original = s.RedditSentimentReader.__init__
def init(self, **kwargs):
    original(self, **kwargs)
    self._session_factory = lambda: Mock(get=lambda *args, **kwargs: Mock(status_code=503))
    page = Mock()
    page.goto.return_value.status = 429
    page.goto.return_value.all_headers.return_value = {'retry-after': '3600'}
    page.close.side_effect = lambda: time.sleep(60)
    context = Mock(new_page=lambda: page)
    self._browser_factory = lambda: (Mock(), Mock(), context)
s.RedditSentimentReader.__init__ = init
main()
'''
        config = dict(subreddits=['stocks'], keywords=['qqq'], post_limit=1,
                      min_interval_s=300, rate_limited_until=0)
        started = time.monotonic()
        with self.assertRaisesRegex(s.RedditFetchError, 'browser_timeout') as raised:
            _run(config, timeout_s=.3, command=[sys.executable, '-c', code])
        self.assertGreater(raised.exception.rate_limited_until, started + 3500)
        self.assertLess(time.monotonic() - started, 1.3)

    def test_runner_cycle_reaches_unrelated_account_after_one_failed_budget(self):
        from types import SimpleNamespace
        from crassus.runner import Runner
        from crassus.strategies import reddit_sentiment as rs
        from verify_reddit_sentiment import make_ctx
        runner = Runner.__new__(Runner)  # No sessions/executors/ledgers are constructed.
        runner.accounts = [SimpleNamespace(alias=f'reddit-{i}') for i in range(9)] + [SimpleNamespace(alias='unrelated')]
        runner._stop = threading.Event()
        runner.stop_file = Mock(exists=lambda: False)
        runner.retired = set()
        runner.cycle_count = 0
        runner.ledger = SimpleNamespace(run_id='offline-fixture')
        runner.snapshots = Mock(read=lambda: None)
        reader = self.reader()
        elapsed = [1000.0]
        def fail():
            elapsed[0] += s._ACQUISITION_TIMEOUT_S
            raise s.RedditFetchError('browser_timeout')
        reader._collect_texts = Mock(side_effect=fail)
        visited = []
        def account(account, snapshot, phase):
            visited.append(account.alias)
            if account.alias != 'unrelated':
                self.assertEqual(rs._decide(make_ctx()).action, 'no_trade')
        runner._run_account = account
        with patch.object(s.time, 'monotonic', side_effect=lambda: elapsed[0]), patch.object(rs, '_reader', reader), \
             patch('crassus.runner.clock.session_phase', return_value='open'), patch('crassus.runner.report_cycle') as report:
            self.assertTrue(runner.run_cycle())
        self.assertEqual(visited[-1], 'unrelated')
        self.assertEqual(len(visited), 10)
        completed = report.call_args.kwargs
        self.assertEqual(completed['duration_seconds'], 20)
        self.assertLess(completed['duration_seconds'], 300)
        self.assertEqual(reader._collect_texts.call_count, 1)

    def test_source_response_age_reduces_cache_lifetime(self):
        reader = self.reader()
        reader.subreddits = ('stocks',)
        response = Mock(status_code=200, headers={'Age': '290'})
        response.json.return_value = {'data': {'children': []}}
        session = Mock(get=Mock(return_value=response))
        reader._session_factory = lambda: session
        now = [1000.0]
        with patch.object(s.time, 'monotonic', side_effect=lambda: now[0]):
            first = reader.read()
            now[0] = 1009
            self.assertIs(reader.read(), first)
            now[0] = 1010
            self.assertIsNot(reader.read(), first)
        self.assertEqual(session.get.call_count, 2)

    def test_browser_response_staleness_is_terminal(self):
        page = Mock()
        page.goto.return_value.status = 200
        page.goto.return_value.all_headers.return_value = {'age': '301'}
        with self.assertRaisesRegex(s.RedditFetchError, 'stale_data'):
            s._fetch_listing_browser(Mock(new_page=lambda: page), 'stocks', limit=50)
        page.wait_for_selector.assert_not_called()
        page.close.assert_called_once()

    def test_browser_429_honors_retry_after_and_invalid_values(self):
        for header, expected in [('3600', 3600), ('bogus', 60), ('nan', 60), ('-2', 60)]:
            with self.subTest(header=header):
                page = Mock()
                page.goto.return_value.status = 429
                page.goto.return_value.all_headers.return_value = {'retry-after': header}
                with self.assertRaises(s.RedditRateLimited) as raised:
                    s._fetch_listing_browser(Mock(new_page=lambda: page), 'stocks', limit=1)
                self.assertEqual(raised.exception.retry_after_s, expected)
                page.close.assert_called_once()

        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime
        future = format_datetime(datetime.now(timezone.utc) + timedelta(hours=2), usegmt=True)
        self.assertGreater(s._retry_after({'Retry-After': future}), 7100)
        self.assertLess(s._retry_after({'Retry-After': future}), 7201)
        response = Mock(status_code=429, headers={'Retry-After': '3600'})
        with self.assertRaises(s.RedditRateLimited) as raised:
            s._fetch_listing_json(Mock(get=lambda *args, **kwargs: response), 'stocks', limit=1)
        self.assertEqual(raised.exception.retry_after_s, 3600)

    def test_browser_provider_cooldown_blocks_force_and_expires(self):
        reader = self.reader()
        reader.subreddits = ('stocks',)
        now = [1000.0]
        limited = Mock(status=429)
        limited.all_headers.return_value = {'retry-after': '3600'}
        page = Mock(goto=Mock(return_value=limited))
        context = Mock(new_page=Mock(return_value=page))
        reader._browser_factory = lambda: (Mock(), Mock(is_connected=lambda: True), context)
        success = Mock(status_code=200, headers={})
        success.json.return_value = {'data': {'children': [{'data': {'title': 'QQQ'}}]}}
        reader._session_factory = lambda: Mock(get=Mock(side_effect=[Mock(status_code=503), success]))
        reader._analyzer_factory = lambda: Mock(polarity_scores=lambda text: {'compound': .2})
        with patch.object(s.time, 'monotonic', side_effect=lambda: now[0]):
            with self.assertRaises(s.RedditRateLimited):
                reader.read()
            now[0] = 1299
            with self.assertRaises(s.RedditFetchError):
                reader.read(force=True)
            self.assertEqual(reader._rate_limited_until, 4600)
            now[0] = 4600
            self.assertEqual(reader.read().sample_size, 1)

    def test_browser_cooldown_is_observed_before_page_cleanup(self):
        page = Mock()
        page.goto.return_value.status = 429
        page.goto.return_value.all_headers.return_value = {'retry-after': '3600'}
        observed = []
        def close():
            self.assertEqual(observed, [4600])
            raise RuntimeError('failed cleanup')
        page.close.side_effect = close
        with patch.object(s.time, 'monotonic', return_value=1000):
            with self.assertRaises(s.RedditRateLimited):
                s._fetch_listing_browser(Mock(new_page=lambda: page), 'stocks', limit=1,
                                         rate_limit_callback=observed.append)
        self.assertEqual(observed, [4600])

    def test_cleanup_runs_after_exited_worker(self):
        from crassus.reddit_acquisition import _run
        child = Mock(pid=12345, returncode=0)
        child.communicate.return_value = (b'{"texts": [], "rate_limited_until": 0}\n', b'')
        child.poll.return_value = 0
        from types import SimpleNamespace
        from crassus import reddit_acquisition as ra
        with patch('crassus.reddit_acquisition.subprocess.Popen', return_value=child), \
             patch('crassus.reddit_acquisition._stop') as stop, \
             patch('crassus.reddit_acquisition.proc_visible', return_value=True), \
             patch.object(ra, 'os', SimpleNamespace(name='posix', environ={})), \
             patch.object(ra, '_subreaper', return_value=0, create=True):
            self.assertEqual(_run({})['texts'], [])
        stop.assert_called_once()

    def test_ownership_marker_cannot_claim_reused_pid(self):
        from crassus import reddit_acquisition as ra
        def stat(started):
            fields = ['0'] * 22
            fields[0], fields[1], fields[19], fields[21] = 'S', '1', str(started), '1'
            return '42 (fixture) ' + ' '.join(fields)
        for later_start, expected in ((111, {42}), (222, set())):
            directory = MagicMock()
            directory.name = '42'
            files = {
                'stat': Mock(read_text=Mock(side_effect=[stat(111), stat(later_start)])),
                'environ': Mock(read_bytes=lambda: b'CRASSUS_ACQUISITION_ID=fixture\0'),
            }
            directory.__truediv__.side_effect = files.__getitem__
            with patch.object(ra, 'Path', return_value=Mock(iterdir=lambda: [directory])):
                self.assertEqual(set(ra._marked_processes('fixture')), expected)

    def test_provider_deadline_survives_cleanup_timeout(self):
        from crassus import reddit_acquisition as ra
        child = Mock(pid=12345)
        child.communicate.side_effect = subprocess.TimeoutExpired('worker', .1,
            output=b'{"rate_limited_until": 4600}\n{"phase": "cleanup"}\n')
        from types import SimpleNamespace
        with patch.object(ra.subprocess, 'Popen', return_value=child), \
             patch.object(ra, '_stop'), patch.object(ra, 'proc_visible', return_value=True), \
             patch.object(ra, 'os', SimpleNamespace(name='posix', environ={})), \
             patch.object(ra, '_subreaper', return_value=0, create=True):
            with self.assertRaisesRegex(s.RedditFetchError, 'cleanup_timeout') as raised:
                ra._run({}, timeout_s=.1)
        self.assertEqual(raised.exception.rate_limited_until, 4600)

    def test_provider_deadline_reaches_shared_failure_window_and_recovers(self):
        from crassus import reddit_acquisition as ra
        reader = s.RedditSentimentReader()
        reader._analyzer_factory = lambda: Mock(polarity_scores=lambda text: {'compound': .2})
        failure = s.RedditFetchError('cleanup_timeout')
        failure.rate_limited_until = 4600
        now = [1000.0]
        with patch.object(s.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(ra, '_run', side_effect=[failure, {'texts': ['QQQ'], 'rate_limited_until': 0, 'source_expires': 4900}]) as run:
            with self.assertRaisesRegex(s.RedditFetchError, 'cleanup_timeout'):
                reader.read()
            now[0] = 1300
            with self.assertRaises(s.RedditFetchError):
                reader.read(force=True)
            now[0] = 4599
            with self.assertRaises(s.RedditFetchError):
                reader.read(force=True)
            self.assertEqual(run.call_count, 1)
            now[0] = 4600
            self.assertEqual(reader.read().sample_size, 1)
            self.assertEqual(run.call_count, 2)

    def test_expiry_during_scoring_does_not_publish_fresh_snapshot(self):
        reader = self.reader()
        now = [1000.0]
        reader._collect_texts = Mock(return_value=['QQQ'])
        def score(text):
            now[0] += 301
            return {'compound': .2}
        reader._analyzer_factory = lambda: Mock(polarity_scores=score)
        with patch.object(s.time, 'monotonic', side_effect=lambda: now[0]):
            with self.assertRaisesRegex(s.RedditFetchError, 'stale_data'):
                reader.read()
        self.assertIsNone(reader._cached)

    def test_cleanup_failure_keeps_observed_primary_diagnosis(self):
        from types import SimpleNamespace
        from crassus import reddit_acquisition as ra
        for output, returncode in [(b'{"error":"source_denied: HTTP 403"}\n', 0),
                                   (b'{"error":"source_denied: HTTP 403"}\n{"phase":"cleanup"}\n', 1)]:
            child = Mock(pid=12345, returncode=returncode)
            child.communicate.return_value = (output, b'')
            with patch.object(ra.subprocess, 'Popen', return_value=child), \
                 patch.object(ra, '_stop', side_effect=s.RedditFetchError('cleanup_timeout: fixture')), \
                 patch.object(ra, 'proc_visible', return_value=True), \
                 patch.object(ra, 'os', SimpleNamespace(name='posix', environ={})), \
                 patch.object(ra, '_subreaper', return_value=0, create=True):
                with self.assertRaises(s.RedditFetchError) as raised:
                    ra._run({})
            self.assertTrue(str(raised.exception).startswith('source_denied: HTTP 403'), str(raised.exception))
            self.assertIn('cleanup_timeout', str(raised.exception))

    def test_cleanup_failure_keeps_cancellation(self):
        from types import SimpleNamespace
        from crassus import reddit_acquisition as ra
        child = Mock(pid=12345)
        child.communicate.side_effect = KeyboardInterrupt
        with patch.object(ra.subprocess, 'Popen', return_value=child), \
             patch.object(ra, '_stop', side_effect=s.RedditFetchError('cleanup_timeout: fixture')), \
             patch.object(ra, 'proc_visible', return_value=True), \
             patch.object(ra, 'os', SimpleNamespace(name='posix', environ={})):
            with self.assertRaises(KeyboardInterrupt) as raised:
                ra._run({})
        self.assertIn('cleanup_timeout', raised.exception.__notes__[0])

    def test_cleanup_deadline_does_not_silently_drop_owned_descendants(self):
        from types import SimpleNamespace
        from crassus import reddit_acquisition as ra
        from crassus.supervisor import Process
        child = Mock(pid=12345)
        with patch.object(ra, '_marked_processes', return_value={42: Process(1, 111, 0, 'Z')}), \
             patch.object(ra, '_signal_process'), patch.object(ra.signal, 'SIGSTOP', 19, create=True), \
             patch.object(ra.signal, 'SIGKILL', 9, create=True), \
             patch.object(ra.time, 'monotonic', side_effect=[0, 2, 2]):
            with self.assertRaisesRegex(s.RedditFetchError, 'cleanup_timeout'):
                ra._stop(child, 'fixture')

    def test_outer_supervisor_owned_zombie_is_already_stopped(self):
        from crassus import reddit_acquisition as ra
        from crassus.supervisor import Process
        identity = Process(1, 111, 0, 'Z')
        child = Mock(pid=12345)
        with patch.object(ra, '_marked_processes', return_value={42: identity}), \
             patch.object(ra, '_read_process', return_value=identity), \
             patch.object(ra, 'Path', return_value=Mock(exists=lambda: True)), \
             patch.object(ra, '_signal_process'), \
             patch.object(ra.signal, 'SIGSTOP', 19, create=True), \
             patch.object(ra.signal, 'SIGKILL', 9, create=True), \
             patch.object(ra.os, 'WNOHANG', 1, create=True), \
             patch.object(ra.os, 'waitpid', side_effect=ChildProcessError), \
             patch.object(ra.time, 'monotonic', side_effect=[0, 0, 0, 2]), \
             patch.object(ra, '_remember_unreaped') as remember:
            ra._stop(child, 'fixture')
        remember.assert_not_called()

    def test_outer_supervisor_live_adoptee_still_reports_cleanup_timeout(self):
        from crassus import reddit_acquisition as ra
        from crassus.supervisor import Process
        identity = Process(1, 111, 0, 'S')
        child = Mock(pid=12345)
        with patch.object(ra, '_marked_processes', return_value={42: identity}), \
             patch.object(ra, '_read_process', return_value=identity), \
             patch.object(ra, 'Path', return_value=Mock(exists=lambda: True)), \
             patch.object(ra, '_signal_process'), \
             patch.object(ra.signal, 'SIGSTOP', 19, create=True), \
             patch.object(ra.signal, 'SIGKILL', 9, create=True), \
             patch.object(ra.os, 'WNOHANG', 1, create=True), \
             patch.object(ra.os, 'waitpid', side_effect=ChildProcessError), \
             patch.object(ra.time, 'monotonic', side_effect=[0, 0, 0, 2]), \
             patch.object(ra, '_remember_unreaped') as remember:
            with self.assertRaisesRegex(s.RedditFetchError, 'cleanup_timeout'):
                ra._stop(child, 'fixture')
        remember.assert_called_once_with({42: identity})

    def test_concurrent_acquisitions_do_not_change_subreaper_state(self):
        from types import SimpleNamespace
        from crassus import reddit_acquisition as ra
        state = [0]
        def subreaper(value=None):
            previous = state[0]
            if value is not None:
                state[0] = value
            return previous
        entered = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]
        children = []
        for i in range(2):
            child = Mock(pid=12345 + i, returncode=0)
            def communicate(*args, i=i, **kwargs):
                entered[i].set()
                self.assertTrue(release[i].wait(2))
                return (b'{"texts": []}\n', b'')
            child.communicate.side_effect = communicate
            children.append(child)
        with patch.object(ra.subprocess, 'Popen', side_effect=children), patch.object(ra, '_stop'), \
             patch.object(ra, 'proc_visible', return_value=True), \
             patch.object(ra, 'os', SimpleNamespace(name='posix', environ={})), \
             patch.object(ra, '_subreaper', side_effect=subreaper, create=True):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(ra._run, {})
                self.assertTrue(entered[0].wait(2))
                second = pool.submit(ra._run, {})
                self.assertTrue(entered[1].wait(2))
                release[0].set()
                first.result(2)
                release[1].set()
                second.result(2)
        self.assertEqual(state[0], 0)

    def test_date_clock_disagreement_is_explicit_and_fails_closed(self):
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime
        fixed = datetime(2026, 10, 5, tzinfo=timezone.utc)
        for delta in (-600, 600):
            headers = {'Age': '0', 'Date': format_datetime(fixed + timedelta(seconds=delta), usegmt=True)}
            with patch.object(s, 'datetime', Mock(now=lambda tz: fixed)):
                with self.assertRaisesRegex(s.RedditStaleData, 'freshness_unverifiable'):
                    s._http_expiry(headers, 'stocks', 300)

    def test_long_valid_provider_delay_is_retained_and_recovers(self):
        reader = self.reader()
        reader.subreddits = ('stocks',)
        response = Mock(status_code=429, headers={'Retry-After': '604800'})
        success = Mock(status_code=200, headers={})
        success.json.return_value = {'data': {'children': []}}
        session = Mock(get=Mock(side_effect=[response, success]))
        reader._session_factory = lambda: session
        reader._analyzer_factory = Mock
        now = [1000.0]
        with patch.object(s.time, 'monotonic', side_effect=lambda: now[0]):
            with self.assertRaises(s.RedditRateLimited):
                reader.read()
            now[0] = 1000 + 604800 - 1
            with self.assertRaises(s.RedditFetchError):
                reader.read(force=True)
            self.assertEqual(session.get.call_count, 1)
            now[0] += 1
            self.assertEqual(reader.read().sample_size, 0)
            self.assertEqual(session.get.call_count, 2)

    def test_extended_provider_cooldown_is_logged_once_per_acquisition(self):
        for delay, extended in [('604800', 604800), ('60', None)]:
            with self.subTest(delay=delay):
                reader = self.reader()
                reader.subreddits = ('stocks',)
                session = Mock()
                session.get.return_value = Mock(status_code=429, headers={'Retry-After': delay})
                reader._session_factory = lambda: session
                with patch.object(s.time, 'monotonic', return_value=1000), \
                     patch.object(s, 'event') as emit:
                    with self.assertRaises(s.RedditRateLimited):
                        reader.read()
                    for _ in range(5):
                        with self.assertRaises(s.RedditFetchError):
                            reader.read(force=True)
                emit.assert_called_once()
                self.assertEqual(emit.call_args.args[1], 'reddit_acquisition_failed')
                self.assertEqual(emit.call_args.kwargs.get('provider_cooldown_seconds'), extended)
                self.assertEqual(session.get.call_count, 1)

    def test_missing_browser_title_is_unavailable_not_neutral(self):
        reader = self.reader()
        reader._analyzer_factory = Mock()
        reader.subreddits = ('stocks',)
        reader._session_factory = lambda: Mock(get=lambda *args, **kwargs: Mock(status_code=503))
        page = Mock()
        page.goto.return_value.status = 200
        page.goto.return_value.all_headers.return_value = {}
        element = Mock(get_attribute=lambda name: 'fixture' if name == 'id' else None)
        page.query_selector_all.return_value = [element]
        reader._browser_factory = lambda: (Mock(), Mock(), Mock(new_page=lambda: page))
        with self.assertRaisesRegex(s.RedditFetchError, 'missing post-title'):
            reader.read()
        self.assertIsNone(reader._cached)
        reader._analyzer_factory.assert_not_called()

    def test_default_reader_uses_process_acquisition(self):
        from crassus import reddit_acquisition as ra
        reader = s.RedditSentimentReader(analyzer_factory=Mock)
        with patch.object(ra, 'acquire', return_value=[]) as acquire:
            self.assertEqual(reader.read().sample_size, 0)
        acquire.assert_called_once_with(reader)

    def test_retained_reap_does_not_claim_reused_or_unrelated_process(self):
        from crassus import reddit_acquisition as ra
        from crassus.supervisor import Process
        from types import SimpleNamespace
        fields = ['0'] * 22
        fields[0], fields[19] = 'Z', '222'
        identity = Process(1, 111, 0, 'Z')
        with patch.dict(ra._PENDING_REAPS, {42: identity}, clear=True), \
             patch.object(ra, 'Path', return_value=Mock(read_text=lambda: '42 (fixture) ' + ' '.join(fields))), \
             patch.object(ra, 'os', SimpleNamespace(waitpid=Mock(), WNOHANG=1)) as proc, \
             patch.object(ra, '_signal_process') as signal_process:
            ra._reap_pending()
            self.assertFalse(ra._PENDING_REAPS)
            proc.waitpid.assert_not_called()
            signal_process.assert_not_called()

    def test_retained_owned_zombie_is_reaped_on_later_call(self):
        from crassus import reddit_acquisition as ra
        from crassus.supervisor import Process
        from types import SimpleNamespace
        fields = ['0'] * 22
        fields[0], fields[19] = 'Z', '111'
        with patch.dict(ra._PENDING_REAPS, {42: Process(1, 111, 0, 'Z')}, clear=True), \
             patch.object(ra, 'Path', return_value=Mock(read_text=lambda: '42 (fixture) ' + ' '.join(fields))), \
             patch.object(ra, 'os', SimpleNamespace(waitpid=Mock(return_value=(42, 0)), WNOHANG=1)) as proc:
            ra._reap_pending()
            self.assertFalse(ra._PENDING_REAPS)
            proc.waitpid.assert_called_once_with(42, 1)

    def test_observed_denial_survives_hung_worker_cleanup(self):
        from crassus.reddit_acquisition import _run
        from crassus.supervisor import proc_visible
        if not proc_visible():
            self.skipTest('requires POSIX acquisition worker')
        code = '''
import time
from unittest.mock import Mock
from crassus import sentiment as s
from crassus.reddit_acquisition import main
original = s.RedditSentimentReader.__init__
def init(self, **kwargs):
    original(self, **kwargs)
    self._session_factory = lambda: Mock(get=lambda *args, **kwargs: Mock(status_code=403))
    self._close_browser = lambda: time.sleep(60)
s.RedditSentimentReader.__init__ = init
main()
'''
        config = dict(subreddits=['stocks'], keywords=['qqq'], post_limit=1, rate_limited_until=0)
        with self.assertRaises(s.RedditFetchError) as raised:
            _run(config, timeout_s=.3, command=[sys.executable, '-c', code])
        self.assertTrue(str(raised.exception).startswith('source_denied'), str(raised.exception))
        self.assertIn('cleanup_timeout', str(raised.exception))

if __name__ == '__main__':
    unittest.main()
