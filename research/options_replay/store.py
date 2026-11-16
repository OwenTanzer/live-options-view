"""Isolated trainer storage for the options Replay (OA-237 S2b, S2c).

One local SQLite file (default `<cache>/trainer.sqlite3`, gitignored), stdlib
only. It is not shared with live trading, paper accounts, the Worker, R2, KV
or D1. A write counts as acknowledged only after its transaction commits;
the server reports "saved" only from that acknowledgment.

Records
- session: hidden day + landing time, fee, persisted clock/attempt (so an
  unrevealed session can be resumed after a restart), revealed_at
- trade: one round trip on one contract
- fill: every buy/sell/expiry with the decision-time visible state + SHA-256;
  immutable (no UPDATE/DELETE)
- fill_void: undo. An immutable record that voids one fill; the fill itself
  is never changed or removed, so undo stays auditable
- note + note_revision: optional, append-only; each revision names the
  revision it was based on, and a stale base is refused, never overwritten
- request: idempotency log. A client request id is committed in the same
  transaction as its effect; a retry with the same id returns the original
  acknowledgment instead of applying twice (lost responses, double clicks,
  retries after reconnect)
- attempt_log: resets (landing time, fresh ledger) with how far each attempt saw

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

SCHEMA_VERSION = 2

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
CREATE TABLE IF NOT EXISTS fill_void (
  void_id TEXT PRIMARY KEY,
  fill_id TEXT NOT NULL UNIQUE REFERENCES fill(fill_id),
  session_id TEXT NOT NULL REFERENCES session(session_id),
  reason TEXT NOT NULL,
  voided_at_clock TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS request (
  request_id TEXT PRIMARY KEY,
  session_id TEXT,
  kind TEXT NOT NULL,
  body_sha256 TEXT NOT NULL,
  ack TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attempt_log (
  session_id TEXT NOT NULL REFERENCES session(session_id),
  attempt INTEGER NOT NULL,
  reset_at TEXT NOT NULL,
  seen_until TEXT NOT NULL,
  PRIMARY KEY (session_id, attempt)
);
CREATE TRIGGER IF NOT EXISTS fill_immutable BEFORE UPDATE ON fill
  BEGIN SELECT RAISE(ABORT, 'fills are immutable'); END;
CREATE TRIGGER IF NOT EXISTS fill_no_delete BEFORE DELETE ON fill
  BEGIN SELECT RAISE(ABORT, 'fills are immutable'); END;
CREATE TRIGGER IF NOT EXISTS note_revision_immutable BEFORE UPDATE ON note_revision
  BEGIN SELECT RAISE(ABORT, 'note revisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS fill_void_immutable BEFORE UPDATE ON fill_void
  BEGIN SELECT RAISE(ABORT, 'voids are immutable'); END;
CREATE TRIGGER IF NOT EXISTS fill_void_no_delete BEFORE DELETE ON fill_void
  BEGIN SELECT RAISE(ABORT, 'voids are immutable'); END;
CREATE TRIGGER IF NOT EXISTS request_immutable BEFORE UPDATE ON request
  BEGIN SELECT RAISE(ABORT, 'request log is append-only'); END;
"""

# session columns added in schema 2 (persisted clock for resume)
SESSION_V2_COLUMNS = (("clock_s", "INTEGER"), ("furthest_s", "INTEGER"),
                      ("attempt", "INTEGER NOT NULL DEFAULT 1"), ("updated_at", "TEXT"))

MAX_NOTE_CHARS = 4000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def state_digest(state: dict) -> tuple[str, str]:
    text = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return text, hashlib.sha256(text.encode()).hexdigest()


def body_digest(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _sim_label(day: str) -> dict | None:
    """Simulation sessions are stored as SIM:<generator>:<seed>:<inputs digest prefix>.
    Nothing in them is date-bearing, so they are shown whether or not revealed."""
    if not day.startswith("SIM:"):
        return None
    _, ver, seed, digest = day.split(":")
    return {"generator_version": ver, "seed": int(seed), "inputs_digest_prefix": digest}


def _weekday(day: str) -> str | None:
    try:
        return datetime.fromisoformat(day.removeprefix("TAPE:")).strftime("%A")
    except ValueError:
        return None


class StoreError(RuntimeError):
    pass


class ConflictError(StoreError):
    """The write was based on state that is no longer current; nothing was written."""

    def __init__(self, msg: str, current: dict | None = None):
        super().__init__(msg)
        self.current = current


class InjectedFault(StoreError):
    pass


class TrainerStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")      # commit = on disk
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        have = {r["name"] for r in self._db.execute("PRAGMA table_info(session)")}
        for col, decl in SESSION_V2_COLUMNS:
            if col not in have:
                self._db.execute(f"ALTER TABLE session ADD COLUMN {col} {decl}")
        self._db.execute("INSERT OR REPLACE INTO meta(k, v) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    def close(self):
        self._db.close()

    def _tx(self, fn):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                out = fn(self._db)
                self._db.execute("COMMIT")
                return out
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    # -- request log (idempotency) -------------------------------------------

    def get_request(self, request_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM request WHERE request_id = ?", (request_id,)).fetchone()
        if r is None:
            return None
        return {"request_id": r["request_id"], "session_id": r["session_id"], "kind": r["kind"],
                "body_sha256": r["body_sha256"], "ack": json.loads(r["ack"]), "created_at": r["created_at"]}

    @staticmethod
    def _log_request(db, request: dict | None, session_id: str | None, kind: str, ack: dict):
        if not request:
            return
        db.execute("INSERT INTO request(request_id, session_id, kind, body_sha256, ack, created_at)"
                   " VALUES (?,?,?,?,?,?)",
                   (request["id"], session_id, kind, request["body_sha256"], json.dumps(ack), _now()))

    # -- sessions -----------------------------------------------------------

    def add_session(self, session_id: str, day: str, start_s: int, fee: float) -> None:
        self._tx(lambda db: db.execute(
            "INSERT INTO session(session_id, created_at, day, start_s, fee, clock_s, furthest_s, attempt, updated_at)"
            " VALUES (?,?,?,?,?,?,?,1,?)", (session_id, _now(), day, start_s, fee, start_s, start_s, _now())))

    def save_clock(self, session_id: str, clock_s: int, furthest_s: int, attempt: int) -> None:
        self._tx(lambda db: db.execute(
            "UPDATE session SET clock_s = ?, furthest_s = ?, attempt = ?, updated_at = ? WHERE session_id = ?",
            (clock_s, furthest_s, attempt, _now(), session_id)))

    def log_reset(self, session_id: str, attempt: int, seen_until: str, new_clock: int,
                  request: dict | None = None) -> dict:
        ack = {"session_id": session_id, "reset_attempt": attempt, "new_attempt": attempt + 1}

        def run(db):
            db.execute("INSERT INTO attempt_log(session_id, attempt, reset_at, seen_until) VALUES (?,?,?,?)",
                       (session_id, attempt, _now(), seen_until))
            db.execute("UPDATE session SET clock_s = ?, furthest_s = ?, attempt = ?, updated_at = ? "
                       "WHERE session_id = ?", (new_clock, new_clock, attempt + 1, _now(), session_id))
            self._log_request(db, request, session_id, "reset", ack)
        self._tx(run)
        return ack

    def mark_revealed(self, session_id: str) -> None:
        self._tx(lambda db: db.execute(
            "UPDATE session SET revealed_at = COALESCE(revealed_at, ?) WHERE session_id = ?",
            (_now(), session_id)))

    def session_row(self, session_id: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM session WHERE session_id = ?", (session_id,)).fetchone()
        return dict(r) if r else None

    def attempt_rows(self, session_id: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM attempt_log WHERE session_id = ? ORDER BY attempt", (session_id,))]

    def session_fills(self, session_id: str) -> list[dict]:
        """Every stored fill of a session, oldest first, with attempt and void status (unmasked:
        for the server's own ledger rebuild, never sent to a player as-is)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT f.*, t.attempt, v.void_id, v.created_at AS voided_at FROM fill f "
                "JOIN trade t USING (trade_id) LEFT JOIN fill_void v USING (fill_id) "
                "WHERE f.session_id = ? ORDER BY f.created_at, f.rowid", (session_id,)).fetchall()
        return [dict(r) for r in rows]

    def sessions(self) -> list[dict]:
        """Masked session list for resume/finish."""
        with self._lock:
            rows = self._db.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM trade t WHERE t.session_id = s.session_id) AS trades, "
                "(SELECT COUNT(*) FROM fill f WHERE f.session_id = s.session_id) AS fills "
                "FROM session s ORDER BY s.created_at").fetchall()
        out = []
        for r in rows:
            revealed = r["revealed_at"] is not None
            clock = r["clock_s"] if r["clock_s"] is not None else r["start_s"]
            out.append({"session_id": r["session_id"], "created_at": r["created_at"], "revealed": revealed,
                        "mode": "simulation" if r["day"].startswith("SIM:") else "replay",
                        "simulation": _sim_label(r["day"]),
                        "day": (None if r["day"].startswith("SIM:") else r["day"].removeprefix("TAPE:")) if revealed else None,
                        "clock": f"{clock // 3600:02d}:{clock % 3600 // 60:02d}:{clock % 60:02d}",
                        "attempt": r["attempt"], "trades": r["trades"], "fills": r["fills"],
                        "updated_at": r["updated_at"]})
        return out

    # -- fills, voids ---------------------------------------------------------

    def record_fill(self, *, session_id: str, trade_id: str, new_trade: bool, attempt: int,
                    fill: dict, decision_state: dict, request: dict | None = None,
                    fault: str | None = None) -> dict:
        """Persist one fill (and its trade on first fill) plus its request id, atomically."""
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
            ack = {"trade_id": trade_id, "fill_id": fill_id, "seq": seq, "saved_at": created,
                   "decision_state_sha256": digest}
            self._log_request(db, request, session_id, "order", ack)
            if fault == "fail_write":
                raise InjectedFault("injected storage failure before commit")
            return ack
        return self._tx(run)

    def void_fill(self, *, session_id: str, fill_id: str, reason: str, clock: str,
                  request: dict | None = None) -> dict:
        void_id = new_id("vd")
        created = _now()

        def run(db):
            if db.execute("SELECT 1 FROM fill WHERE fill_id = ? AND session_id = ?",
                          (fill_id, session_id)).fetchone() is None:
                raise StoreError("unknown fill")
            if db.execute("SELECT 1 FROM fill_void WHERE fill_id = ?", (fill_id,)).fetchone():
                raise ConflictError("fill already voided")
            db.execute("INSERT INTO fill_void(void_id, fill_id, session_id, reason, voided_at_clock, created_at)"
                       " VALUES (?,?,?,?,?,?)", (void_id, fill_id, session_id, reason, clock, created))
            ack = {"void_id": void_id, "fill_id": fill_id, "saved_at": created}
            self._log_request(db, request, session_id, "undo", ack)
            return ack
        return self._tx(run)

    # -- notes ----------------------------------------------------------------

    def add_note_revision(self, trade_id: str, text: str, base_rev: int | None = None,
                          request: dict | None = None) -> dict:
        """Append a revision. `base_rev` is the revision the editor started from (0 = none);
        if a newer revision exists the write is refused with ConflictError (nothing written)."""
        if not isinstance(text, str):
            raise StoreError("note text must be a string")
        if len(text) > MAX_NOTE_CHARS:
            raise StoreError(f"note longer than {MAX_NOTE_CHARS} characters")
        created = _now()

        def run(db):
            if db.execute("SELECT 1 FROM trade WHERE trade_id = ?", (trade_id,)).fetchone() is None:
                raise StoreError("unknown trade")
            row = db.execute("SELECT note_id FROM note WHERE trade_id = ?", (trade_id,)).fetchone()
            note_id = row["note_id"] if row else None
            last = None if note_id is None else db.execute(
                "SELECT rev, text, created_at FROM note_revision WHERE note_id = ? ORDER BY rev DESC LIMIT 1",
                (note_id,)).fetchone()
            current_rev = last["rev"] if last else 0
            if base_rev is not None and int(base_rev) != current_rev:
                raise ConflictError(f"note changed since revision {base_rev} (now revision {current_rev})",
                                    {"rev": current_rev, "text": last["text"] if last else ""})
            if note_id is None:
                note_id = new_id("nt")
                db.execute("INSERT INTO note(note_id, trade_id, created_at) VALUES (?,?,?)",
                           (note_id, trade_id, created))
            if last is not None and last["text"] == text:
                ack = {"trade_id": trade_id, "note_id": note_id, "rev": current_rev, "changed": False,
                       "saved_at": last["created_at"]}
            else:
                db.execute("INSERT INTO note_revision(note_id, rev, text, created_at) VALUES (?,?,?,?)",
                           (note_id, current_rev + 1, text, created))
                ack = {"trade_id": trade_id, "note_id": note_id, "rev": current_rev + 1, "changed": True,
                       "saved_at": created}
            sid = db.execute("SELECT session_id FROM trade WHERE trade_id = ?", (trade_id,)).fetchone()[0]
            self._log_request(db, request, sid, "note", ack)
            return ack
        return self._tx(run)

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
                fills = db.execute("SELECT f.*, v.void_id, v.reason AS void_reason, v.voided_at_clock, "
                                   "v.created_at AS voided_at FROM fill f LEFT JOIN fill_void v USING (fill_id) "
                                   "WHERE f.trade_id = ? ORDER BY f.seq", (t["trade_id"],)).fetchall()
                note = db.execute("SELECT * FROM note WHERE trade_id = ?", (t["trade_id"],)).fetchone()
                revs = [] if note is None else db.execute(
                    "SELECT rev, text, created_at FROM note_revision WHERE note_id = ? ORDER BY rev",
                    (note["note_id"],)).fetchall()
                rec = {
                    "trade_id": t["trade_id"], "session_id": t["session_id"], "attempt": t["attempt"],
                    "contract": t["contract"], "opened_clock": t["opened_clock"], "created_at": t["created_at"],
                    "revealed": revealed,
                    "mode": "simulation" if t["day"].startswith("SIM:") else "replay",
                    "simulation": _sim_label(t["day"]),
                    "day": (None if t["day"].startswith("SIM:") else t["day"].removeprefix("TAPE:")) if revealed else None,
                    "weekday": _weekday(t["day"]) if revealed else None,
                    "fills": [],
                    "note": None if note is None else {
                        "note_id": note["note_id"], "created_at": note["created_at"],
                        "current": dict(revs[-1]) if revs else None,
                        "revisions": [dict(r) for r in revs]},
                }
                qty, live = 0, []
                for f in fills:
                    d = {k: f[k] for k in f.keys() if k not in ("decision_state", "source_key")}
                    d["voided"] = f["void_id"] is not None
                    d["source_key"] = f["source_key"] if revealed else ("(hidden until reveal)" if f["source_key"] else None)
                    if include_state:
                        d["decision_state"] = json.loads(f["decision_state"])
                    d["decision_state_verified"] = (
                        hashlib.sha256(f["decision_state"].encode()).hexdigest() == f["decision_state_sha256"])
                    if not d["voided"]:
                        qty += f["qty"] if f["side"] == "buy" else -f["qty"]
                        live.append(f)
                    rec["fills"].append(d)
                rec["open_qty"] = qty
                if not live:
                    rec["status"] = "voided"
                elif qty > 0:
                    rec["status"] = "open"
                else:
                    rec["status"] = "expired" if any(f["kind"] == "expiry" for f in live) else "closed"
                rec["gross_premium"] = round(sum((f["price"] or 0) * 100 * f["qty"] * (-1 if f["side"] == "buy" else 1)
                                                 for f in live), 2)
                rec["fees"] = round(sum(f["fee"] for f in live), 2)
                rec["net"] = round(sum(f["cash_flow"] for f in live), 2) if (qty == 0 and live) else None
                out.append(rec)
            return out
