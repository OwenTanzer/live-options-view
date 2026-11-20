"""OA-242 credit tape: synthetic fixtures only (repository is public)."""
import gzip
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from market_data.credit_tape import capture
from market_data.credit_tape import serve_private as sp


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
