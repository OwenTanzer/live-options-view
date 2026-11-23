"""Fail-closed release guard for the reviewed Renaissance market MCP image.

A Renaissance image may only be built from the exact accepted commit
c8fcf529698bc57b930495f8c0272b5c1b16b2f4, an ancestor of master, checked out
cleanly, whose cumulative diff against the reviewed base 014b48ad contains
EXACTLY the reviewed paths below -- no more, no less. This prevents a
dispatch from publishing an arbitrary master commit, a producer-only or
unreviewed commit, a dirty tree, or a commit that drops a reviewed file.

Unlike market_mcp_source_guard.py, which checks one commit against its
first parent, this guard checks the whole reviewed branch (three F2
commits plus Steve's UTC-fixture cherry-pick) against its base, because
the release is the branch, not one commit. The allowed set is a constant
in this file on purpose: there is no --allow override, so a dispatch
cannot weaken the list. A future release needs a new pinned guard, not
an edit to this one's constants.
"""

import argparse
import re
import subprocess
import sys

ACCEPTED_SOURCE_SHA = "c8fcf529698bc57b930495f8c0272b5c1b16b2f4"
REVIEWED_BASE_SHA = "014b48ad9707039871b784d42499602fd899ee74"

# Exact cumulative diff of 014b48ad..c8fcf529 (22 files, verified with
# `git diff --name-only 014b48ad c8fcf529`): F2 consumer (8dc2dc85),
# F2 publisher (a278c1f0), parity + concurrency (dbf9c6ac), Steve's UTC
# market-readings fixture cherry-pick (c8fcf529).
REVIEWED_PATHS = frozenset({
    "docs/130-window-publisher.md",
    "docs/131-window-consumer.md",
    "market_data/options_scanner/windows_publish.py",
    "market_mcp/README.md",
    "market_mcp/consumer.js",
    "market_mcp/consumer.test.js",
    "market_mcp/container-smoke.js",
    "market_mcp/http.test.js",
    "market_mcp/overview.test.js",
    "market_mcp/parity.test.js",
    "market_mcp/production-smoke.js",
    "market_mcp/production.test.js",
    "market_mcp/server.js",
    "market_mcp/smoke.js",
    "market_mcp/source.js",
    "market_mcp/spy.test.js",
    "market_mcp/window.test.js",
    "market_mcp/window_fixtures.js",
    "tests/test_market_readings.py",
    "tests/test_oa203_windows_concurrency.py",
    "tests/test_oa203_windows_parity.py",
    "tests/test_oa203_windows_publish.py",
})

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class GuardError(RuntimeError):
    pass


def _git(repo: str, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", repo, *args],
            check=False, capture_output=True, text=True,
        )
    except OSError as exc:
        raise GuardError(f"git unavailable: {type(exc).__name__}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise GuardError(f"git {' '.join(args)} failed: {detail[-1] if detail else 'unknown'}")
    return completed.stdout.strip()


def _is_ancestor(repo: str, sha: str, ref: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", repo, "merge-base", "--is-ancestor", sha, ref],
        check=False, capture_output=True, text=True,
    )
    return completed.returncode == 0


def validate_release(repo: str, sha: str, base: str,
                     master_ref: str = "FETCH_HEAD",
                     accepted: str = ACCEPTED_SOURCE_SHA,
                     reviewed_base: str = REVIEWED_BASE_SHA,
                     allowed: frozenset = REVIEWED_PATHS) -> list[str]:
    """Return the changed paths only when every release guard passes.

    `accepted`, `reviewed_base` and `allowed` default to the pinned
    release constants; the tests pass temp-repo values so the checks are
    exercisable without the real SHAs. The CLI exposes no override for
    them, so a dispatch cannot weaken the list.
    """
    if not SHA_RE.fullmatch(sha or ""):
        raise GuardError("source SHA must be a 40-character lowercase commit id")
    if sha != accepted:
        raise GuardError(f"source {sha} is not the accepted Renaissance commit {accepted}")
    if not SHA_RE.fullmatch(base or ""):
        raise GuardError("base SHA must be a 40-character lowercase commit id")
    if base != reviewed_base:
        raise GuardError(f"base {base} is not the reviewed base {reviewed_base}")

    head = _git(repo, "rev-parse", "HEAD")
    if head != sha:
        raise GuardError(f"checkout HEAD {head} does not match requested source {sha}")

    dirty = _git(repo, "status", "--porcelain")
    if dirty:
        raise GuardError("source checkout is not clean")

    if not _is_ancestor(repo, sha, master_ref):
        raise GuardError(f"source {sha} is not an ancestor of {master_ref}")
    if not _is_ancestor(repo, base, sha):
        raise GuardError(f"reviewed base {base} is not an ancestor of source {sha}")

    changed = [line for line in _git(repo, "diff", "--name-only", base, sha).splitlines() if line]
    if not changed:
        raise GuardError("source has no changes against the reviewed base")
    changed_set = set(changed)
    unexpected = sorted(changed_set - allowed)
    if unexpected:
        raise GuardError("source contains unreviewed paths: " + ", ".join(unexpected))
    missing = sorted(allowed - changed_set)
    if missing:
        raise GuardError("source is missing reviewed paths: " + ", ".join(missing))
    return sorted(changed)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--master-ref", default="FETCH_HEAD")
    args = parser.parse_args()
    try:
        changed = validate_release(args.repo, args.sha, args.base, args.master_ref)
    except GuardError as exc:
        print(f"Market MCP Renaissance release blocked: {exc}", file=sys.stderr)
        return 1
    print(f"Reviewed Renaissance source accepted: {args.sha}")
    for path in changed:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
