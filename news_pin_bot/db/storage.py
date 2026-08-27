"""Local SQLite storage. One file, no server, no external dependency.

This is also the "self-tuning" substrate: every headline, every pin, and
every unexplained move gets logged with its outcome, so accuracy stats can
be computed per source/keyword/ticker later instead of trusting a fixed
heuristic forever.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS headlines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    external_id TEXT,
    symbols TEXT,
    headline TEXT NOT NULL,
    summary TEXT,
    url TEXT,
    published_at REAL,
    ingested_at REAL NOT NULL,
    is_duplicate_of INTEGER,
    impact_score REAL,
    impact_reasoning TEXT,
    scorer TEXT,
    UNIQUE(source, external_id)
);

CREATE TABLE IF NOT EXISTS pins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    headline_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    window_start REAL NOT NULL,
    window_end REAL,
    price_before REAL,
    price_after REAL,
    pct_move REAL,
    volume_ratio REAL,
    confirmed INTEGER NOT NULL DEFAULT 0,
    posted_to_discord INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    FOREIGN KEY(headline_id) REFERENCES headlines(id)
);

CREATE TABLE IF NOT EXISTS unexplained_moves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    ts REAL NOT NULL,
    pct_move REAL,
    zscore REAL,
    volume_ratio REAL,
    matched_headline_id INTEGER,
    posted_to_discord INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(matched_headline_id) REFERENCES headlines(id)
);

CREATE INDEX IF NOT EXISTS idx_headlines_published ON headlines(published_at);
CREATE INDEX IF NOT EXISTS idx_pins_symbol ON pins(symbol);
CREATE INDEX IF NOT EXISTS idx_unexplained_symbol_ts ON unexplained_moves(symbol, ts);
"""


class Storage:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def insert_headline(
        self,
        *,
        source: str,
        external_id: str | None,
        symbols: list[str],
        headline: str,
        summary: str = "",
        url: str = "",
        published_at: float | None,
        is_duplicate_of: int | None = None,
    ) -> int | None:
        """Returns the new row id, or None if this (source, external_id)
        was already ingested (UNIQUE constraint) -- the caller treats that
        as "already seen, skip"."""
        with self._connect() as conn:
            try:
                cur = conn.execute(
                    """INSERT INTO headlines
                       (source, external_id, symbols, headline, summary, url,
                        published_at, ingested_at, is_duplicate_of)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        source, external_id, ",".join(symbols), headline, summary, url,
                        published_at, time.time(), is_duplicate_of,
                    ),
                )
                return cur.lastrowid
            except sqlite3.IntegrityError:
                return None

    def set_impact_score(self, headline_id: int, score: float, reasoning: str, scorer: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE headlines SET impact_score = ?, impact_reasoning = ?, scorer = ? WHERE id = ?",
                (score, reasoning, scorer, headline_id),
            )

    def recent_headline_texts(self, symbol: str, since_ts: float) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT headline FROM headlines
                   WHERE published_at >= ? AND (',' || symbols || ',') LIKE ?
                   ORDER BY published_at DESC LIMIT 50""",
                (since_ts, f"%,{symbol},%"),
            ).fetchall()
            return [r["headline"] for r in rows]

    def create_pin(self, *, headline_id: int, symbol: str, window_start: float,
                    price_before: float) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO pins
                   (headline_id, symbol, window_start, price_before, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (headline_id, symbol, window_start, price_before, time.time()),
            )
            return cur.lastrowid

    def resolve_pin(self, pin_id: int, *, price_after: float, pct_move: float,
                     volume_ratio: float, confirmed: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE pins SET window_end = ?, price_after = ?, pct_move = ?,
                   volume_ratio = ?, confirmed = ? WHERE id = ?""",
                (time.time(), price_after, pct_move, volume_ratio, int(confirmed), pin_id),
            )

    def mark_pin_posted(self, pin_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE pins SET posted_to_discord = 1 WHERE id = ?", (pin_id,))

    def open_pins(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM pins WHERE window_end IS NULL"
            ).fetchall()

    def insert_unexplained_move(self, *, symbol: str, pct_move: float, zscore: float,
                                 volume_ratio: float) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO unexplained_moves (symbol, ts, pct_move, zscore, volume_ratio)
                   VALUES (?, ?, ?, ?, ?)""",
                (symbol, time.time(), pct_move, zscore, volume_ratio),
            )
            return cur.lastrowid

    def mark_unexplained_posted(self, row_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE unexplained_moves SET posted_to_discord = 1 WHERE id = ?", (row_id,))

    def unposted_confirmed_pins(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """SELECT p.*, h.headline, h.url, h.impact_score, h.impact_reasoning
                   FROM pins p JOIN headlines h ON h.id = p.headline_id
                   WHERE p.window_end IS NOT NULL AND p.confirmed = 1 AND p.posted_to_discord = 0"""
            ).fetchall()

    def unposted_unexplained_moves(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM unexplained_moves WHERE posted_to_discord = 0"
            ).fetchall()

    def accuracy_stats(self) -> dict[str, Any]:
        """How often a pin actually confirmed a real move -- the self-tuning
        signal. Not used to auto-adjust weights yet, just surfaced."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(confirmed) AS confirmed
                   FROM pins WHERE window_end IS NOT NULL"""
            ).fetchone()
            total = row["total"] or 0
            confirmed = row["confirmed"] or 0
            return {
                "total_pins": total,
                "confirmed_pins": confirmed,
                "hit_rate": (confirmed / total) if total else None,
            }
