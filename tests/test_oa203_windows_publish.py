import csv
import gzip
import hashlib
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import market_data.options_scanner.windows as w  # noqa: E402
import market_data.options_scanner.windows_outputs as outputs  # noqa: E402
import market_data.options_scanner.windows_publish as pub  # noqa: E402
from market_data.options_scanner.returns import ContractInfo  # noqa: E402
from test_oa203_windows import SESSIONS, info, ob  # noqa: E402

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
DAYS = ["2026-10-05", "2026-10-06"]
KIND = "overnight"
WINDOW_ID = "overnight-2026-10-05_2026-10-06"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FakeBackend:
    """In-memory StorageBackend with corruption/truncation knobs for tests."""

    def __init__(self):
        self.objects: dict[str, list] = {}  # key -> [bytes, last_modified]
        self.puts: list[str] = []
        self.corrupt: set[str] = set()
        self.truncated = False

    def put(self, key, data, content_type):
        self.objects[key] = [bytes(data), NOW]
        self.puts.append(key)
        return {"sha256": sha(data), "bytes": len(data)}

    def get(self, key):
        if key not in self.objects:
            raise KeyError(key)
        if key in self.corrupt:
            return b"corrupted readback"
        return self.objects[key][0]

    def put_if_absent(self, key, data, content_type):
        if key in self.objects:
            return False
        self.objects[key] = [bytes(data), NOW]
        self.puts.append(key)
        return True

    def list_keys(self, prefix):
        if self.truncated:
            raise ValueError("fake truncated inventory")
        return [{"key": k, "size": len(v[0]), "last_modified": v[1]}
                for k, v in sorted(self.objects.items()) if k.startswith(prefix)]

    def delete(self, key):
        self.objects.pop(key, None)

    def set_mtime(self, key, when):
        self.objects[key][1] = when


def xlu_info():
    return ContractInfo(symbol="XLU261009C00040500", underlying="XLU", option_type="call",
                        strike=40.5, expiration="2026-10-09", root="XLU")


def overnight_rows():
    """Four contracts over Oct 5 -> Oct 6.

    XLU261009C00040500 reproduces the real Oct 5-6 replay's qualified
    rank 1 (entry 0.15, exit 0.48, pct 2.2); AAA is up, BBB is down, CCC
    expires before the exit and is excluded on every basis.
    """
    win = w.overnight_windows(SESSIONS[:2])[0]
    specs = [
        (xlu_info(), [ob("2026-10-05", 15, 55, 0.14, 0.15), ob("2026-10-06", 9, 35, 0.48, 0.49)]),
        (info(), [ob("2026-10-05", 15, 55, 1.7, 1.8), ob("2026-10-06", 9, 35, 2.4, 2.5)]),
        (info(symbol="BBB261009C00100000", underlying="BBB", root="BBB"),
         [ob("2026-10-05", 15, 55, 1.9, 2.0), ob("2026-10-06", 9, 35, 1.0, 1.1)]),
        (info(symbol="CCC261005C00100000", underlying="CCC", root="CCC", expiration="2026-10-05"),
         [ob("2026-10-05", 15, 55, 1.7, 1.8), ob("2026-10-06", 9, 35, 2.4, 2.5)]),
    ]
    return win, [w.evaluate(win, ci, path, DAYS) for ci, path in specs]


def write_fixture(root: Path, state="final", kind=KIND, window_id=WINDOW_ID,
                  sessions=SESSIONS[:2], days=DAYS, rows=None, win=None):
    """Write one window with the real producer; returns its directory."""
    if win is None:
        win = w.overnight_windows(sessions)[0]
    if rows is None:
        _, rows = overnight_rows()
    if state == "final":
        status = {d: {"final": True, "assessment": "complete", "sweeps": 6} for d in days}
    elif state == "partial":
        status = {d: {"final": True, "assessment": "complete", "sweeps": 6} for d in days}
        status[days[-1]] = {"final": True, "assessment": "partial", "sweeps": 6}
    else:  # in_progress
        status = {d: {"final": True, "assessment": "complete", "sweeps": 6} for d in days}
        status[days[-1]] = {"final": False, "assessment": "partial", "sweeps": 6}
    sources = [{"key": f"oa203/scanner/{d}/summary.json", "sha256": "a" * 64, "bytes": 123}
               for d in days]
    return outputs.write_window(root, win, rows, w.WindowPolicy(), status, sources,
                                "2026-10-06T20:00:00Z",
                                {"mode": "test", "underlyings": ["XLU", "ABC", "BBB", "CCC"]})


def weekly_fixture(root: Path):
    """A final weekly_cheapest window (hindsight label required)."""
    from test_oa203_windows import WEEK, week_path
    windows, _ = w.weekly_windows(SESSIONS)
    win = next(x for x in windows if x.kind == "weekly_cheapest")
    specs = [
        (info(), week_path()),
        (info(symbol="BBB261009C00100000", underlying="BBB", root="BBB"),
         week_path()[:-1] + [ob("2026-10-09", 15, 55, 0.9, 1.0)]),
        (info(symbol="CCC261006C00100000", underlying="CCC", root="CCC", expiration="2026-10-06"),
         week_path()),
    ]
    rows = [w.evaluate(win, ci, path, WEEK) for ci, path in specs]
    status = {d: {"final": True, "assessment": "complete", "sweeps": 6} for d in WEEK}
    sources = [{"key": f"oa203/scanner/{d}/summary.json", "sha256": "b" * 64, "bytes": 99}
               for d in WEEK]
    return outputs.write_window(root, win, rows, w.WindowPolicy(), status, sources,
                                "2026-10-09T20:00:00Z",
                                {"mode": "test", "underlyings": ["ABC", "BBB", "CCC"]})


def read_json(backend, key):
    return json.loads(backend.get(key).decode("utf-8"))


def read_csv(backend, key):
    return list(csv.DictReader(io.StringIO(backend.get(key).decode("utf-8"))))


class PublishFinal(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.root = self.tmp / "emit"
        self.dir = write_fixture(self.root)
        self.backend = FakeBackend()

    def tearDown(self):
        self._td.cleanup()

    def test_final_publishes_all_members_revision_and_pointer(self):
        result = pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["state"], "final")
        self.assertEqual(result["attempt"], 1)
        prefix = f"oa203/windows/oa203-windows-v1/{KIND}/{WINDOW_ID}"
        revision = "rev_" + sha((self.dir / "summary.json").read_bytes())
        self.assertEqual(result["revision"], revision)
        for name in pub.WINDOW_MEMBERS:
            digest = sha((self.dir / name).read_bytes())
            self.assertEqual(result["members"][name], digest)
            self.assertEqual(sha(self.backend.get(f"{prefix}/{revision}/{name}")), digest)
            self.assertEqual(sha(self.backend.get(f"{prefix}/{name}")), digest)
        pointer = read_json(self.backend, f"{prefix}/latest.json")
        self.assertEqual(pointer["revision"], revision)
        self.assertEqual(pointer["state"], "final")
        self.assertEqual(pointer["members"], result["members"])
        self.assertEqual(pointer["previous_published_at"], None)
        attempt = read_json(self.backend, f"{prefix}/latest-attempt.json")
        self.assertEqual((attempt["status"], attempt["revision"]), ("published", revision))
        self.assertIn(f"{prefix}/budget/{revision}-1.json", self.backend.objects)

    def test_real_replay_shape_qualified_rank_one(self):
        pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW)
        prefix = f"oa203/windows/oa203-windows-v1/{KIND}/{WINDOW_ID}"
        rows = read_csv(self.backend, f"{prefix}/rankings.csv")
        qualified = [r for r in rows if r["basis"] == "qualified"]
        self.assertEqual(qualified[0]["symbol"], "XLU261009C00040500")
        self.assertEqual(qualified[0]["rank"], "1")
        self.assertAlmostEqual(float(qualified[0]["pct"]), 2.2)
        self.assertEqual((float(qualified[0]["entry"]), float(qualified[0]["exit"])), (0.15, 0.48))
        summary = read_json(self.backend, f"{prefix}/summary.json")
        self.assertEqual(summary["counts"]["qualified"]["eligible"], 3)
        self.assertEqual(summary["counts"]["qualified"]["excluded"], 1)

    def test_partial_publishes_with_true_state_never_final(self):
        self.dir = write_fixture(self.root, state="partial")
        result = pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW)
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["state"], "partial")
        prefix = f"oa203/windows/oa203-windows-v1/{KIND}/{WINDOW_ID}"
        pointer = read_json(self.backend, f"{prefix}/latest.json")
        self.assertEqual(pointer["state"], "partial")
        self.assertEqual(pointer["state_reasons"], ["session_partial:2026-10-06"])
        summary = read_json(self.backend, f"{prefix}/summary.json")
        self.assertEqual(summary["state"], "partial")

    def test_weekly_cheapest_final_publishes_with_hindsight_label(self):
        self.dir = weekly_fixture(self.root)
        result = pub.publish_window(self.root, "weekly_cheapest", "weekly-2026-W41-cheapest",
                                    self.backend, now=NOW)
        self.assertEqual(result["status"], "published")
        prefix = "oa203/windows/oa203-windows-v1/weekly_cheapest/weekly-2026-W41-cheapest"
        summary = read_json(self.backend, f"{prefix}/summary.json")
        self.assertTrue(any("hindsight" in x.lower() for x in summary["labels"]))

    def test_retry_is_idempotent_same_revision_no_duplicate_upload(self):
        first = pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW)
        puts = len(self.backend.puts)
        second = pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW)
        self.assertEqual(second["status"], "already_published")
        self.assertEqual(second["revision"], first["revision"])
        self.assertEqual(len(self.backend.puts), puts)

    def test_dry_run_publishes_nothing(self):
        result = pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW,
                                    dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(self.backend.objects, {})
        self.assertEqual(result["budget"]["expired_objects"], 0)

    def test_local_filesystem_backend_roundtrip(self):
        store = self.tmp / "store"
        backend = pub.LocalFilesystemBackend(store)
        result = pub.publish_window(self.root, KIND, WINDOW_ID, backend, now=NOW)
        self.assertEqual(result["status"], "published")
        prefix = f"oa203/windows/oa203-windows-v1/{KIND}/{WINDOW_ID}"
        stored = store / prefix / "summary.json"
        self.assertEqual(sha(stored.read_bytes()), result["members"]["summary.json"])
        second = pub.publish_window(self.root, KIND, WINDOW_ID, backend, now=NOW)
        self.assertEqual(second["status"], "already_published")


class Refusals(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.root = self.tmp / "emit"
        self.dir = write_fixture(self.root)
        self.backend = FakeBackend()

    def tearDown(self):
        self._td.cleanup()

    def _edit_summary(self, mutate):
        path = self.dir / "summary.json"
        summary = json.loads(path.read_text(encoding="utf-8"))
        mutate(summary)
        path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    def test_in_progress_refused_nothing_uploaded(self):
        self.dir = write_fixture(self.root, state="in_progress")
        with self.assertRaisesRegex(ValueError, "in_progress"):
            pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW)
        self.assertEqual(self.backend.objects, {})

    def test_wrong_policy_refused(self):
        self._edit_summary(lambda s: s["policy"].__setitem__("version", "oa203-windows-v2"))
        with self.assertRaisesRegex(ValueError, "policy"):
            pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW)
        self.assertEqual(self.backend.objects, {})

    def test_kind_window_id_mismatch_refused(self):
        with self.assertRaisesRegex(ValueError, "does not match kind"):
            pub.publish_window(self.root, KIND, "weekly-2026-W41-cheapest", self.backend, now=NOW)
        self.assertEqual(self.backend.objects, {})

    def test_rank_gap_refused(self):
        path = self.dir / "rankings.csv"
        lines = path.read_bytes().decode("utf-8").splitlines()
        # Write bytes: Path.write_text would translate LF to CRLF on Windows.
        path.write_bytes(("\n".join(lines[:-1]) + "\n").encode("utf-8"))  # drop one ranked row
        with self.assertRaisesRegex(ValueError, "shortlist"):
            pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW)
        self.assertEqual(self.backend.objects, {})

    def test_crlf_rankings_accepted(self):
        # A CRLF rankings file is the same producer content; the publisher
        # reads it as LF rather than refusing on line endings.
        path = self.dir / "rankings.csv"
        text = path.read_bytes().decode("utf-8")
        path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        result = pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW)
        self.assertEqual(result["status"], "published")

    def test_count_mismatch_refused(self):
        self._edit_summary(lambda s: s["counts"]["qualified"].__setitem__("eligible", 4))
        with self.assertRaisesRegex(ValueError, "counts"):
            pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW)
        self.assertEqual(self.backend.objects, {})

    def test_missing_paths_refused_not_rankings_only(self):
        # The consumer tolerates a missing paths file at read time; the
        # publisher never creates that state.
        (self.dir / "paths.csv.gz").unlink()
        with self.assertRaisesRegex(ValueError, "missing member"):
            pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW)
        self.assertEqual(self.backend.objects, {})

    def test_conflicting_paths_attribution_refused(self):
        raw = gzip.decompress((self.dir / "paths.csv.gz").read_bytes()).decode("utf-8")
        lines = raw.splitlines()
        rows = list(csv.DictReader(io.StringIO(raw)))
        dup = dict(rows[0])
        dup["symbol"] = rows[1]["symbol"]  # two rows claim one symbol
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        writer.writerow(dup)
        (self.dir / "paths.csv.gz").write_bytes(
            gzip.compress(out.getvalue().encode("utf-8"), mtime=0))
        with self.assertRaisesRegex(ValueError, "conflicting attributions"):
            pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW)
        self.assertEqual(self.backend.objects, {})

    def test_hindsight_label_required_for_weekly_cheapest(self):
        self.dir = weekly_fixture(self.root)
        path = self.dir / "summary.json"
        summary = json.loads(path.read_text(encoding="utf-8"))
        summary["labels"] = ["Fixed-endpoint window."]
        path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outputs, labels or provenance"):
            pub.publish_window(self.root, "weekly_cheapest", "weekly-2026-W41-cheapest",
                               self.backend, now=NOW)
        self.assertEqual(self.backend.objects, {})

    def test_weekly_paths_stream_past_regular_limit_and_enforce_weekly_limit(self):
        directory = weekly_fixture(self.root)
        with mock.patch.object(pub, "MAX_PATHS_EXPANDED_BYTES", 1024):
            self.assertEqual(pub.load_window(directory).kind, "weekly_cheapest")
        with mock.patch.object(pub, "MAX_WEEKLY_PATHS_EXPANDED_BYTES", 1024):
            with self.assertRaisesRegex(ValueError, "expanded size ceiling"):
                pub.load_window(directory)

    def test_paths_expanded_ceiling_fails_closed(self):
        with mock.patch.object(pub, "MAX_PATHS_EXPANDED_BYTES", 1024):
            with self.assertRaisesRegex(ValueError, "expanded size ceiling"):
                pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW)
        self.assertEqual(self.backend.objects, {})


class ReadbackAndBudget(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.root = self.tmp / "emit"
        self.dir = write_fixture(self.root)
        self.backend = FakeBackend()

    def tearDown(self):
        self._td.cleanup()

    def test_readback_digest_mismatch_fails_closed_no_pointer(self):
        prefix = f"oa203/windows/oa203-windows-v1/{KIND}/{WINDOW_ID}"
        revision = "rev_" + sha((self.dir / "summary.json").read_bytes())
        self.backend.corrupt.add(f"{prefix}/{revision}/rankings.csv")
        with self.assertRaisesRegex(ValueError, "readback digest mismatch"):
            pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW)
        self.assertNotIn(f"{prefix}/latest.json", self.backend.objects)
        attempt = read_json(self.backend, f"{prefix}/latest-attempt.json")
        self.assertEqual(attempt["status"], "failed")
        # The bytes that were put are correct; only the readback lied.
        self.assertEqual(sha(self.backend.objects[f"{prefix}/{revision}/rankings.csv"][0]),
                         sha((self.dir / "rankings.csv").read_bytes()))

    def test_tampered_pointer_fails_closed(self):
        pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW)
        prefix = f"oa203/windows/oa203-windows-v1/{KIND}/{WINDOW_ID}"
        pointer = read_json(self.backend, f"{prefix}/latest.json")
        pointer["members"]["summary.json"] = "0" * 64
        self.backend.objects[f"{prefix}/latest.json"][0] = (
            json.dumps(pointer, indent=2, sort_keys=True) + "\n").encode()
        with self.assertRaisesRegex(ValueError, "pointer digest changed"):
            pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW)

    def test_budget_exhaustion_fails_closed(self):
        tiny = pub.WindowBudget(max_bytes=1024)
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW, budget=tiny)
        self.assertEqual(self.backend.objects, {})

    def test_truncated_inventory_fails_closed(self):
        self.backend.truncated = True
        with self.assertRaisesRegex(ValueError, "truncated inventory"):
            pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW)
        self.assertEqual(self.backend.objects, {})

    def test_unknown_key_in_inventory_fails_closed(self):
        prefix = f"oa203/windows/oa203-windows-v1/{KIND}/{WINDOW_ID}"
        self.backend.put(f"{prefix}/evil.json", b"{}", "application/json")
        with self.assertRaisesRegex(ValueError, "unrecognized window artifact"):
            pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW)

    def test_attempt_budget_exhausted(self):
        revision = "rev_" + sha((self.dir / "summary.json").read_bytes())
        prefix = f"oa203/windows/oa203-windows-v1/{KIND}/{WINDOW_ID}"
        for attempt in (1, 2, 3):
            self.backend.put(f"{prefix}/budget/{revision}-{attempt}.json", b"{}",
                             "application/json")
        with self.assertRaisesRegex(ValueError, "retry budget exhausted"):
            pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW)
        self.assertNotIn(f"{prefix}/latest.json", self.backend.objects)

    def test_retention_deletes_expired_windows(self):
        old_prefix = "oa203/windows/oa203-windows-v1/overnight/overnight-2026-10-01_2026-10-02"
        old = datetime(2026, 6, 1, tzinfo=timezone.utc)
        for key in (f"{old_prefix}/summary.json", f"{old_prefix}/rankings.csv",
                    f"{old_prefix}/paths.csv.gz", f"{old_prefix}/latest.json"):
            self.backend.put(key, b"x", "application/json")
            self.backend.set_mtime(key, old)
        result = pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW)
        self.assertEqual(result["status"], "published")
        for key in list(self.backend.objects):
            self.assertFalse(key.startswith(old_prefix + "/"), key)

    def test_revision_budget_exhausted(self):
        prefix = f"oa203/windows/oa203-windows-v1/{KIND}/{WINDOW_ID}"
        for i in range(16):
            rev = f"rev_{i:064x}"
            self.backend.put(f"{prefix}/{rev}/summary.json", b"{}", "application/json")
        with self.assertRaisesRegex(ValueError, "revision budget exhausted"):
            pub.publish_window(self.root, KIND, WINDOW_ID, self.backend, now=NOW)


if __name__ == "__main__":
    unittest.main()
