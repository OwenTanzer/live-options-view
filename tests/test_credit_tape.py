"""OA-242 credit tape: synthetic fixtures only (repository is public)."""
import csv
import hashlib
import gzip
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from market_data.credit_tape import capture
from market_data.credit_tape import serve_private as sp
from market_data.credit_tape import slices


def fixture_row(i, action="NEWT", notional="5,000,000+", diss="2099-01-01T00:00:00Z"):
    return {"disseminationIdentifier": f"FIXTURE{i}", "originalDisseminationIdentifier": None,
            "actionType": action, "executionTimestamp": "2099-01-01T00:00:00Z",
            "eventTimestamp": "2099-01-01T00:00:00Z", "disseminationTimestamp": diss,
            "notionalAmountLeg1": notional, "notionalCurrencyLeg1": "USD",
            "underlierIDLeg1": "FIXTURE-ISSUER", "someUnfamiliarField": "ZZZ"}


class FakeResp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def run_poll(src, body, out):
    with mock.patch.object(urllib.request, "urlopen", return_value=FakeResp(body)):
        src.poll(os.path.join(out, "alerts.jsonl"), os.path.join(out, "events.jsonl"))


def lines(path):
    return [json.loads(l) for l in open(path, encoding="utf-8")] if os.path.exists(path) else []


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = self.tmp.name
        self.src = capture.Source("dtcc-sec-credits", capture.SOURCES["dtcc-sec-credits"], self.out)

    def tearDown(self):
        self.tmp.cleanup()

    def test_raw_saved_before_decode_and_new_rows_alert_once(self):
        run_poll(self.src, json.dumps({"tradeList": [fixture_row(1)]}).encode(), self.out)
        self.assertEqual(lines(os.path.join(self.out, "alerts.jsonl")), [])  # warm-up does not alert
        body2 = json.dumps({"tradeList": [fixture_row(2, "CORR"), fixture_row(1)]}).encode()
        run_poll(self.src, body2, self.out)
        run_poll(self.src, body2, self.out)  # identical payload: no new raw file, no duplicate alert
        alerts = lines(os.path.join(self.out, "alerts.jsonl"))
        self.assertEqual([a["id"] for a in alerts], ["FIXTURE2"])
        ev = [e for e in lines(os.path.join(self.out, "events.jsonl")) if e["id"] == "FIXTURE2"][0]
        self.assertEqual(ev["record"]["someUnfamiliarField"], "ZZZ")
        self.assertEqual(ev["record"]["notionalAmountLeg1"], "5,000,000+")
        self.assertEqual(sp.read_raw(Path(self.out), ev["raw_ref"]), body2)
        self.assertEqual(len(list(Path(self.out).rglob("*.json.gz"))), 2)

    def test_decode_failure_still_produces_raw_event(self):
        run_poll(self.src, b"<html>not json", self.out)
        ev = lines(os.path.join(self.out, "events.jsonl"))[0]
        self.assertEqual(ev["kind"], "decode_failure")
        self.assertEqual(sp.read_raw(Path(self.out), ev["raw_ref"]), b"<html>not json")

    def test_old_publication_is_labelled_backfill(self):
        run_poll(self.src, json.dumps({"tradeList": [fixture_row(1)]}).encode(), self.out)
        run_poll(self.src, json.dumps({"tradeList": [fixture_row(9, diss="2000-01-01T00:00:00Z"), fixture_row(1)]}).encode(), self.out)
        ev = [e for e in lines(os.path.join(self.out, "events.jsonl")) if e["id"] == "FIXTURE9"][0]
        self.assertTrue(ev["backfill"])


def fixture_zip(rows):
    buf = io.StringIO()
    w = __import__("csv").DictWriter(buf, fieldnames=["Dissemination Identifier", "Action type", "Notional amount-Leg 1",
                                                      "Odd new column"])
    w.writeheader()
    for r in rows:
        w.writerow(r)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("FIXTURE.csv", buf.getvalue())
    return out.getvalue()


class FakeHTTP:
    """Serves a synthetic index/slices; records requests; can fail or 304."""

    def __init__(self, index, files, etag='"e1"'):
        self.index, self.files, self.etag, self.requests, self.fail = index, files, etag, [], set()

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.requests.append((url, dict(req.header_items())))
        if url in self.fail:
            raise urllib.error.URLError("synthetic outage")
        if url == slices.INDEX_URL:
            if req.get_header("If-none-match") == self.etag:
                raise urllib.error.HTTPError(url, 304, "Not Modified", {}, None)
            r = FakeResp(json.dumps(self.index).encode())
            r.headers = {"ETag": self.etag, "Last-Modified": "Fri, 01 Jan 2099 00:00:00 GMT"}
            return r
        r = FakeResp(self.files[url])
        r.headers = {}
        return r


def slice_meta(n, rows, dissem="2000-01-01T00:00:0%dZ", agency="cftc"):
    name = f"{agency.upper()}_SLICE_CREDITS_2099_01_01_{n}.zip"
    return {"sliceId": 1000 + n, "fileName": name, "rowCount": rows, "dissemDTM": dissem % n,
            "startTs": "2099-01-01T00:00:00Z", "endTs": "2099-01-01T00:00:00Z",
            "fullFilePath": f"https://{slices.SLICE_HOST}/{agency}/slices/{name}"}


class SliceRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = self.tmp.name
        self.ev, self.al = os.path.join(self.out, "events.jsonl"), os.path.join(self.out, "alerts.jsonl")

    def route(self, http, clock=None):
        return slices.SliceRoute(self.out, capture.now, capture.append_jsonl, opener=http,
                                 clock=clock or time.time, lock=capture.WRITE_LOCK)

    def drain(self, r):
        r.drain_live(self.ev, self.al)
        r.drain_backfill(self.ev, self.al, budget=10**6)

    def test_unseen_slices_persisted_before_decode_rowcount_checked_once(self):
        m1, m2 = slice_meta(1, 1), slice_meta(2, 3)  # m2 publishes 3 but carries 2 -> mismatch
        z1 = fixture_zip([{"Dissemination Identifier": "S1", "Action type": "NEWT", "Notional amount-Leg 1": "5,000,000+", "Odd new column": "Q"}])
        z2 = fixture_zip([{"Dissemination Identifier": "S2", "Action type": "CORR"}, {"Dissemination Identifier": "S3", "Action type": "TERM"}])
        http = FakeHTTP({"CFTC_CR": [m2, m1], "SEC_CR": []}, {m1["fullFilePath"]: z1, m2["fullFilePath"]: z2})
        r = self.route(http)
        r.poll_index(self.ev)
        self.assertEqual(sorted(r.pending), ["CFTC_CR:1001", "CFTC_CR:1002"])  # index poll only enqueues
        self.assertFalse(any(u.endswith(".zip") for u, _ in http.requests))
        self.drain(r)
        r.poll_index(self.ev)  # conditional request -> 304, nothing re-enqueued
        self.drain(r)
        self.assertEqual(http.requests[-1][1].get("If-none-match"), '"e1"')
        evs = lines(self.ev)
        pubs = [e for e in evs if e["kind"] == "publication"]
        self.assertEqual([e["id"] for e in pubs], ["S1", "S2", "S3"])  # sliceId order
        self.assertEqual(pubs[0]["record"]["Odd new column"], "Q")
        self.assertEqual(pubs[0]["record"]["Notional amount-Leg 1"], "5,000,000+")
        self.assertEqual(sp.read_raw(Path(self.out), pubs[0]["raw_ref"]), z1)
        mism = [e for e in evs if e["kind"] == "rowcount_mismatch"]
        self.assertEqual([(e["slice"]["fileName"], e["decoded_rows"]) for e in mism], [(m2["fileName"], 2)])
        self.assertTrue(all(e["replay"] for e in pubs))  # published before collector start
        self.assertEqual([a["kind"] for a in lines(self.al)], ["rowcount_mismatch"])  # replays don't alert rows
        self.assertEqual(sum(1 for u, _ in http.requests if u.endswith(".zip")), 2)

    def test_foreign_host_rejected_and_failed_slice_does_not_block_others(self):
        bad = dict(slice_meta(1, 1), fullFilePath="https://evil.example/cftc/slices/x.zip")
        m2, m3 = slice_meta(2, 1, "2999-01-01T00:00:0%dZ"), slice_meta(3, 1, "2999-01-01T00:00:0%dZ")
        z = fixture_zip([{"Dissemination Identifier": "L", "Action type": "NEWT"}])
        http = FakeHTTP({"CFTC_CR": [bad, m2, m3], "SEC_CR": []}, {m2["fullFilePath"]: z, m3["fullFilePath"]: z})
        http.fail.add(m2["fullFilePath"])
        clock = {"t": 0.0}
        r = self.route(http, clock=lambda: clock["t"])
        r.poll_index(self.ev)
        self.drain(r)
        kinds = [(e["kind"], e["slice"]["sliceId"]) for e in lines(self.ev) if e["kind"] in ("slice_rejected", "publication")]
        self.assertEqual(sorted(kinds), [("publication", 1003), ("slice_rejected", 1001)])  # 1002 failed, 1003 not stranded
        self.assertEqual(r.pending["CFTC_CR:1002"]["attempts"], 1)
        http.fail.clear()
        r.drain_live(self.ev, self.al)  # not yet due
        self.assertIn("CFTC_CR:1002", r.pending)
        clock["t"] += slices.RETRY_MAX
        r.poll_index(self.ev)  # index unchanged -> 304
        r.drain_live(self.ev, self.al)
        live = [e for e in lines(self.ev) if e["kind"] == "publication"]
        self.assertEqual(sorted(e["slice"]["sliceId"] for e in live), [1002, 1003])
        self.assertEqual(next(e for e in live if e["slice"]["sliceId"] == 1002)["attempts"], 2)
        self.assertFalse(any(e["replay"] for e in live))
        self.assertEqual(len([a for a in lines(self.al) if a["kind"] == "publication"]), 2)
        self.assertFalse(any("evil" in u for u, _ in http.requests))

    def test_done_marker_prevents_reemit_after_crash(self):
        m = slice_meta(1, 1, "2999-01-01T00:00:0%dZ")
        z = fixture_zip([{"Dissemination Identifier": "C", "Action type": "NEWT"}])
        http = FakeHTTP({"CFTC_CR": [m], "SEC_CR": []}, {m["fullFilePath"]: z})
        r = self.route(http)
        r.poll_index(self.ev)
        r.drain_live(self.ev, self.al)
        # Simulate a crash after the done marker but before pending.json was rewritten.
        with open(r.pending_path, "w", encoding="utf-8") as fh:
            json.dump({str(m["sliceId"]): {"key": "CFTC_CR", "meta": m, "attempts": 0, "retry_at": 0}}, fh)
        r2 = self.route(http)
        self.assertEqual(r2.pending, {})
        r2.drain_live(self.ev, self.al)
        self.assertEqual(sum(1 for e in lines(self.ev) if e["kind"] == "publication"), 1)

    def test_same_slice_id_on_both_channels_gives_two_publications(self):
        mc, ms = slice_meta(1, 1, "2999-01-01T00:00:0%dZ"), slice_meta(1, 1, "2999-01-01T00:00:0%dZ", agency="sec")
        self.assertEqual(mc["sliceId"], ms["sliceId"])
        zc = fixture_zip([{"Dissemination Identifier": "C1", "Action type": "NEWT"}])
        zs = fixture_zip([{"Dissemination Identifier": "S1", "Action type": "NEWT"}])
        http = FakeHTTP({"CFTC_CR": [mc], "SEC_CR": [ms]}, {mc["fullFilePath"]: zc, ms["fullFilePath"]: zs})
        r = self.route(http)
        r.poll_index(self.ev)
        self.assertEqual(sorted(r.pending), ["CFTC_CR:1001", "SEC_CR:1001"])
        r.drain_live(self.ev, self.al)
        r2 = self.route(http)  # restart on an unchanged index
        r2.poll_index(self.ev)
        r2.drain_live(self.ev, self.al)
        pubs = [e for e in lines(self.ev) if e["kind"] == "publication"]
        self.assertEqual(sorted((e["source"], e["id"]) for e in pubs),
                         [("dtcc-cftc-slices", "C1"), ("dtcc-sec-slices", "S1")])
        by_src = {e["source"]: e["raw_ref"] for e in pubs}
        self.assertEqual(sp.read_raw(Path(self.out), by_src["dtcc-cftc-slices"]), zc)
        self.assertEqual(sp.read_raw(Path(self.out), by_src["dtcc-sec-slices"]), zs)
        self.assertEqual(len([a for a in lines(self.al) if a["kind"] == "publication"]), 2)

    def test_synthetic_slice_only_demo(self):
        from market_data.credit_tape import synthetic_demo
        result = synthetic_demo.run(self.out)
        self.assertTrue(result["all_pass"], json.dumps(result["checks"], indent=1))

    def test_synthetic_crash_safe_publish(self):
        from market_data.credit_tape import synthetic_crash
        result = synthetic_crash.run()
        self.assertTrue(result["all_pass"], json.dumps(result, indent=1))
        self.assertEqual([r["stop_at"] for r in result["results"]], synthetic_crash.STOP_POINTS)

    def test_synthetic_resilience_run(self):
        from market_data.credit_tape import synthetic_resilience
        result = synthetic_resilience.run(self.out)
        self.assertEqual(result["verdicts"], {k: True for k in result["verdicts"]}, json.dumps(result, indent=1))


class ServerTests(unittest.TestCase):
    def serve(self, settings, identity=None):
        handler = sp.make_handler(settings, identity=identity or (lambda s, c, v: 0))
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return srv.server_port

    def get(self, port, path, host=None, cookie=None):
        c = HTTPConnection("127.0.0.1", port, timeout=5)
        h = {"Host": host or f"127.0.0.1:{port}"}
        if cookie:
            h["Cookie"] = cookie
        c.request("GET", path, headers=h)
        r = c.getresponse()
        return r.status, r.read(), r

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = Path(self.tmp.name)
        raw = self.archive / "dtcc-sec-credits/raw/2099-01-01"
        raw.mkdir(parents=True)
        with gzip.open(raw / "x_abc.json.gz", "wb") as fh:
            fh.write(b'{"tradeList":[]}')
        (self.archive / "events.jsonl").write_text(json.dumps({
            "kind": "publication", "id": "FIXTURE1", "source": "dtcc-sec-credits", "jurisdiction": "US-SEC",
            "first_seen": "2099-01-01T00:00:01.000+00:00", "raw_ref": "dtcc-sec-credits/raw/2099-01-01/x_abc.json.gz",
            "row_index": 0, "record": fixture_row(1)}) + "\n")

    def test_replay_rejects_an_oversized_or_unterminated_archive_boundary(self):
        for payload in (b"a" * 32, b"a" * 31 + b"\n", b"a" * 33 + b"\n"):
            (self.archive / "events.jsonl").write_bytes(payload)
            with mock.patch.object(sp.replay, "BYTE_LIMIT", 32), self.assertRaises(ValueError):
                sp.replay.read_page(self.archive, 4070908801000, sp.panel_event, 5)

    def test_main_site_table_uses_owner_session_and_static_rows(self):
        from urllib.parse import parse_qs, urlsplit
        settings = sp.Settings(self.archive, "https://cds.example.test", "cid", "secret", frozenset({42}), options_view=True)
        port = self.serve(settings, identity=lambda *_: 42)
        host = "cds.example.test"
        page = "/credit-tape/"
        status, signed_out, _ = self.get(port, page, host)
        self.assertEqual(status, 200)
        self.assertNotIn(b"FIXTURE1", signed_out)
        self.assertIn(b"/credit-tape/auth/login", signed_out)
        _, _, login = self.get(port, page + "auth/login", host)
        params = parse_qs(urlsplit(login.getheader("Location")).query)
        self.assertEqual(params["redirect_uri"], ["https://options.moopertonic.net/credit-tape/auth/callback"])
        flow = login.getheader("Set-Cookie").split(";")[0]
        status, _, callback = self.get(port, page + "auth/callback?code=fake&state=" + params["state"][0], host, flow)
        self.assertEqual((status, callback.getheader("Location")), (303, page))
        session = next(c.split(";")[0] for c in callback.headers.get_all("Set-Cookie") if c.startswith(sp.SESSION_COOKIE+"="))
        subject = "/api/private/identity"
        self.assertEqual(self.get(port, subject, host)[0], 401)
        self.assertEqual(self.get(port, subject, host, sp.SESSION_COOKIE + "=forged")[0], 401)
        self.assertEqual(self.get(port, subject + "?github_id=99", host, session)[0], 404)
        self.assertEqual(self.get(port, page + "api/private/identity", host, session)[0], 404)
        status, principal, headers = self.get(port, subject, host, session)
        self.assertEqual((status, json.loads(principal)), (200, {"github_id": 42}))
        self.assertIn("no-store", headers.getheader("Cache-Control"))
        status, body, response = self.get(port, page, host, session)
        self.assertEqual(status, 200)
        self.assertIn(b"<tbody><tr", body)
        self.assertIn(b"FIXTURE1", body)

        sample_url = page + "?view=treu-sample"
        self.assertEqual(self.get(port, sample_url, host, session)[0], 404)
        sample_dir = self.archive / "samples" / "treu"
        sample_dir.mkdir(parents=True)
        sample_event = {"kind": "publication", "id": "TREU-ONLY", "publisher": "Tradeweb",
                        "channel": "TREU", "source": "tradeweb-uk-mtf-treu", "jurisdiction": "UK",
                        "qualification": "Unclassified sample"}
        (sample_dir / "events.jsonl").write_text(json.dumps(sample_event) + "\n")
        self.assertNotIn(b"TREU-ONLY", self.get(port, sample_url, host)[1])
        live_page = self.get(port, page, host, session)[1]
        self.assertIn(b"?view=treu-sample", live_page)
        self.assertNotIn(b"TREU-ONLY", live_page)
        sample_page = self.get(port, sample_url, host, session)[1]
        self.assertIn(b"TREU-ONLY", sample_page)
        self.assertNotIn(b"FIXTURE1", sample_page)

        import sqlite3
        import zlib
        from market_data.credit_tape import treu
        db = sqlite3.connect(self.archive / treu.DATABASE)
        try:
            treu.initialize(db)
            report = dict(sample_event, id="TREU-LIVE", first_seen="2099-01-01T00:00:03Z",
                          record={"Instrument identification code": "ID-UK", "Price": "99.7"})
            db.execute("INSERT INTO reports(event) VALUES(?)", (zlib.compress(json.dumps(report).encode()),))
            db.commit()
        finally:
            db.close()
        unified = self.get(port, page, host, session)[1]
        self.assertIn(b"FIXTURE1", unified)
        self.assertLess(unified.index(b"TREU-LIVE"), unified.index(b"FIXTURE1"))
        self.assertIn(b"<dt>Price</dt><dd>99.7</dd>", unified)
        self.assertNotIn(b"TREU-ONLY", unified)
        cutoff = 4070908801000  # 2099-01-01T00:00:01.000Z
        replay = page + "replay?one_clock_at="
        gated = self.get(port, replay + str(cutoff), host)[1]
        self.assertIn(b"Sign in with GitHub", gated)
        self.assertIn(f"/credit-tape/auth/login?one_clock_at={cutoff}".encode(), gated)
        self.assertNotIn(b"FIXTURE1", gated)
        before = self.get(port, replay + str(cutoff - 1), host, session)[1]
        self.assertNotIn(b"FIXTURE1", before)
        at = self.get(port, replay + str(cutoff), host, session)[1]
        self.assertIn(b"FIXTURE1", at)
        self.assertIn(b'data-mode="replay"', at)
        self.assertNotIn(b"TREU-LIVE", at)
        after = self.get(port, replay + str(cutoff + 2000), host, session)[1]
        self.assertIn(b"TREU-LIVE", after)
        self.assertNotIn(b"TREU-ONLY", after)
        self.assertEqual(self.get(port, replay + str(cutoff) + "&view=live", host, session)[0], 400)
        with mock.patch.object(sp, "MAX_ROWS", 1):
            tape = self.archive / "events.jsonl"
            split = tape.stat().st_size
            with tape.open("a") as handle:
                handle.write(json.dumps({"kind": "publication", "id": "LATER",
                                         "first_seen": "2099-01-01T10:00:00Z"}) + "\n")
            newest = self.get(port, replay + str(cutoff), host, session)[1]
            self.assertNotIn(b"LATER", newest)
            self.assertIn(f"&amp;end={split}&amp;seq=0".encode(), newest)
            older = self.get(port, replay + str(cutoff) + f"&end={split}&seq=0", host, session)[1]
            self.assertIn(b"FIXTURE1", older)
            self.assertNotIn(b"older archive page", older)
            with tape.open("r+b") as handle:
                handle.truncate(split)
        focused = self.get(port, page + "?view=treu", host, session)[1]
        self.assertIn(b'data-publisher-filter="Tradeweb"', focused)
        self.assertIn(b"FIXTURE1", focused)
        self.assertNotIn(b"TREU-LIVE", self.get(port, page, host)[1])


        self.assertIn(b"5M+", body)
        self.assertIn("no-store", response.getheader("Cache-Control"))
        with (self.archive / "events.jsonl").open("ab") as f:
            f.write(b'{"unfinished"')
        events, cursor = sp.read_table_window(self.archive)
        self.assertEqual(len(events), 1)
        self.assertLess(cursor, (self.archive / "events.jsonl").stat().st_size)

    def test_github_mode_gates_every_data_route(self):
        s = sp.Settings(self.archive, "https://cds.example.test", "cid", "secret", frozenset({42}))
        port = self.serve(s)
        host = "cds.example.test"
        for path in ("/api/trades", "/api/raw?ref=dtcc-sec-credits/raw/2099-01-01/x_abc.json.gz",
                     "/api/export?from=2099-01-01T00:00:00&to=2099-01-02T00:00:00", "/panel.js"):
            self.assertEqual(self.get(port, path, host)[0], 401, path)
        self.assertEqual(self.get(port, "/api/trades", "evil.example")[0], 403)

    def test_allowlisted_identity_gets_session_others_refused(self):
        s = sp.Settings(self.archive, "https://cds.example.test", "cid", "secret", frozenset({42}))
        store = sp.Store()
        for uid, expect in ((7, 403), (42, 303)):
            handler = sp.make_handler(s, store=store, identity=lambda st, c, v, uid=uid: uid)
            srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.shutdown)
            flow, state, _ = store.start_flow()
            st, _, r = self.get(srv.server_port, f"/auth/callback?state={state}&code=c&iss=https%3A%2F%2Fgithub.com%2Flogin%2Foauth",
                                "cds.example.test", f"{sp.FLOW_COOKIE}={flow}")
            self.assertEqual(st, expect)
            if expect == 303:
                sess = [c for c in r.headers.get_all("Set-Cookie") if c.startswith(sp.SESSION_COOKIE + "=") and "Max-Age=28800" in c][0]
                token = sess.split(";")[0]
                st, body, _ = self.get(srv.server_port, "/api/trades", "cds.example.test", token)
                self.assertEqual(st, 200)
                self.assertEqual(json.loads(body)["rows"][0]["summary"]["notionalAmountLeg1"], "5,000,000+")

    def test_dev_local_serves_loopback_only_and_rejects_traversal(self):
        port = self.serve(sp.Settings(self.archive, dev_local=True))
        self.assertEqual(self.get(port, "/api/private/identity")[0], 404)
        self.assertEqual(self.get(port, "/api/trades")[0], 200)
        self.assertEqual(self.get(port, "/api/trades", "cds.example.test")[0], 403)
        self.assertEqual(self.get(port, "/api/raw?ref=../../etc/passwd")[0], 400)
        st, body, r = self.get(port, "/api/raw?ref=dtcc-sec-credits/raw/2099-01-01/x_abc.json.gz")
        self.assertEqual((st, body), (200, b'{"tradeList":[]}'))
        self.assertTrue(r.getheader("X-Original-SHA256"))

    def test_settings_require_https_origin_and_numeric_allowlist(self):
        base = {"CREDIT_TAPE_ARCHIVE": str(self.archive), "CREDIT_TAPE_GITHUB_CLIENT_ID": "c",
                "CREDIT_TAPE_GITHUB_CLIENT_SECRET": "s"}
        with self.assertRaises(ValueError):
            sp.Settings.from_env({**base, "CREDIT_TAPE_PUBLIC_ORIGIN": "http://x.test", "CREDIT_TAPE_GITHUB_ALLOWED_IDS": "1"})
        with self.assertRaises(ValueError):
            sp.Settings.from_env({**base, "CREDIT_TAPE_PUBLIC_ORIGIN": "https://x.test", "CREDIT_TAPE_GITHUB_ALLOWED_IDS": "octocat"})


class ServiceReleaseTests(unittest.TestCase):
    """Release handoff: fail-closed service config, per-channel health, restart check."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = Path(self.tmp.name)
        self.volume = self.archive / "volume"
        self.volume.mkdir()
        self.env = {"CREDIT_TAPE_ARCHIVE": str(self.volume / "credit-tape"), "CREDIT_TAPE_VOLUME": str(self.volume),
                    "CREDIT_TAPE_PUBLIC_ORIGIN": "https://cds.example.test",
                    "CREDIT_TAPE_GITHUB_CLIENT_ID": "cid", "CREDIT_TAPE_GITHUB_CLIENT_SECRET": "secret",
                    "CREDIT_TAPE_GITHUB_ALLOWED_IDS": "42"}

    def test_service_refuses_incomplete_gate_and_dev_local(self):
        from market_data.credit_tape import service
        for env in ({k: v for k, v in self.env.items() if k != "CREDIT_TAPE_GITHUB_CLIENT_SECRET"},
                    {k: v for k, v in self.env.items() if k != "CREDIT_TAPE_GITHUB_ALLOWED_IDS"},
                    {**self.env, "CREDIT_TAPE_DEV_LOCAL": "1"},
                    {**self.env, "CREDIT_TAPE_INTERVAL": "1"}):
            self.assertEqual(service.main(env, is_mount=lambda p: True), 2)
        settings, interval, port = service.config({**self.env, "PORT": "9123"}, is_mount=lambda p: True)
        self.assertEqual((settings.origin, settings.allowed_ids, interval, port),
                         ("https://cds.example.test", frozenset({42}), 20.0, 9123))
        self.assertFalse(settings.dev_local)

    def _run_slice_only(self, dtcc):
        capture.run(str(self.archive), ticker=False, slices=True, once=True, opener=dtcc)

    def test_slice_only_run_health_and_restart_check(self):
        from market_data.credit_tape import check_archive, synthetic_demo
        from market_data.credit_tape.synthetic_resilience import SyntheticDTCC
        dtcc = SyntheticDTCC()
        for agency, key in (("cftc", "CFTC_CR"), ("sec", "SEC_CR")):
            meta, blob = synthetic_demo.fixture(agency)
            dtcc.files[meta["fullFilePath"]] = blob
            dtcc.index[key] = [meta]
        self._run_slice_only(dtcc)
        before = check_archive.check(str(self.archive))
        self.assertTrue(before["clean"], before)
        self.assertEqual(before["publications"], {"CFTC_CR": 1, "SEC_CR": 1})
        self.assertEqual(before["ticker_publications"], 0)
        health = json.loads((self.archive / "health.json").read_text(encoding="utf-8"))
        self.assertEqual(set(health["sources"]), {"dtcc-slice-index"})  # ticker output off
        ok, body = sp.collector_health(self.archive)
        self.assertTrue(ok, body)
        self.assertTrue(body["slice_only"])
        self.assertEqual({k: v["state"] for k, v in body["channels"].items()}, {"CFTC_CR": "ok", "SEC_CR": "ok"})
        for _ in range(2):  # restarts on an unchanged index
            self._run_slice_only(dtcc)
        after = check_archive.check(str(self.archive))
        self.assertTrue(after["clean"], after)
        self.assertTrue(check_archive.compare(before, after)["ok"])
        self.assertEqual(after["publications"], {"CFTC_CR": 1, "SEC_CR": 1})
        # A duplicated line is caught.
        ev = self.archive / "events.jsonl"
        last = ev.read_text(encoding="utf-8").splitlines()[-1]
        with ev.open("a", encoding="utf-8") as fh:
            fh.write(last + "\n")
        self.assertFalse(check_archive.check(str(self.archive))["clean"])

    def test_collector_health_flags_each_channel(self):
        now = time.time()
        iso = lambda age: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - age)) + "Z"
        ch = lambda **kw: {"listed_in_index": True, "listed_slices": 3, "listing_at": iso(30),
                           "pending_live": 0, "pending_backfill": 0, "oldest_live_pending_s": 0, "max_attempts": 0, **kw}

        def write(beat_age=5, index_age=10, cftc=None, sec=None):
            (self.archive / "health.json").write_text(json.dumps({
                "t": iso(beat_age), "collector": {"ticker": False, "slices": True, "interval_s": 20},
                "sources": {"dtcc-slice-index": {"last_index_at": iso(index_age), "polls_err": 0,
                                                 "channels": {"CFTC_CR": cftc or ch(), "SEC_CR": sec or ch()}}}}))
            return sp.collector_health(self.archive, now)

        self.assertTrue(write()[0])
        ok, body = write(sec=ch(listed_in_index=False))
        self.assertEqual((ok, body["channels"]["SEC_CR"]["state"], body["channels"]["CFTC_CR"]["state"]),
                         (False, "missing_from_index", "ok"))
        ok, body = write(cftc=ch(pending_live=1, oldest_live_pending_s=901))
        self.assertEqual((ok, body["channels"]["CFTC_CR"]["state"]), (False, "live_backlog"))
        self.assertFalse(write(beat_age=400)[0])
        self.assertFalse(write(index_age=700)[0])
        (self.archive / "health.json").unlink()
        self.assertFalse(sp.collector_health(self.archive)[0])

    def test_health_endpoint_is_public_but_data_routes_stay_gated(self):
        s = sp.Settings.from_env(self.env)
        srv = ThreadingHTTPServer(("127.0.0.1", 0), sp.make_handler(s, identity=lambda *a: 0))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)

        def get(path, host):
            c = HTTPConnection("127.0.0.1", srv.server_port, timeout=5)
            c.request("GET", path, headers={"Host": host})
            r = c.getresponse()
            return r.status, r.read()

        st, body = get("/healthz/collector", "healthcheck.railway.app")  # no heartbeat yet
        self.assertEqual(st, 503)
        self.assertEqual(json.loads(body)["ok"], False)
        self.assertEqual(get("/healthz", "healthcheck.railway.app")[0], 200)
        for path in ("/", "/api/trades", "/api/raw?ref=dtcc-slices/raw/2999-01-01/X.zip",
                     "/api/export?from=2999-01-01T00:00:00&to=2999-01-02T00:00:00", "/panel.js"):
            self.assertEqual(get(path, "cds.example.test")[0], 401 if path != "/" else 200, path)
            self.assertEqual(get(path, "healthcheck.railway.app")[0], 403, path)


class ReleaseReadinessTests(unittest.TestCase):
    """Chej's release-readiness revision: OAuth flow lifecycle, raw-pending, checker, worker failure."""

    ORIGIN, HOST = "https://cds.example.test", "cds.example.test"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = Path(self.tmp.name)
        self.settings = sp.Settings(self.archive, self.ORIGIN, "cid", "secret", frozenset({42}))

    # -- OAuth (reference: directional-move-lab scripts/serve_private.py @2bdebbe) ------------
    def _server(self, store, owner):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), sp.make_handler(self.settings, store=store,
                                                                    identity=lambda s, c, v: owner()))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return srv.server_port

    def _get(self, port, path, cookie=None):
        c = HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", path, headers={"Host": self.HOST, **({"Cookie": cookie} if cookie else {})})
        r = c.getresponse()
        r.read()
        return r.status, r

    def test_github_documented_code_and_state_callback_signs_in_once(self):
        store = sp.Store()
        port = self._server(store, lambda: 42)
        flow, state, _ = store.start_flow()
        ck = f"{sp.FLOW_COOKIE}={flow}"
        st, r = self._get(port, f"/auth/callback?code=abc&state={state}", ck)  # GitHub's documented form
        self.assertEqual(st, 303)
        session = [c for c in r.headers.get_all("Set-Cookie") if c.startswith(sp.SESSION_COOKIE + "=") and "Max-Age=28800" in c]
        self.assertEqual(len(session), 1)
        self.assertEqual(self._get(port, "/api/trades", session[0].split(";")[0])[0], 200)
        # One-time replay protection: the same flow+state is refused afterwards.
        self.assertEqual(self._get(port, f"/auth/callback?code=abc&state={state}", ck)[0], 403)
        self.assertEqual(len(store.spent_flows), 1)
        self.assertEqual(store.pending_flows, set())

    def test_callback_shape_is_strict(self):
        store = sp.Store()
        port = self._server(store, lambda: 42)
        for q in ("code=a&state={s}&iss=https%3A%2F%2Fevil.example", "code=a&state={s}&extra=1", "code=&state={s}",
                  "code=a&code=b&state={s}", "state={s}"):
            flow, state, _ = store.start_flow()
            self.assertEqual(self._get(port, "/auth/callback?" + q.format(s=state), f"{sp.FLOW_COOKIE}={flow}")[0], 403, q)
        flow, state, _ = store.start_flow()  # RFC 9207 form with GitHub's exact issuer still works
        self.assertEqual(self._get(port, f"/auth/callback?code=a&state={state}&iss=https%3A%2F%2Fgithub.com%2Flogin%2Foauth",
                                   f"{sp.FLOW_COOKIE}={flow}")[0], 303)

    def test_failed_callbacks_release_their_claim_and_cannot_exhaust_sign_in(self):
        store = sp.Store()
        who = {"id": 7}

        def owner():
            if who["id"] is None:
                raise RuntimeError("GitHub exchange failed")
            return who["id"]

        port = self._server(store, owner)
        for i in range(200):  # refused identities and failed exchanges, well past both caps
            who["id"] = 7 if i % 2 else None
            flow, state, _ = store.start_flow()
            self.assertEqual(self._get(port, f"/auth/callback?code=c&state={state}", f"{sp.FLOW_COOKIE}={flow}")[0], 403)
        self.assertEqual((store.pending_flows, store.spent_flows), (set(), {}))  # failure cleanup, no replay records
        who["id"] = 42
        flow, state, _ = store.start_flow()
        self.assertEqual(self._get(port, f"/auth/callback?code=c&state={state}", f"{sp.FLOW_COOKIE}={flow}")[0], 303)

    def test_login_reuses_a_live_flow_and_refuses_a_spent_one(self):
        clock = {"t": 1000.0}
        store = sp.Store(clock=lambda: clock["t"])
        flow, state, verifier = store.start_flow()
        self.assertEqual(store.start_flow(flow), (flow, state, verifier))  # reuse, no new expiry
        store.consume_flow(flow, state)
        store.finish_flow(flow, accepted=True)
        self.assertNotEqual(store.start_flow(flow)[0], flow)
        with self.assertRaises(ValueError):
            store.consume_flow(flow, state)
        clock["t"] += 601  # replay record pruned only once the flow itself has expired
        with self.assertRaises(ValueError):
            store.consume_flow(flow, state)

    # -- raw originals --------------------------------------------------------------------------
    def _route(self, http):
        from market_data.credit_tape import volume
        volume.ensure_identity(str(self.archive))
        return slices.SliceRoute(str(self.archive), capture.now, capture.append_jsonl, opener=http, lock=capture.WRITE_LOCK)

    def _stop_after_commit(self, http):
        r = self._route(http)

        def hook(stage):
            if stage == "after_commit_record":
                raise slices.SimulatedStop(stage)
            return False

        r.crash_hook = hook
        r.poll_index(str(self.archive / "events.jsonl"))
        with self.assertRaises(slices.SimulatedStop):
            r.drain_live(str(self.archive / "events.jsonl"), str(self.archive / "alerts.jsonl"))

    def _setup_slice(self):
        m = slice_meta(1, 1, "2999-01-01T00:00:0%dZ")
        z = fixture_zip([{"Dissemination Identifier": "R1", "Action type": "NEWT"}])
        http = FakeHTTP({"CFTC_CR": [m], "SEC_CR": []}, {m["fullFilePath"]: z})
        raw = self.archive / "dtcc-slices/raw/2999-01-01" / m["fileName"]
        return m, z, http, raw

    def test_missing_raw_stays_pending_until_matching_bytes_return(self):
        from market_data.credit_tape import check_archive
        m, z, http, raw = self._setup_slice()
        self._stop_after_commit(http)
        raw.unlink()  # original lost after the commit record was written
        http.files[m["fullFilePath"]] = b"different bytes"
        r = self._route(http)
        self.assertEqual(r.recovered[0]["status"], "awaiting_raw")
        self.assertFalse(r._done("CFTC_CR", 1001))
        self.assertIn("CFTC_CR:1001", r.pending)
        ev, al = str(self.archive / "events.jsonl"), str(self.archive / "alerts.jsonl")
        r.drain_live(ev, al)  # wrong bytes: still pending, still not done
        self.assertFalse(r._done("CFTC_CR", 1001))
        self.assertIn("differ from the committed sha256", r.pending["CFTC_CR:1001"]["last_error"])
        self.assertFalse(check_archive.check(str(self.archive))["clean"])
        http.files[m["fullFilePath"]] = z
        r.pending["CFTC_CR:1001"]["retry_at"] = 0
        r.drain_live(ev, al)
        self.assertTrue(r._done("CFTC_CR", 1001))
        self.assertEqual(raw.read_bytes(), z)
        pubs = [e for e in lines(ev) if e["kind"] == "publication"]
        self.assertEqual([e["id"] for e in pubs], ["R1"])
        self.assertTrue(check_archive.check(str(self.archive))["clean"])

    def test_hash_invalid_raw_is_not_marked_complete_on_restart(self):
        m, z, http, raw = self._setup_slice()
        self._stop_after_commit(http)
        raw.write_bytes(b"corrupted")
        http.fail.add(m["fullFilePath"])  # source unavailable for now
        r = self._route(http)
        r.drain_live(str(self.archive / "events.jsonl"), str(self.archive / "alerts.jsonl"))
        self.assertFalse(r._done("CFTC_CR", 1001))
        self.assertEqual(r.pending["CFTC_CR:1001"]["attempts"], 1)
        r2 = self._route(http)  # still pending across another restart
        self.assertIn("CFTC_CR:1001", r2.pending)
        self.assertEqual(r2.recovered[0]["status"], "awaiting_raw")

    # -- archive checker ------------------------------------------------------------------------
    def test_checker_rejects_missing_archive_and_any_ticker_output(self):
        from market_data.credit_tape import check_archive
        missing = check_archive.check(str(self.archive / "not-mounted"))
        self.assertFalse(missing["clean"])
        self.assertFalse(check_archive.compare(missing, missing)["ok"])
        (self.archive / "dtcc-slices").mkdir()
        self.assertFalse(check_archive.check(str(self.archive))["clean"])  # no archive identity
        from market_data.credit_tape import volume
        volume.ensure_identity(str(self.archive))
        self.assertTrue(check_archive.check(str(self.archive))["clean"])
        (self.archive / "events.jsonl").write_text(json.dumps({"source": "dtcc-sec-credits", "kind": "possible_gap"}) + "\n")
        res = check_archive.check(str(self.archive))
        self.assertEqual((res["clean"], res["ticker_events"]), (False, 1))
        (self.archive / "events.jsonl").write_text("")
        (self.archive / "dtcc-cftc-credits").mkdir()
        self.assertFalse(check_archive.check(str(self.archive))["clean"])
        with mock.patch.object(sys, "argv", ["check_archive", "--archive", str(self.archive / "nope")]), \
                mock.patch("builtins.print"):
            self.assertEqual(check_archive.main(), 1)

    # -- replay worker failure ------------------------------------------------------------------
    def test_replay_worker_failure_stops_collector_and_service(self):
        from market_data.credit_tape import service
        replay = slice_meta(1, 1)  # disseminated before start -> backfill thread

        class Broken(FakeHTTP):
            def __call__(self, req, timeout=None):
                if req.full_url.endswith(".zip"):
                    raise RuntimeError("unexpected decoder bug")
                return super().__call__(req, timeout)

        http = Broken({"CFTC_CR": [replay], "SEC_CR": []}, {})
        stop = threading.Event()
        with self.assertRaisesRegex(RuntimeError, "replay backfill worker failed"):
            capture.run(str(self.archive), interval=0.05, ticker=False, slices=True, stop=stop, opener=http)
        health = json.loads((self.archive / "health.json").read_text(encoding="utf-8"))
        self.assertEqual(health["sources"]["dtcc-slice-index"]["backfill_worker"]["state"], "failed")
        ok, body = sp.collector_health(self.archive)
        self.assertEqual((ok, body["backfill_worker"]), (False, "failed"))
        env = {"CREDIT_TAPE_ARCHIVE": str(self.archive / "vol" / "credit-tape"), "CREDIT_TAPE_VOLUME": str(self.archive / "vol"),
               "CREDIT_TAPE_PUBLIC_ORIGIN": self.ORIGIN,
               "CREDIT_TAPE_GITHUB_CLIENT_ID": "cid", "CREDIT_TAPE_GITHUB_CLIENT_SECRET": "secret",
               "CREDIT_TAPE_GITHUB_ALLOWED_IDS": "42", "PORT": "0"}
        with mock.patch.object(service.capture, "run", side_effect=RuntimeError("replay backfill worker failed: x")), \
                mock.patch.object(service.treu, "run", side_effect=lambda archive, stop: stop.wait()), \
                mock.patch.object(service.signal, "signal"), mock.patch("builtins.print"):
            (self.archive / "vol").mkdir()
            self.assertEqual(service.main(env, is_mount=lambda p: True), 1)


class PersistentArchiveProofTests(unittest.TestCase):
    """Chej: prove the restart check reads the actual persistent archive (local fixtures only)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vol = self.root / "volume"
        self.archive = self.vol / "credit-tape"

    def env(self, **kw):
        return {"CREDIT_TAPE_ARCHIVE": str(self.archive), "CREDIT_TAPE_VOLUME": str(self.vol),
                "CREDIT_TAPE_PUBLIC_ORIGIN": "https://cds.example.test", "CREDIT_TAPE_GITHUB_CLIENT_ID": "cid",
                "CREDIT_TAPE_GITHUB_CLIENT_SECRET": "secret", "CREDIT_TAPE_GITHUB_ALLOWED_IDS": "42", "PORT": "0", **kw}

    # -- missing volume ---------------------------------------------------------------------------
    def test_service_refuses_absent_volume_and_writes_nothing(self):
        from market_data.credit_tape import service
        mounted = lambda p: True
        cases = {
            "volume path missing": (self.env(), mounted),
            "volume not a mount point": (self.env(), os.path.ismount),  # a plain directory
            "no volume named": ({k: v for k, v in self.env().items() if k != "CREDIT_TAPE_VOLUME"}, mounted),
            "archive outside volume": (self.env(CREDIT_TAPE_ARCHIVE=str(self.root / "elsewhere")), mounted),
            "archive is the volume root": (self.env(CREDIT_TAPE_ARCHIVE=str(self.vol)), mounted),
        }
        with mock.patch("builtins.print"), mock.patch.object(service.capture, "run") as run:
            for name, (env, is_mount) in cases.items():
                if name != "volume path missing":
                    self.vol.mkdir(exist_ok=True)
                self.assertEqual(service.main(env, is_mount=is_mount), 2, name)
                self.assertFalse(self.archive.exists(), name)
                self.assertFalse((self.root / "elsewhere").exists(), name)
            run.assert_not_called()
        # Railway's own variable is honoured when CREDIT_TAPE_VOLUME is not set.
        env = {k: v for k, v in self.env().items() if k != "CREDIT_TAPE_VOLUME"}
        env["RAILWAY_VOLUME_MOUNT_PATH"] = str(self.vol)
        service.config(env, is_mount=lambda p: True)

    def test_checker_refuses_unmounted_volume_and_a_different_archive(self):
        from market_data.credit_tape import check_archive
        self._collect()
        ok = check_archive.check(str(self.archive), str(self.vol), is_mount=lambda p: True)
        self.assertTrue(ok["clean"], ok)
        self.assertTrue(ok["volume"]["mounted_and_contains_archive"])
        bad = check_archive.check(str(self.archive), str(self.vol), is_mount=lambda p: False)
        self.assertFalse(bad["clean"])
        self.assertFalse(check_archive.check(str(self.archive), str(self.root / "no-such-volume"))["clean"])
        # Restart that came up on a fresh directory: different archive_id -> compare fails.
        other = self.root / "fresh"
        self._collect(other)
        after = check_archive.check(str(other))
        self.assertTrue(after["clean"])
        cmp = check_archive.compare(ok, after)
        self.assertEqual((cmp["same_archive_id"], cmp["ok"]), (False, False))
        self.assertTrue(check_archive.compare(ok, check_archive.check(str(self.archive)))["ok"])

    # -- completed commits vs originals and lines -------------------------------------------------
    def _collect(self, out=None):
        from market_data.credit_tape import synthetic_demo
        from market_data.credit_tape.synthetic_resilience import SyntheticDTCC
        out = out or self.archive
        dtcc = SyntheticDTCC()
        for agency, key in (("cftc", "CFTC_CR"), ("sec", "SEC_CR")):
            meta, blob = synthetic_demo.fixture(agency)
            dtcc.files[meta["fullFilePath"]] = blob
            dtcc.index[key] = [meta]
        bad = dict(dtcc.index["CFTC_CR"][0], sliceId=900000000000000102,
                   fileName="CFTC_SLICE_CREDITS_2999_01_01_2.zip",
                   fullFilePath=f"https://{slices.SLICE_HOST}/cftc/slices/CFTC_SLICE_CREDITS_2999_01_01_2.zip")
        dtcc.files[bad["fullFilePath"]] = b"not a zip"  # decode failure, still archived as published
        dtcc.index["CFTC_CR"].append(bad)
        with mock.patch.object(slices.SliceRoute, "is_replay", lambda self, m: False):  # live: alerts too
            capture.run(str(out), ticker=False, slices=True, once=True, opener=dtcc)
        return out

    def _raw(self, name):
        return self.archive / "dtcc-slices/raw/2999-01-01" / name

    def test_completed_commits_validate_including_decode_failure(self):
        from market_data.credit_tape import check_archive
        self._collect()
        res = check_archive.check(str(self.archive))
        self.assertTrue(res["clean"], res)
        self.assertEqual((res["commits_completed_validated"], res["originals_rechecked"]), (3, 3))
        kinds = [e["kind"] for e in lines(str(self.archive / "events.jsonl"))]
        self.assertEqual(kinds.count("decode_failure"), 1)
        self.assertEqual(kinds.count("publication"), 2)

    def test_missing_original_is_reported_per_commit(self):
        from market_data.credit_tape import check_archive
        self._collect()
        self._raw("SEC_SLICE_CREDITS_2999_01_01_1.zip").unlink()
        self._raw("CFTC_SLICE_CREDITS_2999_01_01_2.zip").unlink()  # the decode-failure original
        self._raw("CFTC_SLICE_CREDITS_2999_01_01_1.zip").write_bytes(b"tampered")
        res = check_archive.check(str(self.archive))
        self.assertFalse(res["clean"])
        got = sorted((p["commit"], p["problem"]) for p in res["commit_problem_list"])
        self.assertEqual(got, [
            ("dtcc-cftc-slices/CFTC_CR/900000000000000101", "original_hash_mismatch"),
            ("dtcc-cftc-slices/CFTC_CR/900000000000000102", "original_missing"),
            ("dtcc-sec-slices/SEC_CR/900000000000000101", "original_missing"),
        ])

    def test_missing_or_altered_committed_line_is_reported(self):
        from market_data.credit_tape import check_archive
        self._collect()
        ev, al = self.archive / "events.jsonl", self.archive / "alerts_outbox.jsonl"
        evs = ev.read_text(encoding="utf-8").splitlines()
        drop = next(i for i, l in enumerate(evs) if '"kind":"publication"' in l and "SYN-SEC-0001" in l)
        ev.write_text("\n".join(evs[:drop] + evs[drop + 1:]) + "\n", encoding="utf-8")
        als = al.read_text(encoding="utf-8").splitlines()
        dfail = next(i for i, l in enumerate(als) if '"kind":"decode_failure"' in l)
        al.write_text("\n".join(als[:dfail] + als[dfail + 1:]) + "\n", encoding="utf-8")
        res = check_archive.check(str(self.archive))
        got = sorted((p["commit"], p["problem"]) for p in res["commit_problem_list"])
        self.assertEqual(got, [
            ("dtcc-cftc-slices/CFTC_CR/900000000000000102", "committed_alert_line_missing"),
            ("dtcc-sec-slices/SEC_CR/900000000000000101", "committed_event_line_missing"),
        ])
        self.assertFalse(res["clean"])
        # An edited line (same key, different content) is caught as well.
        self.tearDown_and_rebuild()
        txt = (self.archive / "events.jsonl").read_text(encoding="utf-8").replace("SYN-CFTC-0001", "SYN-CFTC-EDIT", 1)
        (self.archive / "events.jsonl").write_text(txt, encoding="utf-8")
        probs = {p["problem"] for p in check_archive.check(str(self.archive))["commit_problem_list"]}
        self.assertIn("committed_event_line_differs", probs)

    def tearDown_and_rebuild(self):
        import shutil
        shutil.rmtree(self.archive)
        self._collect()


class ReconciliationTests(unittest.TestCase):
    """DTCC coverage reconciliation (synthetic fixtures; the real-day run is reported separately)."""

    DAY, TAG = "2001-02-03", "2001_02_03"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = Path(self.tmp.name)

    @staticmethod
    def row(did, action="NEWT", orig="", notional="5,000,000+"):
        return {"Dissemination Identifier": did, "Original Dissemination Identifier": orig, "Action type": action,
                "Notional amount-Leg 1": notional}

    def zipped(self, rows):
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(self.row("x")))
        w.writeheader()
        for r in rows:
            w.writerow(r)
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("X.csv", buf.getvalue())
        return out.getvalue()

    def captured(self):
        r = self.row
        return {1: [r("A"), r("B")],
                3: [r("C", "CORR", "A"), r("D", "TERM", "Z-NOT-SEEN"), r("R")],
                4: [r("R"), r("X"), r("S", notional="1")],   # R reported twice; X never in the cumulative file
                5: [r("F", notional="1,000,000"), r("S", notional="2")]}

    def build(self):
        """Capture through the real slice route so the archive has commits, done markers and an identity."""
        from market_data.credit_tape.synthetic_resilience import SyntheticDTCC
        dtcc = SyntheticDTCC()
        metas = []
        for n, rows in self.captured().items():
            name = f"CFTC_SLICE_CREDITS_{self.TAG}_{n}.zip"
            m = {"sliceId": 5000 + n, "fileName": name, "rowCount": len(rows), "dissemDTM": f"{self.DAY}T00:0{n}:10Z",
                 "startTs": f"{self.DAY}T00:0{n}:00Z", "endTs": f"{self.DAY}T00:0{n}:09Z",
                 "fullFilePath": f"https://{slices.SLICE_HOST}/cftc/slices/{name}"}
            dtcc.files[m["fullFilePath"]] = self.zipped(rows)
            metas.append(m)
        dtcc.index = {"CFTC_CR": metas, "SEC_CR": []}  # slice 2 never listed: uncaptured
        capture.run(str(self.archive), ticker=False, slices=True, once=True, opener=dtcc)

    def ppd(self, gap=None, cumulative=None, cumulative_status=200):
        r = self.row
        gap = {2: [r("G", "EROR", "B")]} if gap is None else gap
        cumulative = cumulative if cumulative is not None else [
            r("A"), r("B"), r("C", "CORR", "A"), r("D", "TERM", "Z-NOT-SEEN"), r("R"), r("X2"),
            r("S", notional="1"), r("S", notional="1"), r("F", notional="5,000,000+"), r("G", "EROR", "B"), r("H")]
        files = {f"/intraday/cftc/CFTC_SLICE_CREDITS_{self.TAG}_{n}.zip": rows if isinstance(rows, bytes) else self.zipped(rows)
                 for n, rows in gap.items()}
        if cumulative_status == 200:
            files[f"/cumulative/cftc/CFTC_CUMULATIVE_CREDITS_{self.TAG}.zip"] = self.zipped(cumulative)
        self.requests = []

        def open_(req, timeout=None):
            path = req.full_url.split("/ppd/api/report", 1)[1]
            self.requests.append(path)
            if path not in files:
                raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
            return FakeResp(files[path])
        return open_

    def rec(self, recover_gaps=True, **kw):
        from market_data.credit_tape import reconcile
        return reconcile.run(str(self.archive), self.DAY, ["CFTC_CR"], recover_gaps=recover_gaps,
                             fetch_cumulative=True, opener=self.ppd(**kw))

    @staticmethod
    def by_id(parity):
        return {u["id"]: u for u in parity["unmatched"]}

    def test_full_row_multiplicities_report_unmatched_occurrences_on_both_sides(self):
        self.build()
        ev = (self.archive / "events.jsonl").read_bytes()
        al = (self.archive / "alerts_outbox.jsonl").read_bytes() if (self.archive / "alerts_outbox.jsonl").exists() else b""
        res = self.rec()
        self.assertTrue(res["archive_audit"]["clean"])
        ch = res["channels"]["CFTC_CR"]
        self.assertEqual(ch["tape_rows"], {"logged": 10, "verified": 10, "unverified": 0})
        tp = ch["tape_parity"]
        u = self.by_id(tp)
        # S: same total count (2 vs 2) but different rows -> still reported, both sides.
        self.assertEqual((u["S"]["state"], u["S"]["same_total_count"]), ("occurrence_mismatch", True))
        self.assertEqual(sum(x["count"] for x in u["S"]["unmatched_tape"]), 1)
        self.assertEqual(sum(x["count"] for x in u["S"]["unmatched_cumulative"]), 1)
        # R: 2 on the tape, 1 in the cumulative file -> one unmatched tape occurrence, none on the other side.
        self.assertEqual((u["R"]["same_total_count"], u["R"]["unmatched_tape"][0]["count"], u["R"]["unmatched_cumulative"]),
                         (False, 1, []))
        self.assertEqual(u["F"]["differing_fields"], ["Notional amount-Leg 1"])
        self.assertEqual({k: u[k]["state"] for k in ("X", "X2", "G", "H")},
                         {"X": "captured_only", "X2": "cumulative_only", "G": "cumulative_only", "H": "cumulative_only"})
        self.assertEqual(tp["ids"]["matched"], 4)  # A B C D
        self.assertEqual(tp["occurrences"], {"matched": 6, "unmatched_tape": 4, "unmatched_cumulative": 5})
        self.assertFalse(tp["parity"])
        # Report-only recovery is a separate view; it resolves G but never becomes tape parity.
        self.assertNotIn("G", self.by_id(ch["with_report_only"]))
        self.assertIn("G", u)
        cov = ch["slice_coverage"]
        self.assertEqual((cov["uncaptured_numbers"], cov["report_only_recovered"], cov["unknown_numbers"]), ([2], [2], []))
        self.assertEqual(cov["tail"], {"last_captured_number": 5, "boundary": "unknown",
                                       "probes": [{"number": 6, "status": "unavailable_http_404"}]})
        self.assertTrue(all(s["report_only"] for s in ch["report_only"]["slices"]))
        marker = json.loads((self.archive / "dtcc-reconcile" / self.DAY / "report-only-slices" / "CFTC_CR.REPORT_ONLY.json").read_text())
        self.assertTrue(marker["report_only"])
        # Unobserved original is unknown, not an earlier-day claim.
        self.assertEqual(ch["lifecycle_links"], {"original_observed_in_interval": 2, "original_not_observed": 1})
        self.assertEqual(ch["original_not_observed"], [{"id": "D", "original_id": "Z-NOT-SEEN", "action": "TERM"}])
        self.assertEqual(ch["verdict"]["tape_parity"], "incomplete")
        self.assertEqual(ch["verdict"]["unknowns"]["tail_boundary"], "unknown")
        # Tape logs untouched; daily file saved as an original apart from the tape.
        self.assertEqual((self.archive / "events.jsonl").read_bytes(), ev)
        self.assertEqual((self.archive / "alerts_outbox.jsonl").read_bytes() if (self.archive / "alerts_outbox.jsonl").exists() else b"", al)
        self.assertTrue((self.archive / "dtcc-reconcile" / self.DAY / "cumulative" / f"CFTC_CUMULATIVE_CREDITS_{self.TAG}.zip").exists())
        from market_data.credit_tape import check_archive
        self.assertTrue(check_archive.check(str(self.archive))["clean"])  # reconcile output does not disturb the audit

    def test_unavailable_responses_are_unknown(self):
        self.build()
        res = self.rec(gap={})  # slice 2 answers 404
        ch = res["channels"]["CFTC_CR"]
        cov = ch["slice_coverage"]
        self.assertEqual((cov["unknown_numbers"], cov["fetch_status"]), ([2], {"2": "unavailable_http_404"}))
        self.assertNotIn("with_report_only", ch)
        self.assertEqual(ch["verdict"]["unknowns"]["slice_numbers"], [2])
        res = self.rec(cumulative_status=404)
        ch = res["channels"]["CFTC_CR"]
        self.assertEqual((ch["tape_parity"], ch["verdict"]["tape_parity"]), (None, "unknown"))
        self.assertFalse(res["complete"])

    def test_clean_day_is_complete_only_after_a_clean_audit(self):
        self.build()
        tape = [row for rows in self.captured().values() for row in rows]
        res = self.rec(recover_gaps=False, cumulative=tape)
        ch = res["channels"]["CFTC_CR"]
        self.assertEqual(ch["verdict"]["tape_parity"], "complete", ch["verdict"])
        self.assertTrue(res["complete"])
        self.assertEqual(ch["tape_parity"]["repeated_reports_total"], 2)  # R and S kept, consistent
        self.assertEqual(self.requests, [f"/cumulative/cftc/CFTC_CUMULATIVE_CREDITS_{self.TAG}.zip"])
        # Same parity, but a tampered original fails the audit: rows become unverified.
        raw = self.archive / "dtcc-slices" / "raw" / self.DAY / f"CFTC_SLICE_CREDITS_{self.TAG}_1.zip"
        raw.write_bytes(b"tampered")
        res = self.rec(recover_gaps=False, cumulative=tape)
        ch = res["channels"]["CFTC_CR"]
        self.assertFalse(res["archive_audit"]["clean"])
        self.assertEqual((ch["verdict"]["tape_parity"], ch["tape_rows"]["verified"]), ("unverified", 0))
        self.assertFalse(res["complete"])

    def test_legacy_archive_without_commits_is_unverified(self):
        r = self.row
        name = f"CFTC_SLICE_CREDITS_{self.TAG}_1.zip"
        meta = {"fileName": name, "sliceId": 1}
        (self.archive / "dtcc-slices").mkdir()
        (self.archive / "events.jsonl").write_text(
            json.dumps({"source": "dtcc-cftc-slices", "kind": "slice", "slice": meta, "raw_ref": "x", "raw_sha256": "y"}) + "\n" +
            json.dumps({"source": "dtcc-cftc-slices", "kind": "publication", "slice": meta, "record": r("A")}) + "\n")
        res = self.rec(recover_gaps=False, cumulative=[r("A")])
        ch = res["channels"]["CFTC_CR"]
        self.assertEqual((ch["tape_rows"]["verified"], ch["verdict"]["tape_parity"]), (0, "unverified"))
        self.assertEqual(ch["tape_parity"]["ids"], {"cumulative_only": 1})  # unverified rows are not counted as tape
        self.assertEqual(ch["unverified_view"]["ids"], {"matched": 1})  # visible, but labelled and outside the verdict

    # -- boundary 1: the verdict is bound to the audited rows of one snapshot ----------------------
    def _clean_tape(self):
        return [row for rows in self.captured().values() for row in rows]

    def test_verdict_uses_audited_rows_not_slice_filenames(self):
        self.build()
        tape = self._clean_tape()
        first = self.rec(recover_gaps=False, cumulative=tape)["channels"]["CFTC_CR"]
        self.assertEqual(first["verdict"]["tape_parity"], "complete")
        # A publication line that names a real captured slice but is not part of any audited commit.
        meta = {"fileName": f"CFTC_SLICE_CREDITS_{self.TAG}_1.zip", "sliceId": 5001}
        with (self.archive / "events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"source": "dtcc-cftc-slices", "kind": "publication", "slice": meta,
                                 "record": self.row("FORGED")}) + "\n")
        res = self.rec(recover_gaps=False, cumulative=tape + [self.row("FORGED")])
        ch = res["channels"]["CFTC_CR"]
        self.assertTrue(res["archive_audit"]["clean"])  # a line without a commit key is not an audit failure...
        self.assertEqual(ch["tape_rows"], {"logged": 11, "verified": 10, "unverified": 1})
        self.assertEqual(ch["verdict"]["tape_parity"], "unverified")  # ...but it is never counted as tape
        self.assertEqual(self.by_id(ch["tape_parity"])["FORGED"]["state"], "cumulative_only")
        self.assertEqual(ch["bound_to"]["tape_rows_sha256"], first["bound_to"]["tape_rows_sha256"])

    def test_rows_committed_after_the_snapshot_listing_are_excluded(self):
        from market_data.credit_tape import check_archive
        from market_data.credit_tape.synthetic_resilience import SyntheticDTCC
        self.build()
        tape = self._clean_tape()
        real_check = check_archive.check

        def late(day_tag, n):
            def write():  # the collector completes another slice while the audit is running
                dtcc = SyntheticDTCC()
                name = f"CFTC_SLICE_CREDITS_{day_tag}_{n}.zip"
                m = {"sliceId": 7000 + n, "fileName": name, "rowCount": 1, "dissemDTM": "2001-02-04T00:00:10Z",
                     "startTs": "2001-02-04T00:00:00Z", "endTs": "2001-02-04T00:00:09Z",
                     "fullFilePath": f"https://{slices.SLICE_HOST}/cftc/slices/{name}"}
                dtcc.files[m["fullFilePath"]] = self.zipped([self.row(f"LATE-{day_tag}")])
                dtcc.index = {"CFTC_CR": [m], "SEC_CR": []}
                dtcc.etag = f'"late-{day_tag}-{n}"'  # a changed index (the stored ETag would answer 304)
                capture.run(str(self.archive), ticker=False, slices=True, once=True, opener=dtcc)
                assert (self.archive / "dtcc-slices" / "done" / f"dtcc-cftc-slices__CFTC_CR__{7000 + n}.json").exists()
            return write

        def checked(day_tag, n):
            return lambda *a, **kw: real_check(*a, **{**kw, "_after_listing": late(day_tag, n)})

        with mock.patch.object(check_archive, "check", checked("2001_02_04", 1)):  # another day
            res = self.rec(recover_gaps=False, cumulative=tape)
        ch = res["channels"]["CFTC_CR"]
        self.assertTrue(res["archive_audit"]["clean"], res["archive_audit"])
        self.assertEqual(res["archive_audit"]["in_flight"]["commits_after_listing"],
                         ["dtcc-cftc-slices/CFTC_CR/7001"])
        self.assertEqual((ch["verdict"]["tape_parity"], ch["tape_rows"]["verified"]), ("complete", 10))
        with mock.patch.object(check_archive, "check", checked(self.TAG, 9)):  # same day, after the listing
            res = self.rec(recover_gaps=False, cumulative=tape)
        ch = res["channels"]["CFTC_CR"]
        self.assertTrue(res["archive_audit"]["clean"])
        self.assertEqual(ch["tape_rows"], {"logged": 11, "verified": 10, "unverified": 1})
        self.assertEqual(ch["verdict"]["tape_parity"], "unverified")
        self.assertNotIn(f"LATE-{self.TAG}", {u["id"] for u in ch["tape_parity"]["unmatched"]})  # never tape
        # A fresh audit after the commit completed now includes it as audited tape.
        res = self.rec(recover_gaps=False, cumulative=tape + [self.row(f"LATE-{self.TAG}")])
        self.assertEqual(res["channels"]["CFTC_CR"]["verdict"]["tape_parity"], "complete")
        snap = res["archive_audit"]["snapshot"]["logs"]["events.jsonl"]
        self.assertEqual(snap["bytes"], (self.archive / "events.jsonl").stat().st_size)

    # -- boundary 2: report-only failures stay in their own result ---------------------------------
    def test_report_only_decode_failure_cannot_downgrade_verified_tape(self):
        self.build()
        tape = self._clean_tape()
        bad = io.BytesIO()
        with zipfile.ZipFile(bad, "w") as z:
            z.writestr("X.csv", bytes([255, 254]) + b" not utf-8")
        res = self.rec(recover_gaps=True, cumulative=tape, gap={2: bad.getvalue()})
        ch = res["channels"]["CFTC_CR"]
        ro = ch["report_only"]
        self.assertEqual((ro["status"], [f["number"] for f in ro["decode_failures"]]), ("decode_failures", [2]))
        self.assertEqual(ch["verdict"]["tape_parity"], "complete")
        self.assertTrue(res["complete"])
        self.assertNotIn("with_report_only", ch)  # nothing decodable to add

    def test_cli_refuses_an_incomplete_day(self):
        from market_data.credit_tape import reconcile
        self.build()
        today = time.strftime("%Y-%m-%d", time.gmtime())
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            reconcile.main(["--archive", str(self.archive), "--day", today])

class RestoreTests(unittest.TestCase):
    """Offline tape restoration through the commit path (synthetic, disposable archives)."""

    def test_synthetic_restore_demo(self):
        from market_data.credit_tape import synthetic_restore
        with tempfile.TemporaryDirectory() as d:
            result = synthetic_restore.run(os.path.join(d, "archive"))
        self.assertTrue(result["all_pass"], json.dumps(result["checks"], indent=1))

    def test_interrupted_restore_resumes_or_stays_unresolved(self):
        from market_data.credit_tape import synthetic_restore_interrupt as sri
        result = sri.run()
        self.assertTrue(result["all_pass"], json.dumps([(r["scenario"], r["pass"]) for r in result["results"]]))
        by = {r["scenario"]: r for r in result["results"]}
        self.assertEqual(sorted(by), sorted(sri.SCENARIOS))
        for name in ("staged_missing", "staged_tampered"):
            mid = by[name]["after_second_restart"]
            self.assertEqual((mid["slice2_rows"], mid["s3_downloads_of_slice2"]), ([], 0))
            self.assertEqual(mid["restore_status"]["unresolved"], ["CFTC_CR:9102"])
        for r in result["results"]:
            final = r.get("after_rerun_and_restart") or r["after_second_restart"]
            self.assertNotIn("Q-REPLACEMENT", final["slice2_rows"])

    def _missed_slice2(self, d):
        from market_data.credit_tape import synthetic_restore as sr
        from market_data.credit_tape.synthetic_resilience import SyntheticDTCC
        archive = os.path.join(d, "archive")
        dtcc = SyntheticDTCC()
        for n in (1, 2):
            dtcc.files[sr.meta(n)["fullFilePath"]] = sr.zipped(sr.SLICES[n])
        dtcc.index = {"CFTC_CR": [sr.meta(1), sr.meta(2)], "SEC_CR": []}
        dtcc.fail.add(sr.meta(2)["fullFilePath"])
        capture.run(archive, ticker=False, slices=True, once=True, opener=dtcc)
        z = Path(d) / "saved" / sr.meta(2)["fileName"]
        z.parent.mkdir()
        z.write_bytes(sr.zipped(sr.SLICES[2]))
        return archive, z, sr

    def test_unrecorded_hash_stays_unresolved_unless_supplied(self):
        from market_data.credit_tape import restore
        with tempfile.TemporaryDirectory() as d:
            archive, z, sr = self._missed_slice2(d)
            before = Path(archive, "events.jsonl").read_bytes()
            res = restore.restore(archive, "CFTC_CR", str(z))
            self.assertEqual(res["status"], "unresolved")
            self.assertIn("no recorded or supplied sha256", res["reasons"][0])
            self.assertEqual(Path(archive, "events.jsonl").read_bytes(), before)  # nothing written
            self.assertEqual(restore.restore(archive, "CFTC_CR", str(z), sha256="0" * 64)["status"], "unresolved")
            ok = restore.restore(archive, "CFTC_CR", str(z), sha256=hashlib.sha256(z.read_bytes()).hexdigest())
            self.assertEqual(ok["status"], "restored")
            self.assertEqual(sorted(e["id"] for e in sr.pubs(archive, 2)), ["C", "R", "R"])

    def test_row_count_channel_and_archive_mismatches_stay_unresolved(self):
        from market_data.credit_tape import restore
        with tempfile.TemporaryDirectory() as d:
            archive, z, sr = self._missed_slice2(d)
            short = Path(d) / "short" / z.name
            short.parent.mkdir()
            short.write_bytes(sr.zipped(sr.SLICES[2][:2]))  # 2 rows, index entry says 3
            res = restore.restore(archive, "CFTC_CR", str(short), sha256=hashlib.sha256(short.read_bytes()).hexdigest())
            self.assertIn("index entry says 3", res["reasons"][0])
            self.assertEqual(restore.restore(archive, "SEC_CR", str(z))["status"], "unresolved")  # wrong channel
            legacy = Path(d) / "legacy"
            (legacy / "dtcc-slices").mkdir(parents=True)
            (legacy / "events.jsonl").write_text("")
            self.assertIn("not a credit-tape archive", restore.restore(str(legacy), "CFTC_CR", str(z))["reasons"][0])
            self.assertEqual(sr.pubs(archive, 2), [])


if __name__ == "__main__":
    unittest.main()
