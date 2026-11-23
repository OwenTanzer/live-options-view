"""Exact-source guard for the F2 live-windows release.

Pins integrated source 044139d3 on production base3b6b8e1b and exactly
28 reviewed paths. Requires clean checkout and merged ancestry.
The prior Renaissance guard remains unchanged for rollback provenance.
"""

import argparse
import re
import subprocess
import sys

ACCEPTED_SOURCE_SHA = "044139d38f268d800c1c7e72c00f413ab199ece4"
REVIEWED_BASE_SHA = "3b6b8e1b5a978e85de39d7db42496ee0bd37e2a3"

# Exact reviewed production-base to integrated-source diff.
REVIEWED_PATHS = frozenset({
    '.github/scripts/market_mcp_f2_guard.py',
    '.github/scripts/test_market_mcp_f2_guard.py',
    '.github/workflows/publish-market-mcp-f2.yml',
    'docs/130-first-window-publication-plan.md',
    'docs/130-live-window-wiring.md',
    'docs/130-window-publisher.md',
    'docs/131-window-consumer.md',
    'docs/specs/OA203_ACTIVE_LOOKUP.md',
    'market_data/options_scanner/cli.py',
    'market_data/options_scanner/session.py',
    'market_data/options_scanner/windows_combine.py',
    'market_data/options_scanner/windows_live.py',
    'market_data/options_scanner/windows_outputs.py',
    'market_data/options_scanner/windows_publish.py',
    'market_data/options_scanner/windows_replay.py',
    'market_mcp/PRODUCTION.md',
    'market_mcp/README.md',
    'market_mcp/consumer.js',
    'market_mcp/parity.test.js',
    'market_mcp/source.js',
    'market_mcp/window.test.js',
    'tests/test_oa203_windows_combine.py',
    'tests/test_oa203_windows_live.py',
    'tests/test_oa203_windows_parity.py',
    'tests/test_oa203_windows_replay.py',
    'tests/test_oa203_windows_retention.py',
    'tests/test_oa203_windows_retry.py',
    'tests/test_oa203_windows_trigger.py',
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
