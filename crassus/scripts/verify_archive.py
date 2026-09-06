#!/usr/bin/env python3
"""Durable archive failure-boundary tests; no network or credentials required."""
import io
import copy
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from crassus.archive import LedgerArchive, from_environment


class Exists(Exception):
    response = {"ResponseMetadata": {"HTTPStatusCode": 412}}


class Store:
    def __init__(self):
        self.objects = {}
        self.lose_response = False
        self.fail = False
        self.corrupt = False
        self.keys = []

    def put_object(self, **kwargs):
        if self.fail:
            raise RuntimeError("DO_NOT_LOG_SECRET")
        key = kwargs["Key"]
        self.keys.append(key)
        assert kwargs["IfNoneMatch"] == "*"
        if key in self.objects:
            raise Exists()
        self.objects[key] = kwargs
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError("DO_NOT_LOG_SECRET")

    def get_object(self, **kwargs):
        obj = self.objects[kwargs["Key"]]
        body = obj["Body"]
        return {"Body": io.BytesIO(b"x" * len(body) if self.corrupt else body),
                "ContentLength": len(body), "Metadata": obj["Metadata"]}


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.path = self.logs / "decisions-one.jsonl"
        self.path.write_bytes(b'{"n":1}\n')
        self.checkpoint = self.root / "state" / "archive" / "checkpoint.json"
        self.store = Store()
        self.archive = self.restart()

    def restart(self):
        return LedgerArchive(self.logs, self.checkpoint, self.store, "private")

    def test_restart_archives_all_runs_and_only_new_bytes(self):
        self.assertEqual(self.archive.tick(), 0)
        self.path.write_bytes(self.path.read_bytes() + b'{"n":2}\n')
        (self.logs / "decisions-two.jsonl").write_bytes(b'{"n":3}\n')
        self.assertEqual(self.restart().tick(), 0)
        self.assertEqual(len(self.store.objects), 3)
        self.restart().tick()
        self.assertEqual(len(self.store.keys), 3)

    def test_lost_response_freezes_range_across_append_restart(self):
        self.store.lose_response = True
        with self.assertRaises(TimeoutError):
            self.archive.tick()
        self.path.write_bytes(self.path.read_bytes() + b'{"n":2}\n')
        self.assertEqual(self.restart().tick(), 0)
        self.assertEqual(self.store.keys[0], self.store.keys[1])
        self.assertEqual(len(self.store.objects), 2)

    def test_failed_upload_preserves_offset_and_retries(self):
        self.store.fail = True
        with self.assertRaises(RuntimeError):
            self.archive.tick()
        self.assertEqual(json.loads(self.checkpoint.read_text())["files"][self.path.name]["offset"], 0)
        self.store.fail = False
        self.assertEqual(self.restart().tick(), 0)

    def test_remote_corruption_does_not_advance_checkpoint(self):
        self.store.corrupt = True
        with self.assertRaises(ValueError):
            self.archive.tick()
        self.assertEqual(self.archive.state["files"][self.path.name]["offset"], 0)
        self.store.corrupt = False
        self.assertEqual(self.restart().tick(), 0)

    def test_checkpoint_failure_after_upload_retries_same_object(self):
        original = self.archive._save
        calls = 0
        def save():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk full")
            original()
        with patch.object(self.archive, "_save", side_effect=save):
            with self.assertRaises(OSError):
                self.archive.tick()
        self.assertEqual(self.archive.state["files"][self.path.name]["offset"], 0)
        self.assertIsNone(self.archive.state["last_success_at"])
        self.assertEqual(self.archive.tick(), 0)
        self.assertEqual(len(set(self.store.keys)), 1)

    def test_pending_checkpoint_failure_prevents_network_even_on_retry(self):
        with patch.object(self.archive, "_save", side_effect=OSError("disk full")):
            for _ in range(2):
                with self.assertRaises(OSError):
                    self.archive.tick()
        self.assertEqual(self.store.keys, [])
        self.assertEqual(self.archive.tick(), 0)

    def test_partial_record_not_discarded(self):
        self.path.write_bytes(b'{"n":1}\n{"n":')
        self.assertEqual(self.archive.tick(), 5)
        self.path.write_bytes(self.path.read_bytes() + b'2}\n')
        self.assertEqual(self.restart().tick(), 0)
        self.assertEqual(b''.join(o["Body"] for o in self.store.objects.values()), self.path.read_bytes())

    def test_corrupt_and_oversized_records_fail_visibly(self):
        self.path.write_bytes(b'invalid\n')
        with self.assertRaises(ValueError):
            self.archive.tick()
        self.path.write_bytes(b'{"n":123}\n')
        with patch("crassus.archive.MAX_RECORD_BYTES", 4):
            with self.assertRaises(ValueError):
                self.archive.tick()
        self.assertEqual(self.store.keys, [])

    def test_truncation_and_missing_source_fail(self):
        self.archive.tick()
        self.path.write_bytes(b'')
        with self.assertRaises(ValueError):
            self.archive.tick()
        self.path.unlink()
        with self.assertRaises(ValueError):
            self.archive.tick()

    def test_poll_work_is_bounded(self):
        self.path.write_bytes(b'{"n":1}\n' * 20)
        with patch("crassus.archive.BATCH_BYTES", 8):
            self.assertGreater(self.archive.tick(), 0)
        self.assertEqual(len(self.store.objects), 4)

    def test_checkpoint_corruption_and_bucket_change_fail_closed(self):
        with self.assertRaises(ValueError):
            LedgerArchive(self.logs, self.checkpoint, self.store, "other")
        self.checkpoint.write_text('{broken')
        with self.assertRaises(ValueError):
            self.restart()

    def test_invalid_checkpoint_fields_fail_before_network(self):
        self.archive.tick()
        good = json.loads(self.checkpoint.read_text())
        cases = []
        for field, value in (("namespace", "../escape"), ("last_success_at", 3),
                             ("last_success_at", "not-a-date"), ("files", []), ("version", True)):
            state = copy.deepcopy(good)
            state[field] = value
            cases.append(state)
        for offset in (True, -1, "0"):
            state = copy.deepcopy(good)
            state["files"][self.path.name]["offset"] = offset
            cases.append(state)
        for end, digest in ((10**12, "a" * 64), (0, "a" * 64), (True, "a" * 64),
                            (20, "invalid")):
            state = copy.deepcopy(good)
            state["files"][self.path.name]["pending"] = {"end": end, "sha256": digest}
            cases.append(state)
        state = copy.deepcopy(good)
        state["files"]["../decisions-other.jsonl"] = {"offset": 0}
        cases.append(state)
        for state in cases:
            with self.subTest(state=state):
                self.checkpoint.write_text(json.dumps(state))
                with self.assertRaises(ValueError):
                    self.restart()
        self.assertEqual(len(self.store.keys), 1)

    def test_real_sdk_validates_conditional_put_and_readback(self):
        from botocore.stub import Stubber
        settings = {"CRASSUS_ARCHIVE_BUCKET": "private",
                    "CRASSUS_ARCHIVE_ACCOUNT_ID": "a" * 32,
                    "CRASSUS_ARCHIVE_ACCESS_KEY_ID": "fake-key",
                    "CRASSUS_ARCHIVE_SECRET_ACCESS_KEY": "fake-secret",
                    "CRASSUS_ARCHIVE_PRIVATE": "true"}
        with patch.dict(os.environ, settings, clear=True):
            archive = from_environment(self.logs, self.root / "sdk-state")
        body = self.path.read_bytes()
        import hashlib
        digest = hashlib.sha256(body).hexdigest()
        key = (f"crassus-ledger/v1/{archive.state['namespace']}/{self.path.name}/"
               f"{0:020d}-{len(body):020d}-{digest}.jsonl")
        metadata = {"sha256": digest, "start-byte": "0", "end-byte": str(len(body))}
        with Stubber(archive.client) as stub:
            stub.add_response("put_object", {}, {"Bucket": "private", "Key": key,
                              "Body": body, "ContentType": "application/x-ndjson",
                              "Metadata": metadata, "IfNoneMatch": "*"})
            stub.add_response("get_object", {"Body": io.BytesIO(body),
                              "ContentLength": len(body), "Metadata": metadata},
                              {"Bucket": "private", "Key": key})
            self.assertEqual(archive.tick(), 0)
            stub.assert_no_pending_responses()

    def test_main_closes_archive_on_interruption_or_runner_failure(self):
        from crassus.runner import main
        for once in (True, False):
            with self.subTest(once=once):
                runner, archive = Mock(), Mock()
                runner.run_cycle.return_value = False
                runner.run.side_effect = RuntimeError("runner failed")
                with patch("crassus.runner.Runner", return_value=runner), \
                     patch("crassus.runner.load_accounts", return_value=[]), \
                     patch("crassus.runner.configure_logging"), \
                     patch("crassus.runner.archive_from_environment", return_value=archive):
                    if once:
                        self.assertEqual(main(["--once"]), 130)
                    else:
                        with self.assertRaises(RuntimeError):
                            main([])
                archive.start.assert_called_once()
                archive.close.assert_called_once()

    def test_failure_logs_only_type(self):
        self.store.fail = True
        with patch.object(self.archive.stop, "wait", side_effect=lambda _: self.archive.stop.set()):
            with self.assertLogs("crassus.archive", logging.ERROR) as logs:
                self.archive._run()
        self.assertNotIn("DO_NOT_LOG_SECRET", ''.join(logs.output))
        self.assertIn("archive_failed", ''.join(logs.output))

    def test_disabled_or_incomplete_config(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(from_environment(self.logs, self.root / "state"))
        with patch.dict(os.environ, {"CRASSUS_ARCHIVE_BUCKET": "public"}, clear=True):
            with self.assertRaises(ValueError):
                from_environment(self.logs, self.root / "state")

    def test_data_root_covers_ledger_and_state_and_default_unchanged(self):
        code = ('from crassus.config import DEFAULT_LEDGER_DIR, DEFAULT_STATE_DIR, REPO_ROOT; '
                'print(DEFAULT_LEDGER_DIR); print(DEFAULT_STATE_DIR); print(REPO_ROOT)')
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
        env.pop("CRASSUS_DATA_ROOT", None)
        paths = subprocess.check_output([sys.executable, "-c", code], env=env, text=True).splitlines()
        self.assertEqual(paths[:2], [str(Path(paths[2]) / "logs"), str(Path(paths[2]) / "state")])
        env["CRASSUS_DATA_ROOT"] = str(self.root)
        paths = subprocess.check_output([sys.executable, "-c", code], env=env, text=True).splitlines()
        self.assertEqual(paths[:2], [str(self.root / "logs"), str(self.root / "state")])


if __name__ == "__main__":
    unittest.main()
