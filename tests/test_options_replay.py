"""OA-237 S1 Replay: hidden date, no future leak, recorded-quote fills, disabled stale quotes."""

import json
import random
import re
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "research" / "options_replay"))

import archive as A  # noqa: E402
from engine import MIN_DWELL_S, MULTIPLIER, DwellError, ReplayError, ReplaySession  # noqa: E402


class FakeClock:
    """Wall clock that moves 1 s per read, so dwell never blocks unless a test says so."""
    def __init__(self, step=1.0):
        self.t, self.step = 1000.0, step

    def __call__(self):
        self.t += self.step
        return self.t


def RS(pack, **kw):
    kw.setdefault("now", FakeClock())
    return ReplaySession(pack, **kw)

DAY = "2026-09-01"
OPEN, CLOSE = 9 * 3600 + 30 * 60, 16 * 3600


def board(spot: float, width: int = 3, spread: float = 0.04) -> dict:
    """Parity-consistent 0DTE board around `spot`."""
    q = {}
    base = round(spot)
    for k in range(base - width, base + width + 1):
        call_mid = max(spot - k, 0) + 0.5
        put_mid = call_mid - (spot - k)
        q[f"C{k}"] = [round(call_mid - spread / 2, 2), round(call_mid + spread / 2, 2)]
        q[f"P{k}"] = [round(put_mid - spread / 2, 2), round(put_mid + spread / 2, 2)]
    return q


def make_pack(spots_by_t: dict[int, float], overrides=None) -> dict:
    snaps = []
    for t, spot in sorted(spots_by_t.items()):
        snaps.append({"t": t, "key": f"intraday/20260901/snapshot_{t // 3600:02d}{t % 3600 // 60:02d}{t % 60:02d}000000.csv",
                      "spot": spot, "q": board(spot)})
    for fn in overrides or []:
        fn(snaps)
    A.assess(snaps)
    return {"version": 1, "day": DAY, "open_s": OPEN, "close_s": CLOSE,
            "coverage_all": A.coverage([s["t"] for s in snaps], date(2026, 9, 1)),
            "coverage_usable": A.playable_coverage(snaps, OPEN, CLOSE),
            "snapshots": snaps}


def minute_pack(start=OPEN - 120, end=CLOSE, drift=0.01):
    return make_pack({t: 700 + drift * (t - OPEN) / 60 for t in range(start, end + 1, 60)})


DATE_PATTERNS = [r"2026", r"20260901", r"260901", r"09-01", r"Tuesday", r"\bSep"]


class HiddenDateAndClock(unittest.TestCase):
    def test_view_has_no_date_bearing_values(self):
        s = RS(minute_pack(), start_s=OPEN + 3600)
        s.order("buy", "C700", 1)
        s.advance(600, max_bars=None)
        blob = json.dumps(s.view())
        for pat in DATE_PATTERNS:
            self.assertIsNone(re.search(pat, blob), pat)
        self.assertNotIn("intraday/", blob)
        self.assertNotIn("source_key", blob)

    def test_reveal_discloses_date_and_sources(self):
        s = RS(minute_pack(), start_s=OPEN + 3600)
        s.order("buy", "C700", 1)
        r = s.reveal()["reveal"]
        self.assertEqual(r["date"], DAY)
        self.assertEqual(r["weekday"], "Tuesday")
        self.assertTrue(r["reconciliation"]["lines"][0]["source_key"].startswith("intraday/20260901/"))
        with self.assertRaises(ReplayError):
            s.order("buy", "C700", 1)

    def test_no_future_data_before_reveal(self):
        s = RS(minute_pack(), start_s=OPEN + 1800 + 30)
        v = s.view()
        self.assertTrue(all(t <= s.clock for t, _ in v["spot"]["series"]))
        self.assertEqual(v["quotes"]["snapshot_time"], "10:00:00")
        self.assertNotIn("full_day_spot", json.dumps(v))

    def test_clock_only_moves_forward_and_stops_at_close(self):
        s = RS(minute_pack(), start_s=OPEN + 600)
        for bad in (0, -60):
            with self.assertRaises(ReplayError):
                s.advance(bad)
        s.advance(7 * 3600, max_bars=None)
        self.assertEqual(s.clock, CLOSE)
        self.assertTrue(s.view()["session"]["closed"])
        self.assertEqual(s.view()["session"]["seconds_to_close"], 0)

    def test_random_start_inside_session(self):
        for seed in range(20):
            s = RS(minute_pack(), rng=random.Random(seed))
            self.assertTrue(OPEN + 900 <= s.clock < CLOSE - 3600 + 60)


class Fills(unittest.TestCase):
    def test_buy_at_ask_sell_at_bid_with_fees(self):
        s = RS(minute_pack(drift=0.05), start_s=OPEN + 3600, fee_per_contract_side=0.65)
        snap_in = s.board_state()[0]
        bid0, ask0 = snap_in["q"]["C703"]
        s.order("buy", "C703", 2)
        self.assertEqual(s.fills[0]["price"], ask0)
        s.advance(1800, max_bars=None)
        snap_out = s.board_state()[0]
        bid1, _ = snap_out["q"]["C703"]
        s.order("sell", "C703", 2)
        self.assertEqual(s.fills[1]["price"], bid1)
        expected = (bid1 - ask0) * MULTIPLIER * 2 - 4 * 0.65
        self.assertAlmostEqual(s.realized, expected, places=6)
        self.assertAlmostEqual(s.fees, 2.60)
        self.assertAlmostEqual(s.cash, 10_000 + expected, places=6)
        self.assertEqual(s.view()["positions"], [])

    def test_cannot_oversell_or_short(self):
        s = RS(minute_pack(), start_s=OPEN + 3600)
        with self.assertRaises(ReplayError):
            s.order("sell", "C700", 1)
        s.order("buy", "P700", 1)
        with self.assertRaises(ReplayError):
            s.order("sell", "P700", 2)

    def test_unrealized_marks_at_bid_net_of_both_fees(self):
        s = RS(minute_pack(), start_s=OPEN + 3600, fee_per_contract_side=1.0)
        s.order("buy", "C700", 1)
        p = s.view()["positions"][0]
        bid, ask = s.board_state()[0]["q"]["C700"]
        self.assertAlmostEqual(p["unrealized_net"], round((bid - ask) * 100 - 2.0, 2))

    def test_expiry_settles_at_intrinsic_and_is_labelled(self):
        s = RS(minute_pack(drift=0.02), start_s=OPEN + 3600, fee_per_contract_side=0)
        s.order("buy", "C700", 1)
        s.advance(7 * 3600, max_bars=None)
        exp = s.fills[-1]
        self.assertEqual(exp["kind"], "expiry")
        last_spot = 700 + 0.02 * (CLOSE - OPEN) / 60
        self.assertAlmostEqual(exp["price"], round(last_spot - 700, 4), places=4)
        self.assertIn("not a market fill", exp["note"])


class DisabledQuotes(unittest.TestCase):
    def test_gap_older_than_limit_disables(self):
        spots = {t: 700.0 for t in range(OPEN - 60, CLOSE + 1, 60) if not (OPEN + 3600 < t < OPEN + 4200)}
        s = RS(make_pack(spots), start_s=OPEN + 3600 + A.MAX_QUOTE_AGE_S + 30)
        v = s.view()
        self.assertEqual(v["quotes"]["status"], "disabled")
        self.assertEqual(v["quotes"]["code"], "gap")
        self.assertIn("old", v["quotes"]["detail"])
        self.assertFalse(any(r["call"]["buy"] for r in v["board"]))
        with self.assertRaises(ReplayError):
            s.order("buy", "C700", 1)

    def test_frozen_feed_disables_after_limit(self):
        spots = {t: 700.0 + (0 if OPEN + 3600 <= t <= OPEN + 4200 else (t - OPEN) * 1e-4)
                 for t in range(OPEN - 60, CLOSE + 1, 60)}
        pack = make_pack(spots)
        s = RS(pack, start_s=OPEN + 3600 + A.MAX_QUOTE_AGE_S + 60)
        self.assertEqual(s.view()["quotes"]["code"], "frozen")
        self.assertIn("frozen", s.view()["quotes"]["detail"])

    def test_unsynchronized_underlying_disables(self):
        def skew(snaps):
            for sn in snaps:
                if sn["t"] == OPEN + 3600:
                    sn["spot"] += 1.5   # recorded underlying off its own quotes
        s = RS(make_pack({t: 700.0 + (t - OPEN) * 1e-4 for t in range(OPEN - 60, CLOSE + 1, 60)},
                                    [skew]), start_s=OPEN + 3600 + 5)
        v = s.view()
        self.assertEqual(v["quotes"]["code"], "parity")
        self.assertIn("Consistency check failed", v["quotes"]["detail"])
        self.assertIn("does not prove when they were observed", v["quotes"]["detail"])
        self.assertIsNone(v["spot"]["now"])
        self.assertTrue(all(t != OPEN + 3600 for t, _ in v["spot"]["series"]))

    def test_side_specific_missing_prices(self):
        def holes(snaps):
            for sn in snaps:
                sn["q"]["C703"] = [0.0, 0.02]     # no bid: can buy, cannot sell
                sn["q"]["C697"] = [3.0, None]      # no ask: cannot buy
                sn["q"]["P697"] = [0.5, 0.4]       # crossed
        s = RS(make_pack({t: 700.0 + (t - OPEN) * 1e-4 for t in range(OPEN - 60, CLOSE + 1, 60)}, [holes]),
                          start_s=OPEN + 3600)
        s.order("buy", "C703", 1)
        with self.assertRaises(ReplayError):
            s.order("sell", "C703", 1)
        with self.assertRaises(ReplayError):
            s.order("buy", "C697", 1)
        with self.assertRaises(ReplayError):
            s.order("buy", "P697", 1)
        self.assertIsNone(s.view()["positions"][0]["unrealized_net"])


class PacingAndControls(unittest.TestCase):
    def test_dwell_enforced_between_moves(self):
        t = [0.0]
        s = ReplaySession(minute_pack(), start_s=OPEN + 3600, now=lambda: t[0])
        s.step_bar()
        t[0] += MIN_DWELL_S - 0.01
        with self.assertRaises(DwellError):
            s.step_bar()
        t[0] += 0.02
        s.step_bar()
        with self.assertRaises(DwellError):
            s.advance(60)

    def test_advance_crosses_at_most_one_bar_by_default(self):
        s = RS(minute_pack(), start_s=OPEN + 3600 + 10)
        s.advance(3600)
        self.assertEqual(s.clock, OPEN + 3600 + 119)   # next bar at +60, capped before the one at +120
        self.assertEqual(s.view()["quotes"]["snapshot_time"], "10:31:00")

    def test_step_lands_exactly_on_next_bar(self):
        s = RS(minute_pack(), start_s=OPEN + 3600 + 10)
        s.step_bar()
        self.assertEqual(s.clock, OPEN + 3660)
        self.assertEqual(s.view()["quotes"]["age_s"], 0)

    def test_reset_returns_to_landing_with_fresh_ledger_and_keeps_attempt(self):
        s = RS(minute_pack(drift=0.05), start_s=OPEN + 3600)
        s.order("buy", "C703", 1)
        s.advance(1800, max_bars=None)
        s.order("sell", "C703", 1)
        first = s.realized
        s.advance(600, max_bars=None)
        v = s.reset()
        self.assertEqual(s.clock, OPEN + 3600)
        self.assertEqual((s.realized, s.fills, s.cash), (0.0, [], 10_000.0))
        self.assertEqual(v["session"]["attempt"], 2)
        self.assertEqual(v["attempts"][0]["realized_net"], round(first, 2))
        self.assertEqual(v["attempts"][0]["seen_until"], "11:10:00")
        self.assertNotIn("intraday/", json.dumps(v))
        r = s.reveal()["reveal"]
        self.assertAlmostEqual(r["earlier_attempts"][0]["reconciliation"]["net"], round(first, 2))

    def test_dark_stretches_are_past_only_and_explained(self):
        def skew(snaps):
            for sn in snaps:
                if OPEN + 600 <= sn["t"] < OPEN + 900:
                    sn["spot"] += 2.0
        s = RS(make_pack({t: 700.0 + (t - OPEN) * 1e-4 for t in range(OPEN - 60, CLOSE + 1, 60)}, [skew]),
               start_s=OPEN + 3600)
        d = s.view()["dark"]
        self.assertEqual([(x["start"], x["end"], x["code"]) for x in d], [(OPEN + 600, OPEN + 900, "parity")])
        s2 = RS(s.pack, start_s=OPEN + 700)
        d2 = s2.view()["dark"]
        self.assertEqual(d2[-1]["end"], OPEN + 700)       # never shows how long the stretch will last
        self.assertEqual(s2.view()["quotes"]["since"], "09:40:00")


class Reconciliation(unittest.TestCase):
    def test_reconciliation_matches_ledger_and_raw_rows(self):
        pack = minute_pack(drift=0.05)
        by_key = {sn["key"]: sn for sn in pack["snapshots"]}

        def raw(key, c):
            b, a = by_key[key]["q"][c]
            return {"key": key, "Bid": b, "Ask": a, "UnderlyingPrice": by_key[key]["spot"], "OptionSymbol": "x"}
        s = RS(pack, start_s=OPEN + 3600, fee_per_contract_side=0.65, raw_lookup=raw)
        s.order("buy", "C703", 2)
        s.advance(1800, max_bars=None)
        s.order("sell", "C703", 2)
        rc = s.reveal()["reveal"]["reconciliation"]
        self.assertTrue(rc["reconciles"])
        self.assertAlmostEqual(rc["net"], s.realized, places=2)
        self.assertEqual(rc["fees"], 2.60)
        self.assertTrue(all(l["raw_matches"] for l in rc["lines"]))
        self.assertEqual([l["decision_time"] for l in rc["lines"]], ["10:30:00 ET", "11:00:00 ET"])
        self.assertAlmostEqual(sum(l["cash_flow"] for l in rc["lines"]), rc["net"], places=2)

    def test_reconciliation_flags_a_mismatching_raw_row(self):
        pack = minute_pack()
        s = RS(pack, start_s=OPEN + 3600,
               raw_lookup=lambda k, c: {"key": k, "Bid": 9.0, "Ask": 9.5, "UnderlyingPrice": 1, "OptionSymbol": "x"})
        s.order("buy", "C700", 1)
        self.assertFalse(s.reveal()["reveal"]["reconciliation"]["lines"][0]["raw_matches"])


class ArchiveHelpers(unittest.TestCase):
    def test_parse_snapshot_key(self):
        self.assertEqual(A.parse_snapshot_key("intraday/20261113/snapshot_161416905398.csv"),
                         ("20261113", 16 * 3600 + 14 * 60 + 16))
        for bad in ("intraday/20261113/snapshot_161416905398.provenance.json",
                    "intraday/spy/20261113/snapshot_161416905398.csv", "intraday/latest.json"):
            self.assertIsNone(A.parse_snapshot_key(bad))

    def test_parse_snapshot_keeps_only_same_day_expiry(self):
        csv_text = ("TradeDate,Expiration,Strike,Type,Bid,Ask,UnderlyingPrice,QQQ\n"
                    "2026-09-01,2026-09-01,700.0,call,1.0,1.1,700.2,688.0\n"
                    "2026-09-01,2026-09-02,700.0,put,2.0,2.1,700.2,688.0\n"
                    "2026-09-01,2026-09-01,700.5,put,,0.9,700.2,688.0\n")
        snap = A.parse_snapshot(csv_text.encode(), DAY)
        self.assertEqual(snap["spot"], 700.2)          # UnderlyingPrice, never the QQQ column
        self.assertEqual(snap["q"], {"C700": [1.0, 1.1], "P700.5": [None, 0.9]})

    def test_coverage_counts_quoted_time_not_minute_slots(self):
        d = date(2026, 9, 1)
        every70 = list(range(OPEN - 30, CLOSE, 70))
        self.assertEqual(A.coverage(every70, d)["ratio"], 1.0)
        holed = [t for t in every70 if not OPEN + 3600 <= t < OPEN + 4800]
        cov = A.coverage(holed, d)
        self.assertLess(cov["ratio"], 0.97)
        self.assertGreater(cov["max_gap_s"], 1000)

    def test_playable_coverage_matches_engine_rule(self):
        def skew(snaps):
            for sn in snaps:
                if OPEN + 3600 <= sn["t"] < OPEN + 3900 or sn["t"] % 600 == 0:
                    sn["spot"] += 2.0
        spots = {t: 700.0 + (t - OPEN) * 1e-4 for t in range(OPEN - 60, CLOSE + 1, 60)
                 if not (OPEN + 7200 < t < OPEN + 7800)}
        pack = make_pack(spots, [skew])
        s = RS(pack, start_s=OPEN)
        live = sum(1 for t in range(OPEN, CLOSE) if s.board_state(t)[1] is None)
        cov = pack["coverage_usable"]
        self.assertEqual(cov["live_s"], live)
        self.assertGreaterEqual(cov["longest_dark_s"], 300)
        ok, why = A.pack_qualifies(pack)
        self.assertTrue(ok, why)
        self.assertFalse(A.pack_qualifies(pack, min_ratio=0.99)[0])

    def test_archive_module_is_read_only(self):
        src = "\n".join((ROOT / "research" / "options_replay" / f).read_text(encoding="utf-8")
                        for f in ("archive.py", "engine.py", "server.py", "qualify.py"))
        for verb in ("put_object", "delete_object", "upload_file", "copy_object", "kv ", "wrangler"):
            self.assertNotIn(verb, src)
        self.assertIsNone(re.search(r"^s*(from|import) backend", src, re.M))


class TrainerStorage(unittest.TestCase):
    """S2b: fills, decision-time state, trade identity and note revisions persist and recover."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "trainer.sqlite3"

    def _session(self, store, pack=None, **kw):
        pack = pack or minute_pack(drift=0.05)
        holder = {}

        def on_fill(sess, fill, state, new_trade):
            return store.record_fill(session_id=sess.id, trade_id=fill["trade_id"], new_trade=new_trade,
                                     attempt=len(sess.attempts) + 1, fill=fill, decision_state=state)
        s = RS(pack, start_s=OPEN + 3600, on_fill=on_fill,
               on_reveal=lambda sess: store.mark_revealed(sess.id), **kw)
        store.add_session(s.id, pack["day"], s.start_s, s.fee)
        return s

    def test_trade_identity_spans_partial_fills_and_new_round_trip_gets_new_id(self):
        from store import TrainerStore
        st = TrainerStore(self.db)
        s = self._session(st)
        s.order("buy", "C703", 2)
        s.order("buy", "C703", 1)
        s.advance(600, max_bars=None)
        s.order("sell", "C703", 2)
        s.order("sell", "C703", 1)
        s.order("buy", "C703", 1)
        ids = [f["trade_id"] for f in s.fills]
        self.assertEqual(len(set(ids[:4])), 1)
        self.assertNotEqual(ids[4], ids[0])
        recs = st.trades(session_id=s.id)
        self.assertEqual([r["status"] for r in recs], ["closed", "open"])
        self.assertEqual([f["seq"] for f in recs[0]["fills"]], [1, 2, 3, 4])
        self.assertAlmostEqual(recs[0]["net"], round(s.realized, 2), places=2)

    def test_notes_are_revisions_and_never_touch_decision_state(self):
        from store import TrainerStore
        st = TrainerStore(self.db)
        s = self._session(st)
        s.order("buy", "C700", 1)
        tid = s.fills[0]["trade_id"]
        before = st.trades(trade_id=tid)[0]["fills"][0]
        a1 = st.add_note_revision(tid, "first idea")
        a2 = st.add_note_revision(tid, "first idea, revised")
        a3 = st.add_note_revision(tid, "first idea, revised")   # identical text: no new revision
        self.assertEqual((a1["rev"], a2["rev"], a3["rev"], a3["changed"]), (1, 2, 2, False))
        after = st.trades(trade_id=tid)[0]
        self.assertEqual(after["fills"][0]["decision_state"], before["decision_state"])
        self.assertEqual(after["fills"][0]["decision_state_sha256"], before["decision_state_sha256"])
        self.assertTrue(after["fills"][0]["decision_state_verified"])
        self.assertEqual([r["text"] for r in after["note"]["revisions"]], ["first idea", "first idea, revised"])
        import sqlite3
        with self.assertRaises(sqlite3.DatabaseError):
            st._db.execute("UPDATE fill SET price = 0")
        with self.assertRaises(sqlite3.DatabaseError):
            st._db.execute("UPDATE note_revision SET text = 'x'")

    def test_decision_state_is_what_was_visible_before_the_order(self):
        from store import TrainerStore
        st = TrainerStore(self.db)
        s = self._session(st)
        visible = s.view()
        s.order("buy", "C700", 1)
        rec = st.trades(session_id=s.id)[0]["fills"][0]
        self.assertEqual(rec["decision_state"], json.loads(json.dumps(visible)))
        self.assertEqual(rec["decision_state"]["positions"], [])
        self.assertEqual(rec["decision_state"]["session"]["time_of_day"], "10:30:00 ET")

    def test_masked_until_reveal_then_unmasked_and_recovered_by_fresh_connection(self):
        from store import TrainerStore
        st = TrainerStore(self.db)
        s = self._session(st)
        s.order("buy", "C700", 1)
        blob = json.dumps(st.trades())
        for pat in DATE_PATTERNS:
            self.assertIsNone(re.search(pat, blob.replace(st.trades()[0]["created_at"], "")), pat)
        self.assertNotIn("intraday/", blob)
        s.reveal()
        st.close()
        fresh = TrainerStore(self.db)              # new connection, nothing in memory
        rec = fresh.trades()[0]
        self.assertEqual(rec["day"], DAY)
        self.assertTrue(rec["fills"][0]["source_key"].startswith("intraday/20260901/"))

    def test_storage_failure_means_order_not_placed(self):
        def broken(*a, **k):
            raise RuntimeError("disk full")
        s = RS(minute_pack(), start_s=OPEN + 3600, on_fill=broken)
        with self.assertRaises(ReplayError) as cm:
            s.order("buy", "C700", 1)
        self.assertIn("not saved", str(cm.exception))
        self.assertEqual((s.fills, s.cash, s.lots.get("C700", {}).get("qty", 0)), ([], 10_000.0, 0))

    def test_records_recovered_over_http_by_a_fresh_server(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        import server as srv
        from store import TrainerStore

        def boot():
            arc = A.Archive(self.tmp / "cache")
            g = srv.Games(arc, [DAY], 0.65, 1, force_start=OPEN + 3600, store=TrainerStore(self.db))
            g._load = lambda day: minute_pack(drift=0.05)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.make_handler(g))
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            return httpd, g, f"http://127.0.0.1:{httpd.server_address[1]}"

        def call(base, path, body=None):
            req = urllib.request.Request(base + path, None if body is None else json.dumps(body).encode(),
                                         {"Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(req))

        httpd, g, base = boot()
        v = call(base, "/api/new", {})
        sid = v["session"]["id"]
        g.sessions[sid].now = FakeClock()
        v = call(base, "/api/order", {"s": sid, "side": "buy", "contract": "C703", "qty": 1})
        call(base, "/api/advance", {"s": sid, "seconds": 600, "max_bars": None})
        v = call(base, "/api/order", {"s": sid, "side": "sell", "contract": "C703", "qty": 1})
        plain = v["fills"][0]["trade_id"]
        v = call(base, "/api/order", {"s": sid, "side": "buy", "contract": "P704", "qty": 1})
        noted = v["fills"][-1]["trade_id"]
        self.assertTrue(v["fills"][-1]["saved"]["fill_id"].startswith("fl_"))
        call(base, "/api/note", {"trade_id": noted, "text": "fade the pop"})
        call(base, "/api/note", {"trade_id": noted, "text": "fade the pop; out by 11"})
        call(base, "/api/advance", {"s": sid, "seconds": 300, "max_bars": None})
        call(base, "/api/order", {"s": sid, "side": "sell", "contract": "P704", "qty": 1})
        httpd.shutdown(); httpd.server_close(); g.store.close()
        del g

        httpd2, g2, base2 = boot()                 # fresh server: new process state, same store file
        self.assertEqual(g2.sessions, {})
        recs = {t["trade_id"]: t for t in call(base2, "/api/records?state=1")["trades"]}
        self.assertEqual(set(recs), {plain, noted})
        self.assertIsNone(recs[plain]["note"])
        self.assertEqual([r["text"] for r in recs[noted]["note"]["revisions"]],
                         ["fade the pop", "fade the pop; out by 11"])
        for t in recs.values():
            self.assertEqual(t["status"], "closed")
            self.assertIsNone(t["day"])
            self.assertTrue(all(f["source_key"] == "(hidden until reveal)" for f in t["fills"]))
            self.assertTrue(all(f["decision_state_verified"] for f in t["fills"]))
            self.assertAlmostEqual(t["net"], round(t["gross_premium"] - t["fees"], 2), places=2)
        httpd2.shutdown(); httpd2.server_close(); g2.store.close()


if __name__ == "__main__":
    unittest.main()
