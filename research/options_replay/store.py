"""Isolated trainer storage for the options Replay (OA-237 S2b).

One local SQLite file (default `<cache>/trainer.sqlite3`, gitignored), stdlib
only. It is not shared with live trading, paper accounts, the Worker, R2, KV
or D1. A write counts as acknowledged only after its transaction commits;
the server reports "saved" only from that acknowledgment.

Records
- session: hidden day + landing time, fee, revealed_at (secret until reveal)
- trade: one round trip on one contract (opens on the buy that leaves the
  contract flat->long, closes when its quantity returns to 0 or it expires)
- fill: every buy/sell/expiry with the *decision-time visible state* -- the
  exact masked view the player had when the order was placed -- stored once
  with its SHA-256; nothing ever updates it
- note + note_revision: optional, append-only; editing a note adds a revision
  and never touches fills or decision state

Masking: retrieval hides the date, weekday and source keys of any session not
yet revealed, whether read through the server or the CLI (records.py).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session (
  session_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  day TEXT NOT NULL,
  start_s INTEGER NOT NULL,
  fee REAL NOT NULL,
  revealed_at TEXT
);
CREATE TABLE IF NOT EXISTS trade (
  trade_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES session(session_id),
  attempt INTEGER NOT NULL,
  contract TEXT NOT NULL,
  opened_clock TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fill (
  fill_id TEXT PRIMARY KEY,
  trade_id TEXT NOT NULL REFERENCES trade(trade_id),
  session_id TEXT NOT NULL REFERENCES session(session_id),
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL,
  side TEXT NOT NULL,
  contract TEXT NOT NULL,
  qty INTEGER NOT NULL,
  price REAL,
  fee REAL NOT NULL,
  cash_flow REAL NOT NULL,
  net_pnl REAL,
  decision_clock TEXT NOT NULL,
  quote_time TEXT,
  quote_age_s INTEGER,
  bid REAL,
  ask REAL,
  source_key TEXT,
  decision_state TEXT NOT NULL,
  decision_state_sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS note (
  note_id TEXT PRIMARY KEY,
  trade_id TEXT NOT NULL UNIQUE REFERENCES trade(trade_id),
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS note_revision (
  note_id TEXT NOT NULL REFERENCES note(note_id),
  rev INTEGER NOT NULL,
  text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (note_id, rev)
);
CREATE TRIGGER IF NOT EXISTS fill_immutable BEFORE UPDATE ON fill
  BEGIN SELECT RAISE(ABORT, 'fills are immutable'); END;
CREATE TRIGGER IF NOT EXISTS fill_no_delete BEFORE DELETE ON fill
  BEGIN SELECT RAISE(ABORT, 'fills are immutable'); END;
CREATE TRIGGER IF NOT EXISTS note_revision_immutable BEFORE UPDATE ON note_revision
  BEGIN SELECT RAISE(ABORT, 'note revisions are append-only'); END;
"""

MAX_NOTE_CHARS = 4000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def state_digest(state: dict) -> tuple[str, str]:
    text = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return text, hashlib.sha256(text.encode()).hexdigest()


class StoreError(RuntimeError):
    pass


class TrainerStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")      # commit = on disk
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        self._db.execute("INSERT OR IGNORE INTO meta(k, v) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    def close(self):
        self._db.close()

    def _tx(self, fn):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                out = fn(self._db)
                self._db.execute("COMMIT")
                return out
            except Exception:
                self._db.execute("ROLLBACK")
                raise

    # -- writes ---------------------------------------------------------------

    def add_session(self, session_id: str, day: str, start_s: int, fee: float) -> None:
        self._tx(lambda db: db.execute(
            "INSERT INTO session(session_id, created_at, day, start_s, fee) VALUES (?,?,?,?,?)",
            (session_id, _now(), day, start_s, fee)))

    def mark_revealed(self, session_id: str) -> None:
        self._tx(lambda db: db.execute(
            "UPDATE session SET revealed_at = COALESCE(revealed_at, ?) WHERE session_id = ?",
            (_now(), session_id)))

    def record_fill(self, *, session_id: str, trade_id: str, new_trade: bool, attempt: int,
                    fill: dict, decision_state: dict) -> dict:
        """Persist one fill (and its trade on first fill). Returns the acknowledgment."""
        text, digest = state_digest(decision_state)
        fill_id = new_id("fl")
        created = _now()

        def run(db):
            if new_trade:
                db.execute("INSERT INTO trade(trade_id, session_id, attempt, contract, opened_clock, created_at)"
                           " VALUES (?,?,?,?,?,?)",
                           (trade_id, session_id, attempt, fill["contract"], fill["time_of_day"], created))
            seq = db.execute("SELECT COUNT(*) FROM fill WHERE trade_id = ?", (trade_id,)).fetchone()[0] + 1
            db.execute(
                "INSERT INTO fill(fill_id, trade_id, session_id, seq, kind, side, contract, qty, price, fee,"
                " cash_flow, net_pnl, decision_clock, quote_time, quote_age_s, bid, ask, source_key,"
                " decision_state, decision_state_sha256, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (fill_id, trade_id, session_id, seq, fill["kind"], fill["side"], fill["contract"], fill["qty"],
                 fill["price"], fill["fee"], fill["cash_flow"], fill.get("net_pnl"), fill["time_of_day"],
                 fill.get("quote_time"), fill.get("quote_age_s"), fill.get("bid"), fill.get("ask"),
                 fill.get("source_key"), text, digest, created))
            return seq
        seq = self._tx(run)
        return {"trade_id": trade_id, "fill_id": fill_id, "seq": seq, "saved_at": created,
                "decision_state_sha256": digest}

    def add_note_revision(self, trade_id: str, text: str) -> dict:
        if not isinstance(text, str):
            raise StoreError("note text must be a string")
        if len(text) > MAX_NOTE_CHARS:
            raise StoreError(f"note longer than {MAX_NOTE_CHARS} characters")
        created = _now()

        def run(db):
            if db.execute("SELECT 1 FROM trade WHERE trade_id = ?", (trade_id,)).fetchone() is None:
                raise StoreError("unknown trade")
            row = db.execute("SELECT note_id FROM note WHERE trade_id = ?", (trade_id,)).fetchone()
            if row is None:
                note_id = new_id("nt")
                db.execute("INSERT INTO note(note_id, trade_id, created_at) VALUES (?,?,?)",
                           (note_id, trade_id, created))
            else:
                note_id = row["note_id"]
            last = db.execute("SELECT rev, text FROM note_revision WHERE note_id = ? ORDER BY rev DESC LIMIT 1",
                              (note_id,)).fetchone()
            if last is not None and last["text"] == text:
                return note_id, last["rev"], False
            rev = (last["rev"] if last else 0) + 1
            db.execute("INSERT INTO note_revision(note_id, rev, text, created_at) VALUES (?,?,?,?)",
                       (note_id, rev, text, created))
            return note_id, rev, True
        note_id, rev, changed = self._tx(run)
        return {"trade_id": trade_id, "note_id": note_id, "rev": rev, "changed": changed, "saved_at": created}

    # -- reads (masked) ------------------------------------------------------

    def trades(self, session_id: str | None = None, trade_id: str | None = None,
               include_state: bool = True) -> list[dict]:
        db = self._db
        q = ("SELECT t.*, s.day, s.start_s, s.fee AS session_fee, s.revealed_at FROM trade t "
             "JOIN session s USING (session_id)")
        args: tuple = ()
        if trade_id:
            q += " WHERE t.trade_id = ?"
            args = (trade_id,)
        elif session_id:
            q += " WHERE t.session_id = ?"
            args = (session_id,)
        q += " ORDER BY t.created_at"
        with self._lock:
            rows = db.execute(q, args).fetchall()
            out = []
            for t in rows:
                revealed = t["revealed_at"] is not None
                fills = db.execute("SELECT * FROM fill WHERE trade_id = ? ORDER BY seq", (t["trade_id"],)).fetchall()
                note = db.execute("SELECT * FROM note WHERE trade_id = ?", (t["trade_id"],)).fetchone()
                revs = [] if note is None else db.execute(
                    "SELECT rev, text, created_at FROM note_revision WHERE note_id = ? ORDER BY rev",
                    (note["note_id"],)).fetchall()
                rec = {
                    "trade_id": t["trade_id"], "session_id": t["session_id"], "attempt": t["attempt"],
                    "contract": t["contract"], "opened_clock": t["opened_clock"], "created_at": t["created_at"],
                    "revealed": revealed,
                    "day": t["day"] if revealed else None,
                    "weekday": datetime.fromisoformat(t["day"]).strftime("%A") if revealed else None,
                    "fills": [],
                    "note": None if note is None else {
                        "note_id": note["note_id"], "created_at": note["created_at"],
                        "current": dict(revs[-1]) if revs else None,
                        "revisions": [dict(r) for r in revs]},
                }
                qty = 0
                for f in fills:
                    d = {k: f[k] for k in f.keys() if k not in ("decision_state", "source_key")}
                    d["source_key"] = f["source_key"] if revealed else ("(hidden until reveal)" if f["source_key"] else None)
                    if include_state:
                        d["decision_state"] = json.loads(f["decision_state"])
                    d["decision_state_verified"] = (
                        hashlib.sha256(f["decision_state"].encode()).hexdigest() == f["decision_state_sha256"])
                    qty += f["qty"] if f["side"] == "buy" else -f["qty"]
                    rec["fills"].append(d)
                rec["open_qty"] = qty
                rec["status"] = "open" if qty > 0 else ("expired" if any(f["kind"] == "expiry" for f in fills) else "closed")
                rec["gross_premium"] = round(sum((f["price"] or 0) * 100 * f["qty"] * (-1 if f["side"] == "buy" else 1)
                                                 for f in fills), 2)
                rec["fees"] = round(sum(f["fee"] for f in fills), 2)
                rec["net"] = round(sum(f["cash_flow"] for f in fills), 2) if qty == 0 else None
                out.append(rec)
            return out
