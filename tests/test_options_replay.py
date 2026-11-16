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

        def on_fill(sess, fill, state, new_trade, request=None, fault=None):
            return store.record_fill(session_id=sess.id, trade_id=fill["trade_id"], new_trade=new_trade,
                                     attempt=len(sess.attempts) + 1, fill=fill, decision_state=state,
                                     request=request, fault=fault)
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


class S2cRecovery(unittest.TestCase):
    """S2c: pending/acked/failed writes, lost responses, duplicates, undo, stale retries, resume."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "trainer.sqlite3"
        self.servers = []

    def tearDown(self):
        for httpd, g in self.servers:
            httpd.shutdown()
            httpd.server_close()
            g.store.close()

    def boot(self, faults=True):
        import threading
        from http.server import ThreadingHTTPServer
        import server as srv
        from store import TrainerStore
        arc = A.Archive(self.tmp / "cache")
        g = srv.Games(arc, [DAY], 0.65, 1, force_start=OPEN + 3600, store=TrainerStore(self.db))
        g._load = lambda day: minute_pack(drift=0.05)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.make_handler(g, allow_faults=faults))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.servers.append((httpd, g))
        return g, f"http://127.0.0.1:{httpd.server_address[1]}"

    def stop(self, g):
        for i, (h, gg) in enumerate(self.servers):
            if gg is g:
                h.shutdown(); h.server_close(); g.store.close()
                self.servers.pop(i)
                return

    @staticmethod
    def call(base, path, body=None, fault=None):
        import urllib.error
        import urllib.request
        headers = {"Content-Type": "application/json"}
        if fault:
            headers["X-Replay-Fault"] = fault
        req = urllib.request.Request(base + path, None if body is None else json.dumps(body).encode(), headers)
        try:
            return 200, json.load(urllib.request.urlopen(req, timeout=10))
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def start(self, base, g):
        _, v = self.call(base, "/api/new", {})
        g.sessions[v["session"]["id"]].now = FakeClock()
        return v["session"]["id"], v

    def store_counts(self, g):
        db = g.store._db
        return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("trade", "fill", "fill_void", "request", "note_revision")}

    def order(self, base, sid, v, side, contract, rid, fault=None):
        return self.call(base, "/api/order", {"s": sid, "side": side, "contract": contract, "qty": 1,
                                              "request_id": rid, "expected_clock": v["session"]["clock_s"]},
                         fault)

    def test_acknowledged_write_logs_request_with_fill(self):
        g, base = self.boot()
        sid, v = self.start(base, g)
        code, r = self.order(base, sid, v, "buy", "C703", "req-ack-0001")
        self.assertEqual(code, 200)
        self.assertFalse(r["request"]["replayed"])
        ack = r["request"]["ack"]
        prior = g.store.get_request("req-ack-0001")
        self.assertEqual(prior["ack"]["fill_id"], ack["fill_id"])
        self.assertEqual(self.store_counts(g)["fill"], 1)

    def test_failed_write_changes_nothing_and_retry_succeeds_once(self):
        g, base = self.boot()
        sid, v = self.start(base, g)
        code, r = self.order(base, sid, v, "buy", "C703", "req-fail-0001", fault="fail_write")
        self.assertEqual(code, 503)
        self.assertTrue(r["not_saved"])
        self.assertEqual(self.store_counts(g), {"trade": 0, "fill": 0, "fill_void": 0, "request": 0,
                                                "note_revision": 0})
        self.assertEqual(g.sessions[sid].cash, 10_000.0)
        self.assertEqual(g.sessions[sid].fills, [])
        code, r = self.order(base, sid, v, "buy", "C703", "req-fail-0001")
        self.assertEqual(code, 200)
        self.assertEqual(self.store_counts(g)["fill"], 1)

    def test_committed_write_with_lost_response_is_recovered_by_retry(self):
        import http.client
        import urllib.error
        g, base = self.boot()
        sid, v = self.start(base, g)
        with self.assertRaises((http.client.RemoteDisconnected, urllib.error.URLError, ConnectionError)):
            self.order(base, sid, v, "buy", "C703", "req-lost-0001", fault="drop_response")
        self.assertEqual(self.store_counts(g)["fill"], 1)          # it did commit
        code, r = self.order(base, sid, v, "buy", "C703", "req-lost-0001")   # client retries, same id
        self.assertEqual(code, 200)
        self.assertTrue(r["request"]["replayed"])
        self.assertEqual(r["request"]["ack"]["fill_id"], g.store.get_request("req-lost-0001")["ack"]["fill_id"])
        self.assertEqual(self.store_counts(g)["fill"], 1)          # exactly once
        self.assertEqual(len(r["positions"]), 1)
        self.assertEqual(r["positions"][0]["qty"], 1)

    def test_duplicate_submissions_apply_once(self):
        import threading
        g, base = self.boot()
        sid, v = self.start(base, g)
        results = []
        ts = [threading.Thread(target=lambda: results.append(self.order(base, sid, v, "buy", "C703", "req-dup-0001")))
              for _ in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual({c for c, _ in results}, {200})
        self.assertEqual(len({r["request"]["ack"]["fill_id"] for _, r in results}), 1)
        self.assertEqual(sum(1 for _, r in results if not r["request"]["replayed"]), 1)
        self.assertEqual(self.store_counts(g)["fill"], 1)
        code, r = self.order(base, sid, v, "buy", "P704", "req-dup-0001")   # same id, different order
        self.assertEqual(code, 409)
        self.assertIn("different request", r["error"])

    def test_undo_voids_but_keeps_the_fill_and_is_idempotent(self):
        g, base = self.boot()
        sid, v = self.start(base, g)
        _, r = self.order(base, sid, v, "buy", "C703", "req-undo-buy1")
        fid = r["request"]["ack"]["fill_id"]
        self.assertEqual(r["undoable_fill_id"], fid)
        body = {"s": sid, "fill_id": fid, "request_id": "req-undo-0001", "expected_clock": r["session"]["clock_s"]}
        code, u = self.call(base, "/api/undo", body)
        self.assertEqual(code, 200)
        self.assertEqual(u["account"]["cash"], 10_000.0)
        self.assertEqual(u["positions"], [])
        code, u2 = self.call(base, "/api/undo", body)                # retried undo: replayed, not doubled
        self.assertTrue(u2["request"]["replayed"])
        self.assertEqual(u2["request"]["ack"]["void_id"], u["request"]["ack"]["void_id"])
        c = self.store_counts(g)
        self.assertEqual((c["fill"], c["fill_void"]), (1, 1))
        rec = g.store.trades(session_id=sid)[0]
        self.assertEqual(rec["status"], "voided")
        self.assertTrue(rec["fills"][0]["voided"])
        self.assertTrue(rec["fills"][0]["decision_state_verified"])
        import sqlite3
        with self.assertRaises(sqlite3.DatabaseError):
            g.store._db.execute("DELETE FROM fill_void")

    def test_undo_refused_after_the_clock_moves(self):
        g, base = self.boot()
        sid, v = self.start(base, g)
        _, r = self.order(base, sid, v, "buy", "C703", "req-undo-buy2")
        fid = r["request"]["ack"]["fill_id"]
        _, a = self.call(base, "/api/step", {"s": sid, "expected_clock": r["session"]["clock_s"]})
        self.assertIsNone(a["undoable_fill_id"])
        code, u = self.call(base, "/api/undo", {"s": sid, "fill_id": fid, "request_id": "req-undo-0002",
                                                "expected_clock": a["session"]["clock_s"]})
        self.assertEqual(code, 409)
        self.assertEqual(self.store_counts(g)["fill_void"], 0)

    def test_stale_retries_are_refused_with_the_current_state(self):
        g, base = self.boot()
        sid, v = self.start(base, g)
        old_clock = v["session"]["clock_s"]
        _, a = self.call(base, "/api/step", {"s": sid, "expected_clock": old_clock})
        code, r = self.call(base, "/api/step", {"s": sid, "expected_clock": old_clock})   # stale step retry
        self.assertEqual(code, 409)
        self.assertTrue(r["stale"])
        self.assertEqual(r["view"]["session"]["clock_s"], a["session"]["clock_s"])
        code, r = self.order(base, sid, v, "buy", "C703", "req-stale-0001")  # order built on the old clock
        self.assertEqual(code, 409)
        self.assertTrue(r["stale"])
        self.assertEqual(self.store_counts(g)["fill"], 0)

    def test_stale_note_revision_is_refused_not_overwritten(self):
        g, base = self.boot()
        sid, v = self.start(base, g)
        _, r = self.order(base, sid, v, "buy", "C703", "req-note-buy1")
        tid = r["request"]["ack"]["trade_id"]
        c1, n1 = self.call(base, "/api/note", {"trade_id": tid, "text": "one", "base_rev": 0, "request_id": "req-note-0001"})
        c2, n2 = self.call(base, "/api/note", {"trade_id": tid, "text": "two", "base_rev": 1, "request_id": "req-note-0002"})
        c3, n3 = self.call(base, "/api/note", {"trade_id": tid, "text": "one", "base_rev": 0, "request_id": "req-note-0001"})
        c4, n4 = self.call(base, "/api/note", {"trade_id": tid, "text": "stale edit", "base_rev": 1,
                                               "request_id": "req-note-0003"})
        self.assertEqual((c1, n1["rev"], c2, n2["rev"]), (200, 1, 200, 2))
        self.assertEqual((c3, n3["rev"], n3["replayed"]), (200, 1, True))      # retried rev 1: no rev 3
        self.assertEqual((c4, n4["conflict"], n4["current"]["rev"]), (409, True, 2))
        revs = g.store.trades(trade_id=tid)[0]["note"]["revisions"]
        self.assertEqual([x["text"] for x in revs], ["one", "two"])

    def test_unrevealed_session_resumes_after_restart_and_reveals_safely(self):
        g, base = self.boot()
        sid, v = self.start(base, g)
        _, r = self.order(base, sid, v, "buy", "C703", "req-res-buy01")
        _, a = self.call(base, "/api/advance", {"s": sid, "seconds": 600, "max_bars": None,
                                                "expected_clock": r["session"]["clock_s"]})
        _, b = self.order(base, sid, a, "buy", "P704", "req-res-buy02")
        _, u = self.call(base, "/api/undo", {"s": sid, "fill_id": b["request"]["ack"]["fill_id"],
                                             "request_id": "req-res-undo1", "expected_clock": b["session"]["clock_s"]})
        before = {k: u[k] for k in ("positions", "account")}
        before_clock = u["session"]["clock_s"]
        self.stop(g)

        g2, base2 = self.boot()                                  # fresh server, nothing in memory
        self.assertEqual(g2.sessions, {})
        _, lst = self.call(base2, "/api/sessions")
        self.assertEqual([(x["session_id"], x["revealed"], x["day"]) for x in lst["sessions"]], [(sid, False, None)])
        code, w = self.call(base2, "/api/resume", {"s": sid})
        self.assertEqual(code, 200)
        g2.sessions[sid].now = FakeClock()
        self.assertEqual(w["session"]["clock_s"], before_clock)
        self.assertEqual({k: w[k] for k in ("positions", "account")}, before)
        self.assertEqual([f.get("voided", False) for f in w["fills"]], [False, True])
        # save acknowledgments carry the real wall-clock save time; strip those, then no session date
        blob = re.sub(r'"(saved_at|created_at)": "[^"]*"', '', json.dumps(w))
        for pat in DATE_PATTERNS:
            self.assertIsNone(re.search(pat, blob), pat)
        self.assertNotIn("intraday/", blob)
        # a retry of the pre-restart order is recognised, not re-applied
        _, rr = self.order(base2, sid, v, "buy", "C703", "req-res-buy01")   # identical body
        self.assertTrue(rr["request"]["replayed"])
        _, sell = self.order(base2, sid, w, "sell", "C703", "req-res-sell1")
        self.assertEqual(sell["positions"], [])
        _, rv = self.call(base2, "/api/reveal", {"s": sid})
        self.assertEqual(rv["reveal"]["date"], DAY)
        self.assertTrue(rv["reveal"]["reconciliation"]["reconciles"])
        recs = g2.store.trades(session_id=sid)
        self.assertEqual([x["status"] for x in recs], ["closed", "voided"])
        self.assertTrue(all(x["day"] == DAY for x in recs))
        self.assertEqual(self.store_counts(g2)["fill"], 3)

    def test_resume_restores_reset_attempts(self):
        g, base = self.boot()
        sid, v = self.start(base, g)
        _, r = self.order(base, sid, v, "buy", "C703", "req-rst-buy01")
        _, a = self.call(base, "/api/advance", {"s": sid, "seconds": 600, "max_bars": None,
                                                "expected_clock": r["session"]["clock_s"]})
        _, s1 = self.order(base, sid, a, "sell", "C703", "req-rst-sell1")
        first_net = s1["account"]["realized_net"]
        _, z = self.call(base, "/api/reset", {"s": sid, "request_id": "req-rst-0001",
                                              "expected_clock": s1["session"]["clock_s"]})
        _, z2 = self.call(base, "/api/reset", {"s": sid, "request_id": "req-rst-0001",
                                               "expected_clock": s1["session"]["clock_s"]})
        self.assertTrue(z2["request"]["replayed"])
        self.assertEqual(z2["session"]["attempt"], 2)            # duplicate reset did not add attempt 3
        self.stop(g)
        g2, base2 = self.boot()
        code, w = self.call(base2, "/api/resume", {"s": sid})
        self.assertEqual(w["session"]["attempt"], 2)
        self.assertEqual(w["session"]["clock_s"], OPEN + 3600)
        self.assertEqual(w["attempts"][0]["realized_net"], first_net)
        self.assertEqual(w["fills"], [])


class SimulationMode(unittest.TestCase):
    """S3a: a generated day is tradable, labelled as simulation, reproducible by seed, recoverable."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.servers = []

    def tearDown(self):
        for h, g in self.servers:
            h.shutdown(); h.server_close(); g.store.close()

    class FakeSim:
        """Deterministic stand-in for simulate.Simulator (no R2): seed -> drift."""
        digest = "f" * 64

        def __init__(self):
            self.calls = 0

        def generate(self, seed, version=None):
            self.calls += 1
            p = minute_pack(drift=0.01 * (seed % 7 + 1))
            for sn in p["snapshots"]:
                sn["key"] = f"sim/sim-v1/{seed}/{sn['t'] // 3600:02d}{sn['t'] % 3600 // 60:02d}"
            p.update({"mode": "simulation", "day": f"SIM:sim-v1:{seed}:{self.digest[:12]}",
                      "sim": {"seed": seed, "generator_version": "sim-v1", "inputs_digest": self.digest,
                              "pack_sha256": "a" * 64, "donor_day": "2025-10-09", "mechanism": "test",
                              "limitations": ["provisional"], "calibration": {"historical_sessions": 1,
                              "donor_days": 1, "r2_objects": 1}, "parity_residual_usd": {"median": 0, "p95": 0}}})
            return p

        def raw_quote(self, seed, key, c, version=None):
            for sn in self.generate(seed)["snapshots"]:
                if sn["key"] == key:
                    b, a = sn["q"][c]
                    return {"key": key, "OptionSymbol": "SIM " + c, "Bid": b, "Ask": a, "UnderlyingPrice": sn["spot"]}

    def boot(self):
        import threading
        from http.server import ThreadingHTTPServer
        import server as srv
        from store import TrainerStore
        g = srv.Games(A.Archive(self.tmp / "cache"), [DAY], 0.65, 1, force_start=OPEN + 3600,
                      store=TrainerStore(self.tmp / "t.sqlite3"))
        g.sim = self.FakeSim()
        g._load = lambda day: minute_pack(drift=0.05)
        h = ThreadingHTTPServer(("127.0.0.1", 0), srv.make_handler(g))
        threading.Thread(target=h.serve_forever, daemon=True).start()
        self.servers.append((h, g))
        return g, f"http://127.0.0.1:{h.server_address[1]}"

    def test_simulated_trade_is_labelled_reproducible_and_recoverable(self):
        call = S2cRecovery.call
        g, base = self.boot()
        _, v = call(base, "/api/new", {"mode": "simulation", "seed": 42})
        sid = v["session"]["id"]
        g.sessions[sid].now = FakeClock()
        self.assertEqual(v["session"]["mode"], "simulation")
        self.assertEqual(v["session"]["simulation"]["seed"], 42)
        self.assertEqual(v["session"]["simulation"]["pricing"], "provisional")
        _, b = call(base, "/api/order", {"s": sid, "side": "buy", "contract": "C700", "qty": 1,
                                         "request_id": "sim-buy-0001", "expected_clock": v["session"]["clock_s"]})
        _, a = call(base, "/api/advance", {"s": sid, "seconds": 600, "max_bars": None,
                                           "expected_clock": b["session"]["clock_s"]})
        _, sv = call(base, "/api/order", {"s": sid, "side": "sell", "contract": "C700", "qty": 1,
                                          "request_id": "sim-sell-001", "expected_clock": a["session"]["clock_s"]})
        rec = g.store.trades(session_id=sid)[0]
        self.assertEqual((rec["mode"], rec["simulation"]["seed"], rec["status"]), ("simulation", 42, "closed"))
        h, gg = self.servers.pop(); h.shutdown(); h.server_close(); gg.store.close()
        g2, base2 = self.boot()                       # fresh server: resume regenerates from the seed
        _, w = call(base2, "/api/resume", {"s": sid})
        self.assertEqual(w["account"], sv["account"])
        self.assertEqual(w["session"]["simulation"]["seed"], 42)
        _, rv = call(base2, "/api/reveal", {"s": sid})
        r = rv["reveal"]
        self.assertEqual((r["mode"], r["date"], r["weekday"]), ("simulation", None, None))
        self.assertEqual(r["simulation"]["seed"], 42)
        rc = r["reconciliation"]
        self.assertTrue(rc["reconciles"])
        self.assertTrue(all(l["raw_matches"] for l in rc["lines"]))   # regenerated quotes match the fills

    def test_resume_refuses_changed_simulation_inputs(self):
        call = S2cRecovery.call
        g, base = self.boot()
        _, v = call(base, "/api/new", {"mode": "simulation", "seed": 7})
        sid = v["session"]["id"]
        h, gg = self.servers.pop(); h.shutdown(); h.server_close(); gg.store.close()
        g2, base2 = self.boot()
        g2.sim.digest = "0" * 64
        code, r = call(base2, "/api/resume", {"s": sid})
        self.assertEqual(code, 409)
        self.assertIn("inputs changed", r["error"])

    def test_replay_mode_unchanged_and_bad_mode_refused(self):
        call = S2cRecovery.call
        g, base = self.boot()
        _, v = call(base, "/api/new", {})
        self.assertEqual(v["session"]["mode"], "replay")
        self.assertIsNone(v["session"]["simulation"])
        code, r = call(base, "/api/new", {"mode": "fantasy"})
        self.assertEqual(code, 409)


class CalibrationMath(unittest.TestCase):
    """S3b evaluator primitives: pricing, inversion, variance clock, split discipline."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "research" / "options_replay" / "calibration"))
        import evaluate as E
        self.E = E

    def test_put_call_parity_and_inversion(self):
        E = self.E
        for S, K, sig, tau in ((700, 698, 0.2, 0.002), (700, 703, 0.35, 0.0005), (500, 500, 0.15, 1 / 252)):
            c, p = E.bs(S, K, sig, tau, "C"), E.bs(S, K, sig, tau, "P")
            self.assertAlmostEqual(c - p, S - K, places=6)
            self.assertAlmostEqual(E.implied_sigma(c, S, K, tau, "C"), sig, places=4)
        self.assertEqual(E.bs(700, 690, 0.2, 0.0, "C"), 10.0)

    def test_clock_monotonic_and_tail(self):
        E = self.E
        clk = E.Clock([3.0] * 6 + [1.0] * 66 + [2.0] * 6)
        taus = [clk.tau(t) for t in range(E.OPEN, E.CLOSE + 1, 60)]
        self.assertTrue(all(a >= b for a, b in zip(taus, taus[1:])))
        self.assertAlmostEqual(clk.tau(E.OPEN) * E.YEAR_DAYS, 1.0, places=9)
        self.assertEqual(clk.tau(E.CLOSE), 0.0)
        tail = clk.with_tail(0.05)
        self.assertAlmostEqual(tail.tau(E.CLOSE) * E.YEAR_DAYS, 0.05, places=9)
        self.assertAlmostEqual(E.UNIFORM.covered(E.OPEN, E.OPEN + 3900), 13 / 78, places=9)

    def test_split_never_puts_exploratory_days_in_held_out(self):
        E = self.E
        days = ["2026-09-30", "2026-10-02", "2026-10-22", "2026-11-02", "2026-11-04"]
        dev, held, expl = E.split("QQQ", days)
        self.assertEqual(dev, ["2026-09-30"])
        self.assertEqual(held, ["2026-10-02", "2026-11-04"])
        self.assertEqual(expl, ["2026-10-22", "2026-11-02"])

    def test_protocol_tolerances_are_the_declared_ones(self):
        tol = self.E.TOL
        self.assertEqual((tol["A1_median_abs_level_usd"], tol["A2_abs_median_bias_usd"], tol["A3_median_abs_hold_pnl_usd"],
                          tol["A4_median_abs_quiet_change_usd"], tol["B1_median_ratio_band"], tol["B2_median_abs_hold_pnl_usd"]),
                         (8.0, 4.0, 4.0, 3.0, [0.85, 1.15], 6.0))


class HeldOutCoverage(unittest.TestCase):
    """coverage_s3c counts reserved sessions from listings only, never bodies."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "research" / "options_replay" / "calibration"))
        import coverage_s3c as C
        self.C = C

    def test_classify(self):
        C, mon = self.C, date(2026, 11, 16)
        full = list(range(OPEN - 60, CLOSE, 65))
        self.assertEqual(C.classify(date(2026, 11, 21), full, mon, 0)[0], "not_session")
        self.assertEqual(C.classify(mon, [], date(2026, 11, 17), 0)[0], "missing")
        self.assertEqual(C.classify(mon, full, mon, 12 * 3600)[0], "pending")
        self.assertEqual(C.classify(mon, [], mon, 3600)[0], "pending")
        self.assertEqual(C.classify(mon, full, date(2026, 11, 17), 0)[0], "eligible")
        holed = [t for t in full if not (12 * 3600 <= t < 12 * 3600 + 1200)]
        self.assertEqual(C.classify(mon, holed, date(2026, 11, 17), 0)[0], "excluded")

    def test_counts_listings_without_downloading(self):
        C = self.C
        day = "20261116"
        keys = [f"intraday/{day}/snapshot_{t // 3600:02d}{t % 3600 // 60:02d}{t % 60:02d}000000.csv"
                for t in range(OPEN - 60, CLOSE, 65)]

        class Arc:
            def list_keys(self, prefix, refresh=False):
                return keys if prefix == f"intraday/{day}/" else []

            def get(self, key):
                raise AssertionError("coverage must not download snapshot bodies")
            get_many = get

        r = C.count(Arc(), date(2026, 11, 17), 0)
        self.assertEqual(r["symbols"]["QQQ"]["eligible"], 1)
        self.assertEqual(r["symbols"]["SPY"]["eligible"], 0)
        self.assertFalse(r["ready"])
        self.assertEqual(C.HELD_FROM.isoformat(), "2026-11-16")
        self.assertEqual(C.MIN_SESSIONS, 10)


class HeldOutSelection(unittest.TestCase):
    """S3c evaluator: reserved forward files are chosen by name before decoding; exactly the first ten."""

    def setUp(self):
        import tempfile
        sys.path.insert(0, str(ROOT / "research" / "options_replay" / "calibration"))
        import evaluate_s3c as V
        self.V = V
        self.tmp = Path(tempfile.mkdtemp())
        d = self.tmp / "calibration_s3c"
        d.mkdir()
        self.dev = ["2026-11-10", "2026-11-12", "2026-11-13"]
        # 13 synthetic forward sessions, written out of order; only the first ten are valid JSON,
        # so decoding any later one (or any forward file in development mode) would raise.
        self.fwd = [f"2026-{m:02d}-{x:02d}" for m, x in
                    [(11, 16), (11, 17), (11, 18), (11, 19), (11, 20), (11, 23), (11, 24), (11, 25), (11, 27),
                     (11, 30), (12, 1), (12, 2), (12, 3)]]
        for day in reversed(self.dev + self.fwd):
            name = d / f"QQQ_{day.replace('-', '')}.json"
            if day in self.fwd[10:]:
                name.write_text("not json: must never be decoded")
            else:
                name.write_text(json.dumps({"day": day, "points": []}))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_development_never_decodes_forward_files(self):
        V = self.V
        for f in (self.tmp / "calibration_s3c").glob("QQQ_202611[12]*.json"):
            if f.stem[4:] >= "20261116":
                f.write_text("not json")
        days = V.load(self.tmp, "QQQ")
        self.assertEqual(sorted(days), self.dev)
        self.assertEqual(V.split(sorted(days)), (self.dev, []))

    def test_exactly_first_ten_in_order_with_more_available(self):
        V = self.V
        self.assertEqual(V.MIN_SESSIONS, 10)
        self.assertEqual(V.held_out_selection(self.dev + self.fwd), self.fwd[:10])
        days = V.load(self.tmp, "QQQ", "held_out")      # files 11-13 are invalid JSON: never opened
        self.assertEqual(sorted(days), self.dev + self.fwd[:10])
        dev, held = V.split(sorted(days))
        self.assertEqual((dev, held), (self.dev, self.fwd[:10]))

    def test_fewer_than_ten_is_empty_and_refused_without_logging(self):
        import subprocess
        V = self.V
        self.assertEqual(V.held_out_selection(self.dev + self.fwd[:9]), [])
        for f in (self.tmp / "calibration_s3c").glob("QQQ_*.json"):
            if f.stem[4:] >= "20261125":
                f.unlink()                               # 7 forward QQQ sessions remain; SPY has none
        log = ROOT / "research" / "options_replay" / "calibration" / "s3c_attempts.jsonl"
        before = log.read_bytes()
        r = subprocess.run([sys.executable, str(ROOT / "research" / "options_replay" / "calibration" / "evaluate_s3c.py"),
                            "--cache", str(self.tmp), "--set", "held_out", "--label", "fixture"],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not evaluable, nothing run or logged", r.stderr)
        self.assertEqual(log.read_bytes(), before)


class SimulationInputPin(unittest.TestCase):
    """Fresh-cache simulation inputs come only from the pinned inventory, or the build stops."""

    def setUp(self):
        import hashlib
        import tempfile
        import simulate as M
        self.M, self.h = M, (lambda b: hashlib.sha256(b).hexdigest())
        self.tmp = Path(tempfile.mkdtemp())
        self.r2 = self.tmp / "sim_inputs" / "r2"
        bars = {"t": [100, 400, 700], "o": [1, 2, 3], "h": [1, 2, 3], "l": [1, 2, 3], "c": [1, 2, 3], "v": [1, 1, 1]}
        files = {"history/QQQ/5/2026.json": json.dumps(bars).encode(),
                 "history/QQQ/options_0dte/2026-01-05.json": b'{"strike": [1]}',
                 "intraday/20260105/snapshot_100000000000.csv": b"a,b\n1,2\n"}
        for k, b in files.items():
            (self.r2 / k).parent.mkdir(parents=True, exist_ok=True)
            (self.r2 / k).write_bytes(b)
        # Synthetic "later archive": a reserved forward day the pin does not contain.
        (self.r2 / "intraday/20261116").mkdir(parents=True)
        (self.r2 / "intraday/20261116/snapshot_100000000000.csv").write_bytes(b"later")
        self.pin = {"manifest": {"r2_objects": {k: self.h(b) for k, b in files.items()},
                                 "inputs_digest": "d" * 64, "frozen_sha256": None},
                    "bars": {"2026": {"key": "history/QQQ/5/2026.json", "n": 3, "first_t": 100, "last_t": 700}},
                    "donor_days": ["2026-01-05"],
                    "intraday_keys": {"20260105": ["intraday/20260105/snapshot_100000000000.csv"]}}

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def src(self):
        return self.M.pinned_source(self.pin, self.r2)

    def test_only_pinned_days_and_keys_are_visible(self):
        M, src = self.M, self.src()
        self.assertEqual(src.intraday_days_available(), ["20260105"])
        self.assertEqual(src.intraday_snapshots("20261116"), [])
        self.assertEqual([d.isoformat() for d in src.all_options_0dte_days("QQQ")], ["2026-01-05"])
        with self.assertRaises(M.PinError):
            src.intraday_snapshot_csv("intraday/20261116/snapshot_100000000000.csv")
        with self.assertRaises(M.PinError):
            src._list("intraday/")

    def test_grown_bar_file_is_cut_at_the_pinned_last_bar(self):
        f = self.r2 / "history/QQQ/5/2026.json"
        b = json.loads(f.read_text())
        for k in b:
            b[k] += [9, 9] if k != "t" else [1000, 1300]   # bars after the freeze
        f.write_text(json.dumps(b))
        self.assertEqual(self.src().five_min_bars("QQQ", [2026])[2026]["t"], [100, 400, 700])
        b["c"][0] = 42                                     # pre-freeze history no longer matches
        b["t"] = [100, 700, 1000, 1300, 1600]
        f.write_text(json.dumps(b))
        with self.assertRaises(self.M.PinError):
            self.src().five_min_bars("QQQ", [2026])

    def test_changed_object_stops_and_is_not_kept(self):
        k = "history/QQQ/options_0dte/2026-01-05.json"
        (self.r2 / k).write_bytes(b'{"strike": [2]}')
        with self.assertRaises(self.M.PinError):
            self.src().options_0dte_day("QQQ", date(2026, 1, 5))
        self.assertFalse((self.r2 / k).exists())

    def test_rebuild_must_reproduce_frozen_bytes_or_write_nothing(self):
        import pickle
        M, cache = self.M, self.tmp / "sim_inputs"

        def builder(src):
            return (src.five_min_bars("QQQ", [2026]), src.intraday_days_available())
        good = pickle.dumps(builder(self.src()), protocol=4)
        self.pin["manifest"]["frozen_sha256"] = "0" * 64
        with self.assertRaises(M.PinError):
            M.rebuild_from_pin(cache, self.pin, builder=builder)
        self.assertFalse((cache / "frozen_inputs.pkl").exists())
        self.pin["manifest"]["frozen_sha256"] = self.h(good)
        inputs, man = M.rebuild_from_pin(cache, self.pin, builder=builder)
        self.assertEqual((cache / "frozen_inputs.pkl").read_bytes(), good)
        self.assertEqual(json.loads((cache / "inputs_manifest.json").read_text())["inputs_digest"], "d" * 64)

    def test_committed_pin_matches_the_frozen_identity(self):
        pin = self.M.load_pin()
        self.assertEqual(pin["manifest"]["inputs_digest"][:16], "13e18c6aea01fdb9")
        self.assertEqual(len(pin["manifest"]["r2_objects"]), 535)
        self.assertEqual(len(pin["donor_days"]), 80)
        self.assertTrue(all(d < "20261116" for d in pin["intraday_keys"]))


if __name__ == "__main__":
    unittest.main()
