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
    scored_at REAL,
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
    incomplete_reason TEXT,
    classification TEXT,
    shadow INTEGER NOT NULL DEFAULT 0,
    control_pct_move REAL,
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
    matched_relation TEXT,
    matched_timing_secs REAL,
    posted_to_discord INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(matched_headline_id) REFERENCES headlines(id)
);

-- Persists the anomaly scanner's per-symbol cooldown so a restart mid-move
-- doesn't immediately re-flag and re-post (MOO-170 finding 3).
CREATE TABLE IF NOT EXISTS anomaly_cooldowns (
    symbol TEXT PRIMARY KEY,
    last_flagged_at REAL NOT NULL
);

-- Sparse (ts, price, cumulative volume) samples covering the window around
-- a pin or anomaly event, enough to replay its classification/return
-- without depending on the since-evicted in-memory ring buffer (MOO-170
-- finding 2/acceptance: "replay reproduces interval returns... without
-- future information at the decision endpoint").
CREATE TABLE IF NOT EXISTS price_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,  -- 'pin' | 'anomaly'
    event_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    ts REAL NOT NULL,
    price REAL NOT NULL,
    cum_volume REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_headlines_published ON headlines(published_at);
CREATE INDEX IF NOT EXISTS idx_pins_symbol ON pins(symbol);
CREATE INDEX IF NOT EXISTS idx_unexplained_symbol_ts ON unexplained_moves(symbol, ts);
CREATE INDEX IF NOT EXISTS idx_price_observations_event ON price_observations(event_type, event_id);
"""


class Storage:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            # Existing local databases predate explicit incomplete outcomes,
            # scoring latency, replay classification/shadow observations,
            # and headline-relevance metadata (MOO-170). Each ALTER is
            # idempotent -- checked against the live column set every open
            # -- so this migration is safe to run repeatedly.
            pin_columns = {row["name"] for row in conn.execute("PRAGMA table_info(pins)")}
            if "incomplete_reason" not in pin_columns:
                conn.execute("ALTER TABLE pins ADD COLUMN incomplete_reason TEXT")
            if "classification" not in pin_columns:
                conn.execute("ALTER TABLE pins ADD COLUMN classification TEXT")
            if "shadow" not in pin_columns:
                conn.execute("ALTER TABLE pins ADD COLUMN shadow INTEGER NOT NULL DEFAULT 0")
            if "control_pct_move" not in pin_columns:
                conn.execute("ALTER TABLE pins ADD COLUMN control_pct_move REAL")

            headline_columns = {row["name"] for row in conn.execute("PRAGMA table_info(headlines)")}
            if "scored_at" not in headline_columns:
                conn.execute("ALTER TABLE headlines ADD COLUMN scored_at REAL")

            move_columns = {row["name"] for row in conn.execute("PRAGMA table_info(unexplained_moves)")}
            if "matched_relation" not in move_columns:
                conn.execute("ALTER TABLE unexplained_moves ADD COLUMN matched_relation TEXT")
            if "matched_timing_secs" not in move_columns:
                conn.execute("ALTER TABLE unexplained_moves ADD COLUMN matched_timing_secs REAL")

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
        ingested_at: float | None = None,
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
                        published_at, time.time() if ingested_at is None else ingested_at, is_duplicate_of,
                    ),
                )
                return cur.lastrowid
            except sqlite3.IntegrityError:
                return None

    def set_impact_score(self, headline_id: int, score: float, reasoning: str, scorer: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE headlines SET impact_score = ?, impact_reasoning = ?, scorer = ?,
                   scored_at = ? WHERE id = ?""",
                (score, reasoning, scorer, time.time(), headline_id),
            )

    def scoring_latency_stats(self) -> dict[str, Any]:
        """How long headlines sit in the score queue before a scorer result
        lands -- the backlog/latency visibility MOO-170 finding 1 asks for.
        Only covers headlines that have been scored; an unscored backlog
        shows up as a growing gap between this count and total headlines."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT scored_at - ingested_at AS latency FROM headlines
                   WHERE scored_at IS NOT NULL AND ingested_at IS NOT NULL"""
            ).fetchall()
        latencies = [r["latency"] for r in rows if r["latency"] is not None]
        if not latencies:
            return {"count": 0, "mean_secs": None, "max_secs": None}
        return {
            "count": len(latencies),
            "mean_secs": sum(latencies) / len(latencies),
            "max_secs": max(latencies),
        }

    def recent_headline_texts(self, symbol: str, since_ts: float) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT headline FROM headlines
                   WHERE published_at >= ? AND (',' || symbols || ',') LIKE ?
                   ORDER BY published_at DESC LIMIT 50""",
                (since_ts, f"%,{symbol},%"),
            ).fetchall()
            return [r["headline"] for r in rows]

    def candidate_headlines_for_match(self, symbol: str, center_ts: float, window_secs: float) -> list[sqlite3.Row]:
        """Headlines tagged to `symbol` published within `window_secs` of
        `center_ts`, in either direction -- the candidate pool for
        relevance matching against an anomaly (MOO-170 finding 4: a later
        arrival must be considered, not just headlines already seen)."""
        with self._connect() as conn:
            return conn.execute(
                """SELECT id, headline, url, published_at, symbols FROM headlines
                   WHERE published_at >= ? AND published_at <= ?
                     AND (',' || symbols || ',') LIKE ?
                   ORDER BY published_at""",
                (center_ts - window_secs, center_ts + window_secs, f"%,{symbol},%"),
            ).fetchall()

    def unreconciled_unexplained_moves(self, since_ts: float) -> list[sqlite3.Row]:
        """Unexplained moves with no matched headline yet, recent enough
        that a just-ingested headline could still be relevant -- feeds the
        reconciliation pass that links a *later*-arriving headline to an
        already-logged move."""
        with self._connect() as conn:
            return conn.execute(
                """SELECT * FROM unexplained_moves
                   WHERE matched_headline_id IS NULL AND ts >= ?""",
                (since_ts,),
            ).fetchall()

    def create_pin(self, *, headline_id: int, symbol: str, window_start: float,
                    price_before: float, shadow: bool = False) -> int:
        """`shadow=True` opens a non-posting observation window for a
        headline that scored below the Discord-post threshold -- it's
        measured and counted in evaluation_report() exactly like a real
        pin, just never enqueued to Discord (MOO-170 finding 6: evaluation
        needs a denominator beyond score>=5 headlines)."""
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO pins
                   (headline_id, symbol, window_start, price_before, shadow, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (headline_id, symbol, window_start, price_before, int(shadow), time.time()),
            )
            return cur.lastrowid

    def resolve_pin(self, pin_id: int, *, price_after: float, pct_move: float,
                     volume_ratio: float, confirmed: bool, window_end: float | None = None,
                     classification: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE pins SET window_end = ?, price_after = ?, pct_move = ?,
                   volume_ratio = ?, confirmed = ?, classification = ?
                   WHERE id = ? AND window_end IS NULL""",
                (time.time() if window_end is None else window_end,
                 price_after, pct_move, volume_ratio, int(confirmed), classification, pin_id),
            )

    def set_pin_control(self, pin_id: int, control_pct_move: float | None) -> None:
        """Records the approximate no-news control comparison for one pin --
        see docs/moo170_evaluation_protocol.md. `None` means no clean
        no-news window was available (a headline was found nearby), which
        is itself meaningful and excluded from the control aggregate below,
        never treated as a zero move."""
        with self._connect() as conn:
            conn.execute("UPDATE pins SET control_pct_move = ? WHERE id = ?", (control_pct_move, pin_id))

    def record_price_observations(self, *, event_type: str, event_id: int, symbol: str,
                                    samples: list[tuple[float, float, float]]) -> None:
        """Persists sparse (ts, price, cum_volume) samples for one pin/anomaly
        event so its return/classification can be replayed later without
        depending on the in-memory ring buffer, which evicts."""
        if not samples:
            return
        with self._connect() as conn:
            conn.executemany(
                """INSERT INTO price_observations (event_type, event_id, symbol, ts, price, cum_volume)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                [(event_type, event_id, symbol, ts, price, cum_volume) for ts, price, cum_volume in samples],
            )

    def price_observations_for(self, event_type: str, event_id: int) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """SELECT * FROM price_observations WHERE event_type = ? AND event_id = ?
                   ORDER BY ts""",
                (event_type, event_id),
            ).fetchall()

    def mark_pin_incomplete(self, pin_id: int, reason: str) -> None:
        """Terminate a missing-data observation without fabricating an outcome."""
        with self._connect() as conn:
            conn.execute(
                """UPDATE pins SET window_end = ?, incomplete_reason = ?,
                   price_after = NULL, pct_move = NULL, volume_ratio = NULL, confirmed = 0,
                   classification = 'insufficient_evidence'
                   WHERE id = ? AND window_end IS NULL""",
                (time.time(), reason, pin_id),
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

    def attach_matched_headline(self, move_id: int, headline_id: int, *, relation: str, timing_secs: float) -> None:
        """Links a candidate headline to a previously-logged unexplained
        move. `relation` is "preceding" (published before the move) or
        "following" (a later arrival, reconciled after the fact -- always
        explicitly dated via `timing_secs`, never presented as if it were
        known at detection time). The move row itself was already inserted
        regardless of whether a match exists (MOO-170 finding 4): matching
        is evidence attached after the fact, never a gate on logging."""
        with self._connect() as conn:
            conn.execute(
                """UPDATE unexplained_moves SET matched_headline_id = ?, matched_relation = ?,
                   matched_timing_secs = ? WHERE id = ?""",
                (headline_id, relation, timing_secs, move_id),
            )

    def mark_unexplained_posted(self, row_id: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE unexplained_moves SET posted_to_discord = 1 WHERE id = ?", (row_id,))

    def last_anomaly_flag_ts(self, symbol: str) -> float | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_flagged_at FROM anomaly_cooldowns WHERE symbol = ?", (symbol,),
            ).fetchone()
            return row["last_flagged_at"] if row else None

    def set_anomaly_flag_ts(self, symbol: str, ts: float) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO anomaly_cooldowns (symbol, last_flagged_at) VALUES (?, ?)
                   ON CONFLICT(symbol) DO UPDATE SET last_flagged_at = excluded.last_flagged_at""",
                (symbol, ts),
            )

    def unposted_confirmed_pins(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """SELECT p.*, h.headline, h.url, h.impact_score, h.impact_reasoning
                   FROM pins p JOIN headlines h ON h.id = p.headline_id
                   WHERE p.window_end IS NOT NULL AND p.confirmed = 1 AND p.posted_to_discord = 0
                     AND p.shadow = 0"""
            ).fetchall()

    def unposted_unexplained_moves(self) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                """SELECT m.*, h.headline AS matched_headline, h.url AS matched_url
                   FROM unexplained_moves m LEFT JOIN headlines h ON h.id = m.matched_headline_id
                   WHERE m.posted_to_discord = 0"""
            ).fetchall()

    def accuracy_stats(self) -> dict[str, Any]:
        """How often a *real* (non-shadow) pin actually confirmed a move --
        the headline-Discord-worthy signal. Excludes shadow observations,
        which exist only to build the evaluation denominator (see
        evaluation_report) and were never posted."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(confirmed) AS confirmed
                   FROM pins WHERE window_end IS NOT NULL AND incomplete_reason IS NULL
                     AND shadow = 0"""
            ).fetchone()
            total = row["total"] or 0
            confirmed = row["confirmed"] or 0
            return {
                "total_pins": total,
                "confirmed_pins": confirmed,
                "hit_rate": (confirmed / total) if total else None,
            }

    def evaluation_report(self) -> dict[str, Any]:
        """The MOO-170 finding 6/7 evaluation surface: outcomes across every
        scored headline (not just score>=5 posts), broken out by scorer
        identity (never pooling Ollama and VADER-fallback samples), plus
        alert-burden and coverage counts. This is descriptive only -- it
        does not auto-tune any threshold."""
        with self._connect() as conn:
            by_scorer = conn.execute(
                """SELECT h.scorer AS scorer, COUNT(*) AS total,
                          SUM(p.confirmed) AS confirmed,
                          SUM(CASE WHEN p.incomplete_reason IS NOT NULL THEN 1 ELSE 0 END) AS incomplete
                   FROM pins p JOIN headlines h ON h.id = p.headline_id
                   WHERE p.window_end IS NOT NULL
                   GROUP BY h.scorer"""
            ).fetchall()
            score_strata = conn.execute(
                """SELECT CAST(h.impact_score AS INT) AS score_bucket, COUNT(*) AS total,
                          SUM(p.confirmed) AS confirmed
                   FROM pins p JOIN headlines h ON h.id = p.headline_id
                   WHERE p.window_end IS NOT NULL AND p.incomplete_reason IS NULL
                   GROUP BY score_bucket ORDER BY score_bucket"""
            ).fetchall()
            alert_burden = conn.execute(
                """SELECT COUNT(*) AS pins_posted FROM pins WHERE posted_to_discord = 1"""
            ).fetchone()["pins_posted"]
            missed = conn.execute(
                """SELECT COUNT(*) AS total FROM unexplained_moves WHERE matched_headline_id IS NULL"""
            ).fetchone()["total"]
            reconciled_later = conn.execute(
                """SELECT COUNT(*) AS total FROM unexplained_moves WHERE matched_relation = 'following'"""
            ).fetchone()["total"]
            shadow_totals = conn.execute(
                """SELECT COUNT(*) AS total, SUM(confirmed) AS confirmed
                   FROM pins WHERE shadow = 1 AND window_end IS NOT NULL AND incomplete_reason IS NULL"""
            ).fetchone()
            control = conn.execute(
                """SELECT COUNT(*) AS n, AVG(ABS(pct_move)) AS pin_avg_abs_move,
                          AVG(ABS(control_pct_move)) AS control_avg_abs_move
                   FROM pins
                   WHERE shadow = 0 AND incomplete_reason IS NULL AND control_pct_move IS NOT NULL"""
            ).fetchone()
            control_no_clean_window = conn.execute(
                """SELECT COUNT(*) AS total FROM pins
                   WHERE shadow = 0 AND incomplete_reason IS NULL AND control_pct_move IS NULL
                     AND window_end IS NOT NULL"""
            ).fetchone()["total"]
        return {
            "by_scorer": [dict(r) for r in by_scorer],
            "score_strata": [dict(r) for r in score_strata],
            "alert_burden_pins_posted": alert_burden,
            "unexplained_moves_no_match": missed,
            "unexplained_moves_reconciled_later": reconciled_later,
            "shadow_total": shadow_totals["total"] or 0,
            "shadow_confirmed": shadow_totals["confirmed"] or 0,
            "scoring_latency": self.scoring_latency_stats(),
            # Approximate no-news control comparison (see
            # docs/moo170_evaluation_protocol.md) -- `n` is the explicit
            # denominator of pins that actually got a clean control window;
            # `control_no_clean_window` pins had a headline too close to the
            # shifted window to serve as a no-news comparison.
            "control_sample_size": control["n"] or 0,
            "control_pin_avg_abs_pct_move": control["pin_avg_abs_move"],
            "control_baseline_avg_abs_pct_move": control["control_avg_abs_move"],
            "control_no_clean_window": control_no_clean_window,
        }
