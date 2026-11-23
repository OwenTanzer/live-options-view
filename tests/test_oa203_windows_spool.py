"""Offline bounds/cleanup regressions for the disk-backed live builder."""
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from test_oa203_windows_live import _write_day, DAYS
from market_data.options_scanner import windows_live as live
from market_data.options_scanner.windows_spool import BuildLimits, WindowSpool


def _row(**kwargs):
    return dict(underlying='AAA', type='call', strike=100, expiration='2026-10-09',
                t=1, sweep=1, bid=None, ask=None, **kwargs)


class SpoolTests(unittest.TestCase):
    def test_compact_rows_preserve_encounter_order_and_none(self):
        with tempfile.TemporaryDirectory() as td:
            spool = WindowSpool(Path(td) / 'test.sqlite', BuildLimits())
            try:
                spool['ZZZ'].append({**_row(), 'underlying': 'B', 't': 2})
                spool['AAA'].append({**_row(), 'underlying': 'A', 't': 1})
                spool['ZZZ'].append({**_row(), 'underlying': 'A', 't': 1, 'contract_size': None})
                rows = list(spool.items())
                self.assertEqual([s for s, _ in rows], ['ZZZ', 'AAA'])
                self.assertEqual([r['underlying'] for r in rows[0][1]], ['B', 'A'])
                self.assertIsNone(rows[0][1][0]['contract_size'])
            finally:
                spool.close()

    def test_resource_limits_abort_before_outputs_and_cleanup(self):
        cases = [dict(max_symbol_key_bytes=1), dict(max_symbols=1), dict(max_symbol_rows=1),
                 dict(max_symbol_bytes=1), dict(max_window_bytes=1),
                 dict(max_source_bytes=1), dict(max_row_bytes=1),
                 dict(min_scratch_free_bytes=10**30)]
        for changes in cases:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                days = [_write_day(root, d) for d in DAYS[:2]]
                before = {p: p.read_bytes() for d in days for p in d.rglob('*') if p.is_file()}
                with self.assertRaises(ValueError):
                    live.build_windows(days, {'AAA', 'BBB', 'CCC'}, root / 'emit',
                                       limits=replace(BuildLimits(), **changes))
                self.assertEqual(list((root / 'emit').iterdir()), [])
                self.assertTrue(all(p.read_bytes() == data for p, data in before.items()))

    def test_missing_quote_is_not_silently_changed_to_null(self):
        with tempfile.TemporaryDirectory() as td:
            spool = WindowSpool(Path(td) / 'test.sqlite', BuildLimits())
            try:
                for key in ('bid', 'ask'):
                    row = _row()
                    del row[key]
                    with self.assertRaises(KeyError):
                        spool['AAA'].append(row)
                spool['AAA'].append(_row())
                self.assertIsNone(list(spool.items())[0][1][0]['bid'])
            finally:
                spool.close()

    def test_disk_page_budget_is_enforced(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'test.sqlite'
            spool = WindowSpool(path, replace(BuildLimits(), max_spool_bytes=65536))
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    for i in range(10000):
                        spool[str(i)].append({**_row(), 'underlying': 'A' * 1000})
                self.assertLessEqual(path.stat().st_size, 65536)
            finally:
                spool.close()

    def test_success_cleans_spool_and_retry_is_identical(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            days = [_write_day(root, d) for d in DAYS]
            reports = []
            blobs = []
            for n in range(2):
                emit = root / f'emit{n}'
                reports.append(live.build_windows(days, {'AAA', 'BBB', 'CCC'}, emit,
                                                  full_universe=True, generated_at='fixed'))
                self.assertFalse(list(emit.glob('.windows-spool-*')))
                blobs.append({str(p.relative_to(emit)): p.read_bytes()
                              for p in emit.rglob('*') if p.is_file()})
            self.assertEqual(blobs[0], blobs[1])
            self.assertEqual(reports[0], reports[1])

    def test_conflict_report_budget_aborts_and_cleans(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            days = [_write_day(root, d) for d in DAYS[:2]]
            original = live.combine.combine_rows
            def conflicting(*args):
                rows, conflicts, stats = original(*args)
                return rows, [{"symbol": "AAA", "attributions": [{"underlying": "AAA"}]}], stats
            with patch.object(live.combine, 'combine_rows', side_effect=conflicting):
                with self.assertRaisesRegex(ValueError, 'conflict report budget'):
                    live.build_windows(days, {'AAA', 'BBB', 'CCC'}, root / 'emit',
                                       limits=replace(BuildLimits(), max_conflict_bytes=1))
            self.assertFalse(list((root / 'emit').glob('.windows-spool-*')))

    def test_failed_build_never_calls_publisher(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            [_write_day(root, d) for d in DAYS[:2]]
            with patch.object(live, 'WindowSpool', side_effect=lambda path, limits: WindowSpool(
                    path, replace(limits, max_symbols=1))), \
                 patch.object(live, 'publish_windows') as publish:
                with self.assertRaises(ValueError):
                    live.run_after_finalize(root, DAYS[1], {}, emit_root=root / 'emit')
                publish.assert_not_called()


if __name__ == '__main__':
    unittest.main()
