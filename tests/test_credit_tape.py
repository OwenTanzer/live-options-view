"""OA-242 credit tape: synthetic fixtures only (repository is public)."""
import gzip
import io
import json
import os
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
        self.assertEqual(sorted(r.pending), [1001, 1002])  # index poll only enqueues
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
        self.assertEqual(r.pending[1002]["attempts"], 1)
        http.fail.clear()
        r.drain_live(self.ev, self.al)  # not yet due
        self.assertIn(1002, r.pending)
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


if __name__ == "__main__":
    unittest.main()
