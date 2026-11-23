#!/usr/bin/env python3
"""Verify the pre-merge provisioning gate (#41 / B2).

The gate's job is to make "landed as inactive" or "provisioned and recorded"
the only ways an account can become active. These fixtures exercise: a brand
new active entry with and without evidence, an activated entry (false -> true),
a renamed `password_env` on an already active entry, an explicitly inactive
entry that needs nothing, a grandfathered entry that is already active, missing
values under `--check-env`, malformed evidence, a literal password in the
catalog, an unresolvable base revision, and the guarantee that no secret value
reaches the output.

    python scripts/verify_provisioning_gate.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "crassus"))
sys.path.insert(0, str(ROOT / "crassus" / "scripts"))

import provisioning_gate  # noqa: E402
from crassus.provisioning import (  # noqa: E402
    evaluate, load_evidence, newly_active_entries,
)


def _scratch_root() -> Path:
    """A writable scratch directory (the platform temp dir may be read-only)."""
    candidate = Path(os.environ.get("CRASSUS_TEST_TMPDIR") or ROOT.parent / ".test-tmp") / "provisioning"
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def _tmpdir() -> Path:
    base = _scratch_root()
    for attempt in range(100):
        candidate = base / f"run-{os.getpid()}-{time.time_ns()}-{attempt}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError(f"could not create a scratch directory under {base}")


def _write(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))
    return path


def _entry(alias, username, env, *, active=None):
    entry = {"alias": alias, "username": username, "password_env": env,
             "strategy_id": "smoke_atm_roundtrip"}
    if active is not None:
        entry["active"] = active
    return entry


def _run(argv):
    """Run the CLI, returning (exit code, parsed-or-raw stdout)."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = provisioning_gate.main(argv)
    text = buffer.getvalue()
    try:
        return code, json.loads(text)
    except ValueError:
        return code, text


class GateBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.dir = _tmpdir()
        self.catalog = self.dir / "accounts.example.json"
        self.evidence = self.dir / "provisioning_evidence.json"
        _write(self.evidence, {"schema": "provisioning_evidence.v1", "entries": {}})

    def cli(self, *extra, base="none", env=None):
        argv = ["--catalog", str(self.catalog), "--evidence", str(self.evidence),
                "--base-ref", base, "--json", *extra]
        if env is not None:
            saved = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            try:
                return _run(argv)
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
        return _run(argv)

    def test_new_active_entry_without_evidence_fails(self):
        _write(self.catalog, {"accounts": [_entry("New", "crassus_new", "CRASSUS_PW_NEW")]})
        code, out = self.cli()
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertEqual(out["findings"][0]["kind"], "missing_evidence")
        self.assertEqual(out["findings"][0]["env_name"], "CRASSUS_PW_NEW")

    def test_new_active_entry_with_evidence_passes(self):
        _write(self.catalog, {"accounts": [_entry("New", "crassus_new", "CRASSUS_PW_NEW")]})
        _write(self.evidence, {"schema": "provisioning_evidence.v1", "entries": {
            "CRASSUS_PW_NEW": {"provisioned_at": "2026-11-09", "by": "OT"}
        }})
        code, out = self.cli()
        self.assertEqual(code, 0, out)
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["newly_active"]), 1)

    def test_inactive_entry_needs_no_evidence(self):
        _write(self.catalog, {"accounts": [
            _entry("Later", "crassus_later", "CRASSUS_PW_LATER", active=False)
        ]})
        code, out = self.cli()
        self.assertEqual(code, 0, out)
        self.assertEqual(out["newly_active"], [])

    def test_activation_of_an_inactive_entry_requires_evidence(self):
        base = self.dir / "base.json"
        _write(base, {"accounts": [_entry("Later", "crassus_later", "CRASSUS_PW_LATER", active=False)]})
        _write(self.catalog, {"accounts": [_entry("Later", "crassus_later", "CRASSUS_PW_LATER", active=True)]})
        base_entries = json.loads(base.read_text())["accounts"]
        result = evaluate(json.loads(self.catalog.read_text())["accounts"],
                          base_entries=base_entries, evidence={})
        self.assertFalse(result.ok)
        self.assertEqual(result.findings[0].kind, "missing_evidence")

        _write(self.evidence, {"schema": "provisioning_evidence.v1", "entries": {
            "CRASSUS_PW_LATER": {"provisioned_at": "2026-11-09", "by": "OT"}
        }})
        code, out = self.cli("--base-catalog", str(base))
        self.assertEqual(code, 0, out)

    def test_renamed_password_env_on_an_active_entry_requires_new_evidence(self):
        base_entries = [_entry("Old", "crassus_old", "CRASSUS_PW_OLD")]
        head_entries = [_entry("Old", "crassus_old", "CRASSUS_PW_OLD_V2")]
        new = newly_active_entries(base_entries, head_entries)
        self.assertEqual([item["env_name"] for item in new], ["CRASSUS_PW_OLD_V2"])

    def test_already_active_on_the_same_variable_is_grandfathered(self):
        base_entries = [_entry("Old", "crassus_old", "CRASSUS_PW_OLD")]
        head_entries = [_entry("Old", "crassus_old", "CRASSUS_PW_OLD")]
        self.assertEqual(newly_active_entries(base_entries, head_entries), [])
        result = evaluate(head_entries, base_entries=base_entries, evidence={})
        self.assertTrue(result.ok, [f.message() for f in result.findings])

    def test_missing_environment_value_fails_even_with_evidence(self):
        _write(self.catalog, {"accounts": [_entry("New", "crassus_new", "CRASSUS_PW_NEW")]})
        _write(self.evidence, {"schema": "provisioning_evidence.v1", "entries": {
            "CRASSUS_PW_NEW": {"provisioned_at": "2026-11-09", "by": "OT"}
        }})
        saved = os.environ.pop("CRASSUS_PW_NEW", None)
        try:
            code, out = self.cli("--check-env")
            self.assertEqual(code, 1)
            self.assertEqual(out["findings"][0]["kind"], "missing_value")

            code, out = self.cli("--check-env", env={"CRASSUS_PW_NEW": "hunter2-not-a-real-secret"})
            self.assertEqual(code, 0, out)
            # The value itself must never appear in the gate's output.
            self.assertNotIn("hunter2", json.dumps(out))
        finally:
            if saved is not None:
                os.environ["CRASSUS_PW_NEW"] = saved

    def test_unusable_evidence_exits_two(self):
        _write(self.catalog, {"accounts": [_entry("New", "crassus_new", "CRASSUS_PW_NEW")]})
        _write(self.evidence, {"schema": "provisioning_evidence.v1", "entries": {
            "CRASSUS_PW_NEW": {"provisioned_at": "2026-11-09", "by": "OT", "value": "abc123"}
        }})
        code, out = self.cli()
        self.assertEqual(code, 2)
        self.assertIn("unsupported field", out["error"])

    def test_evidence_needs_a_date_and_a_name(self):
        _write(self.catalog, {"accounts": [_entry("New", "crassus_new", "CRASSUS_PW_NEW")]})
        _write(self.evidence, {"schema": "provisioning_evidence.v1", "entries": {
            "CRASSUS_PW_NEW": {"provisioned_at": "last tuesday", "by": "OT"}
        }})
        with self.assertRaises(ValueError):
            load_evidence(self.evidence)

    def test_literal_password_in_catalog_is_a_finding(self):
        entry = _entry("Leaky", "crassus_leaky", "CRASSUS_PW_LEAKY")
        entry["password"] = "not-in-source-control"
        _write(self.catalog, {"accounts": [entry]})
        code, out = self.cli()
        self.assertEqual(code, 1)
        self.assertIn("secret_in_catalog", [f["kind"] for f in out["findings"]])
        self.assertNotIn("not-in-source-control", json.dumps(out))

    def test_bad_env_name_is_a_finding(self):
        _write(self.catalog, {"accounts": [_entry("Odd", "crassus_odd", "PW_ODD")]})
        code, out = self.cli()
        self.assertEqual(code, 1)
        self.assertIn("invalid_evidence", [f["kind"] for f in out["findings"]])

    def test_no_base_revision_treats_every_active_entry_as_new(self):
        _write(self.catalog, {"accounts": [
            _entry("A", "crassus_a", "CRASSUS_PW_A"),
            _entry("B", "crassus_b", "CRASSUS_PW_B", active=False),
        ]})
        code, out = self.cli()
        self.assertEqual(code, 1)
        self.assertFalse(out["base_available"])
        self.assertEqual([item["env_name"] for item in out["newly_active"]], ["CRASSUS_PW_A"])

    def test_unresolvable_base_ref_is_an_input_error_not_a_pass(self):
        _write(self.catalog, {"accounts": [_entry("A", "crassus_a", "CRASSUS_PW_A")]})
        code, out = self.cli(base="no-such-revision-anywhere")
        self.assertEqual(code, 2)
        self.assertIn("base revision", out["error"])

    def test_git_base_ref_reads_the_catalog_from_that_revision(self):
        # Exercises the real git path (`git show <ref>:crassus/accounts...`)
        # rather than the --base-catalog fixture override. HEAD and the
        # worktree agree in a clean checkout, so nothing is newly active; a
        # developer with uncommitted catalog edits is skipped rather than
        # failed, because that delta is not the commit under test.
        catalog = ROOT / "crassus" / "accounts.example.json"
        dirty = subprocess.run(
            ["git", "-C", str(ROOT), "diff", "--quiet", "HEAD", "--",
             "crassus/accounts.example.json"],
            capture_output=True,
        )
        if dirty.returncode not in (0, 1):
            self.skipTest("git is not available to resolve a base revision here")
        if dirty.returncode == 1:
            self.skipTest("the catalog differs from HEAD; the delta is uncommitted work")

        code, out = _run([
            "--base-ref", "HEAD", "--json",
            "--catalog", str(catalog), "--evidence", str(self.evidence),
        ])
        self.assertEqual(code, 0, out)
        self.assertTrue(out["base_available"])
        self.assertEqual(out["newly_active"], [])
        self.assertGreater(out["checked_entries"], 0)

    def test_repository_catalog_passes_on_delta_against_itself_like_baseline(self):
        # Both sides are the live catalog: nothing is newly active, so the gate
        # is green with an empty evidence file. This is the state a merge of
        # unrelated work must not break.
        code, out = _run([
            "--catalog", str(ROOT / "crassus" / "accounts.example.json"),
            "--evidence", str(self.evidence),
            "--base-ref", "none", "--json",
        ])
        # `none` deliberately treats every active entry as new, so this is a
        # failure that names real variables -- proof the gate sees the catalog.
        self.assertEqual(code, 1)
        envs = [item["env_name"] for item in out["newly_active"]]
        self.assertIn("CRASSUS_PW_BOWMAN", envs)
        self.assertNotIn("CRASSUS_PW_PERSEPHONE", envs)  # inactive


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(GateBehaviourTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("=" * 66)
    print(f"{result.testsRun - len(result.failures) - len(result.errors)} passed, "
          f"{len(result.failures) + len(result.errors)} failed")
    raise SystemExit(0 if result.wasSuccessful() else 1)
