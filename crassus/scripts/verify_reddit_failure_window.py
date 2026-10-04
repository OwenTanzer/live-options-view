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
from unittest.mock import Mock, patch

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
            ({'data': {'children': []}}, {'Date': 'Mon, 01 Jan 2001 00:00:00 GMT'}, 'stale_data'),
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
        stop.assert_called_once_with(child)
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

if __name__ == '__main__':
    unittest.main()
