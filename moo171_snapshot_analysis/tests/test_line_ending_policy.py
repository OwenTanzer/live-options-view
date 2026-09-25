"""Portability of the frozen-run identity across checkouts.

The frozen manifest records source files (and reproduction_check records the
manifest itself) as raw-byte sha256. Those identities are only reproducible
if every checkout materializes the same bytes git stores -- which
.gitattributes (eol=lf) enforces. These tests check the COMMITTED evidence
against the COMMITTED blobs, i.e. exactly what a fresh checkout on any
platform sees, rather than creating and verifying a manifest on the same
working tree.

After any change to a module *.py file, a new frozen run
(`run_audit.py --new-run`) must be committed alongside it, or
test_committed_manifest_source_identity_matches_fresh_checkout fails.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import run_audit
from r2_source import ManifestMismatch

MODULE_DIR = Path(__file__).resolve().parents[1]


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=MODULE_DIR, capture_output=True, check=True).stdout


@pytest.fixture(scope="module")
def head_blob():
    try:
        prefix = _git("rev-parse", "--show-prefix").decode().strip()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")

    def read(relpath: str) -> bytes:
        return _git("show", f"HEAD:{prefix}{relpath}")

    return read


def test_committed_manifest_source_identity_matches_fresh_checkout(head_blob):
    manifest = json.loads(head_blob("out/source_manifest.json"))
    committed_py = sorted(
        Path(p).name for p in _git("ls-tree", "--name-only", "HEAD", "./").decode().split()
        if p.endswith(".py")
    )
    assert sorted(manifest["source_file_sha256"]) == committed_py
    for name, frozen_sha in manifest["source_file_sha256"].items():
        assert hashlib.sha256(head_blob(name)).hexdigest() == frozen_sha, name


def test_reproduction_check_identifies_the_committed_manifest_bytes(head_blob):
    check = json.loads(head_blob("out/reproduction_check.json"))
    manifest_blob = head_blob("out/source_manifest.json")
    assert check["frozen_manifest_sha256"] == hashlib.sha256(manifest_blob).hexdigest()


def test_tracked_text_files_are_lf_in_this_checkout():
    try:
        tracked = _git("ls-files", "--", ".").decode().split()  # paths relative to MODULE_DIR
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    text_suffixes = {".py", ".md", ".json", ".csv", ".txt"}
    crlf = [p for p in tracked if Path(p).suffix in text_suffixes and b"\r" in (MODULE_DIR / p).read_bytes()]
    assert crlf == []


def test_gitattributes_forces_lf_and_keeps_binaries_binary():
    attrs = _git("check-attr", "eol", "text", "--", "run_audit.py", "out/source_manifest.json",
                 "out/predictor_comparison.csv", "out/option_rows.parquet").decode()
    assert "run_audit.py: eol: lf" in attrs
    assert "out/source_manifest.json: eol: lf" in attrs
    assert "out/predictor_comparison.csv: eol: lf" in attrs
    assert "out/option_rows.parquet: text: unset" in attrs


def test_source_file_sha256_refuses_crlf_source(tmp_path, monkeypatch):
    (tmp_path / "ok.py").write_bytes(b"x = 1\n")
    (tmp_path / "bad.py").write_bytes(b"x = 1\r\n")
    monkeypatch.setattr(run_audit, "MODULE_DIR", tmp_path)
    with pytest.raises(ManifestMismatch, match="bad.py"):
        run_audit._source_file_sha256()
