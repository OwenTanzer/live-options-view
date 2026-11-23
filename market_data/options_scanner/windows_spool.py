"""Private, disposable disk spool for the local windows builder.

No source files are changed. Bounds reject the whole build, never truncate it.
Encounter order is retained, including duplicate-symbol attribution order.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BuildLimits:
    max_row_bytes: int = 64 * 1024
    max_source_bytes: int = 16 * 1024 * 1024
    max_symbol_key_bytes: int = 16 * 1024 * 1024
    max_symbols: int = 500_000
    max_symbol_rows: int = 20_000
    max_symbol_bytes: int = 8 * 1024 * 1024
    max_conflict_bytes: int = 32 * 1024 * 1024
    max_window_bytes: int = 512 * 1024 * 1024
    max_spool_bytes: int = 32 * 1024 * 1024 * 1024
    min_scratch_free_bytes: int = 576 * 1024 * 1024

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in vars(self).values()):
            raise ValueError("window build limits must be positive integers")
        if self.max_spool_bytes < 65536:
            raise ValueError("window spool budget must be at least 65536 bytes")


# Only fields consumed by the existing identity checks and evaluator.
_FIELDS = ('underlying', 'type', 'strike', 'expiration', 'contract_size', 'root',
           't', 'sweep', 'bid', 'ask', 'bid_ms', 'ask_ms', 'spot', 'bid_size',
           'ask_size', 'volume', 'oi')


class WindowSpool:
    def __init__(self, path: Path, limits: BuildLimits):
        self.path, self.limits = path, limits
        self.ids = {}
        self.symbol_key_bytes = 0
        self.counts = {}
        self.sizes = {}
        self.window_sizes = {}
        self.row_count = 0
        self._check_space()
        self.db = sqlite3.connect(path)
        try:
            # Rebuildable data only: no journal/WAL multiplication, bounded cache,
            # and all sorting uses the primary keys rather than an in-memory sort.
            self.db.execute('PRAGMA journal_mode=OFF')
            self.db.execute('PRAGMA synchronous=OFF')
            self.db.execute('PRAGMA cache_size=-8192')
            self.db.execute('PRAGMA temp_store=FILE')
            page_size = self.db.execute('PRAGMA page_size').fetchone()[0]
            self.db.execute(f'PRAGMA max_page_count={limits.max_spool_bytes // page_size}')
            self.db.execute('CREATE TABLE observations (symbol INTEGER, seq INTEGER, payload TEXT, PRIMARY KEY(symbol,seq)) WITHOUT ROWID')
            self.db.execute('CREATE TABLE results (window TEXT, seq INTEGER, payload TEXT, PRIMARY KEY(window,seq)) WITHOUT ROWID')
            self._check_space()
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def _check_space(self):
        if shutil.disk_usage(self.path.parent).free < self.limits.min_scratch_free_bytes:
            raise ValueError('window spool scratch headroom exhausted; build refused')

    def __getitem__(self, symbol):
        # The replay loader's append interface; no observation list is retained.
        return _Appender(self, symbol)

    def append(self, symbol, row):
        # These were indexed (not .get) by the original builder. Missing
        # quote fields must remain malformed input, not become null quotes.
        for key in ('underlying', 'type', 'strike', 'expiration', 't', 'sweep', 'bid', 'ask'):
            row[key]
        payload = json.dumps([row.get(k) for k in _FIELDS], separators=(',', ':'))
        size = len(payload.encode('utf-8'))
        count = self.counts.get(symbol, 0) + 1
        total = self.sizes.get(symbol, 0) + size
        if count > self.limits.max_symbol_rows or total > self.limits.max_symbol_bytes:
            raise ValueError(f'window spool symbol budget exceeded: {symbol}')
        if symbol not in self.ids:
            if len(self.ids) >= self.limits.max_symbols:
                raise ValueError('window spool symbol count budget exceeded')
            key_bytes = len(symbol.encode('utf-8'))
            if self.symbol_key_bytes + key_bytes > self.limits.max_symbol_key_bytes:
                raise ValueError('window spool symbol key byte budget exceeded')
            self.symbol_key_bytes += key_bytes
            self.ids[symbol] = len(self.ids)
        self.counts[symbol], self.sizes[symbol] = count, total
        self.db.execute('INSERT INTO observations VALUES (?,?,?)', (self.ids[symbol], count, payload))
        self.row_count += 1
        if self.row_count % 1000 == 0:
            self.db.commit()
            self._check_space()

    def items(self):
        self.db.commit()
        for symbol, ident in self.ids.items():
            yield symbol, [dict(zip(_FIELDS, json.loads(payload))) for (payload,) in
                           self.db.execute('SELECT payload FROM observations WHERE symbol=? ORDER BY seq', (ident,))]

    def add_result(self, window, attribution, row):
        payload = json.dumps([attribution, row], separators=(',', ':'))
        total, count = self.window_sizes.get(window, (0, 0))
        total += len(payload.encode('utf-8'))
        if total > self.limits.max_window_bytes:
            raise ValueError(f'window result budget exceeded: {window}')
        self.window_sizes[window] = (total, count + 1)
        self.db.execute('INSERT INTO results VALUES (?,?,?)', (window, count, payload))
        if count % 1000 == 0:
            self.db.commit()
            self._check_space()

    def results(self, window):
        self.db.commit()
        return [json.loads(payload) for (payload,) in self.db.execute(
            'SELECT payload FROM results WHERE window=? ORDER BY seq', (window,))]


class _Appender:
    def __init__(self, spool, symbol):
        self.spool, self.symbol = spool, symbol

    def append(self, row):
        self.spool.append(self.symbol, row)
