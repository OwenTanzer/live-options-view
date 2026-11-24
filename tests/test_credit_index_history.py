import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch, Mock

from market_data.credit_tape import index_history as history, restore, storage


class Store:
    def __init__(self):
        self.data = {}

    def upload_file(self, path, bucket, key):
        self.data[bucket, key] = Path(path).read_bytes()

    def download_file(self, bucket, key, path):
        Path(path).write_bytes(self.data[bucket, key])


class IndexHistoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.archive = Path(temp.name)
        self.root = self.archive / "dtcc-slices"
        self.folder = self.root / "index-history"
        self.path = self.folder / "2026-11-20.sqlite"
        self.store = Store()

    def sample(self, entries, n=0):
        return {"fetched_at": f"2026-11-20T20:00:{n:02d}+00:00", "etag": str(n),
                "index_sha256": str(n), "last_modified": None, "entries": entries}

    def test_revisions_repeats_removals_reordering_and_idempotent_retry(self):
        a = {"sliceId": 1, "fileName": "a"}
        b = {"sliceId": 2, "fileName": "b"}
        fixtures = [[a, b, a], [a, b, a], [b, a], [dict(a, rowCount=14)], [], None, "invalid"]
        expected = {}
        for i, rows in enumerate(fixtures):
            name = f"{i}.json"
            expected[name] = self.sample({"CFTC_CR": rows, "SEC_CR": []}, i)
            history.record(self.root, name, expected[name])
            history.record(self.root, name, expected[name])
        with self.assertRaises(ValueError):
            history.record(self.root, "0.json", self.sample({"CFTC_CR": [{"rolled": "back"}]}))
        self.assertEqual(dict(history.snapshots(self.path)), expected)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM versions").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM listings").fetchone()[0], 6)

    def test_verified_eviction_then_replay_with_legacy_files(self):
        entry = {"sliceId": 1, "fileName": "A.zip"}
        sample = self.sample({"CFTC_CR": [entry], "SEC_CR": []})
        history.record(self.root, "recent.json", sample)
        legacy = self.root / "index" / "2026-11-19" / "legacy.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps(sample))
        with patch.object(history, "s3", return_value=self.store):
            history.rotate(self.folder, "2026-11-20", "private", "archive-id")
            self.assertTrue(self.path.exists())
            history.rotate(self.folder, "2026-11-21", "private", "archive-id")
            self.assertFalse(self.path.exists())
            self.assertEqual(dict(history.partitions(self.root)), {"recent.json": sample})
            rows = restore.index_entries(str(self.archive), "CFTC_CR", "A.zip")
            self.assertEqual([r["sliceId"] for r in rows], [1, 1])
            with self.assertRaises(ValueError):
                history.record(self.root, "late.json", sample)
            self.store.data[next(iter(self.store.data))] = b"bad cold read"
            with self.assertRaises(ValueError):
                list(history.partitions(self.root))

    def test_corrupt_upload_leaves_authoritative_local_copy(self):
        value = self.sample({"CFTC_CR": []})
        history.record(self.root, "one", value)
        with patch.object(self.store, "download_file", side_effect=lambda b, k, p: Path(p).write_bytes(b"bad")):
            with self.assertRaises(ValueError):
                history.seal(self.path, self.store, "private", "a")
        self.assertTrue(self.path.exists())
        self.assertFalse(self.path.with_suffix(".remote").exists())
        self.assertEqual(dict(history.snapshots(self.path)), {"one": value})

    def test_headroom_thresholds_and_zero(self):
        for free, warn, pause in [(0, True, True), (499999999, True, True),
                                  (500000000, True, False), (1499999999, True, False),
                                  (1500000000, False, False)]:
            with patch.object(storage.shutil, "disk_usage", return_value=Mock(free=free)):
                self.assertEqual(storage.status(self.root), {
                    "available_bytes": free, "low_space": warn, "pause_writers": pause})

    def test_full_volume_serves_login_and_starts_no_capture_writers(self):
        import threading
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        from market_data.credit_tape import service, volume
        root = self.archive / "tape"
        root.mkdir()
        volume.ensure_identity(str(root))
        env = {"CREDIT_TAPE_ARCHIVE": str(root), "CREDIT_TAPE_VOLUME": str(self.archive),
               "CREDIT_TAPE_PUBLIC_ORIGIN": "https://credit.test", "CREDIT_TAPE_GITHUB_CLIENT_ID": "fixture",
               "CREDIT_TAPE_GITHUB_CLIENT_SECRET": "fixture", "CREDIT_TAPE_GITHUB_ALLOWED_IDS": "42", "PORT": "0"}
        ready = threading.Event()
        server = []
        handlers = {}
        results = []

        def listen(addr, handler):
            server.append(ThreadingHTTPServer(("127.0.0.1", 0), handler))
            ready.set()
            return server[0]

        with patch.object(service, "ThreadingHTTPServer", side_effect=listen), \
                patch.object(service.signal, "signal", side_effect=handlers.__setitem__), \
                patch.object(storage.shutil, "disk_usage", return_value=Mock(free=0)), \
                patch.object(service.capture, "run") as dtcc, patch.object(service.treu, "run") as treu:
            worker = threading.Thread(target=lambda: results.append(service.main(env, is_mount=lambda _: True)))
            worker.start()
            try:
                self.assertTrue(ready.wait(5))
                for path, code in [("/healthz/collector", 503), ("/auth/login", 302), ("/api/trades", 401)]:
                    con = HTTPConnection("127.0.0.1", server[0].server_port, timeout=5)
                    try:
                        con.request("GET", path, headers={"Host": "credit.test"})
                        resp = con.getresponse()
                        self.assertEqual(resp.status, code)
                        if path == "/auth/login":
                            self.assertTrue(resp.getheader("Location").startswith("https://github.com/login/oauth/authorize?"))
                        if path == "/healthz/collector":
                            self.assertEqual(json.loads(resp.read())["disk"]["available_bytes"], 0)
                    finally:
                        con.close()
            finally:
                if handlers:
                    handlers[service.signal.SIGTERM]()
                worker.join(8)
            self.assertEqual(results, [0])
            dtcc.assert_not_called()
            treu.assert_not_called()
