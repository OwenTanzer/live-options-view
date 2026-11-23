"""Fail-closed checks for the reviewed Renaissance release guard.

The guard's pinned constants (ACCEPTED_SOURCE_SHA, REVIEWED_BASE_SHA,
REVIEWED_PATHS) are the defaults of validate_release; the tests pass
temp-repo SHAs as `accepted`/`reviewed_base` so every check is
exercisable without the real SHAs. The CLI exposes no override for
those parameters, so a dispatch cannot weaken them.
"""

import os
import subprocess
import tempfile
import unittest

from market_mcp_release_guard import (
    ACCEPTED_SOURCE_SHA,
    REVIEWED_BASE_SHA,
    REVIEWED_PATHS,
    GuardError,
    validate_release,
)


class Repo:
    def __init__(self, root):
        self.root = root
        self.git("init", "-b", "master")
        self.git("config", "user.name", "Guard Test")
        self.git("config", "user.email", "guard@example.invalid")

    def git(self, *args):
        completed = subprocess.run(
            ["git", "-C", self.root, *args],
            check=True, capture_output=True, text=True,
        )
        return completed.stdout.strip()

    def write(self, path, text):
        target = os.path.join(self.root, path)
        if os.path.dirname(target):
            os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")


def accepted_repo():
    """A repo whose base..HEAD diff is exactly the reviewed path set."""
    temp = tempfile.TemporaryDirectory()
    repo = Repo(temp.name)
    repo.write("README.md", "base\n")
    base = repo.commit("base")
    for path in sorted(REVIEWED_PATHS):
        repo.write(path, "reviewed\n")
    sha = repo.commit("renaissance")
    return temp, repo, base, sha


def check(repo, sha, base, master_ref="master"):
    return validate_release(repo.root, sha, base, master_ref,
                            accepted=sha, reviewed_base=base)


class ReleaseGuardTests(unittest.TestCase):
    def test_exact_reviewed_tree_passes(self):
        temp, repo, base, sha = accepted_repo()
        with temp:
            changed = check(repo, sha, base)
            self.assertEqual(changed, sorted(REVIEWED_PATHS))

    def test_wrong_source_sha_blocks(self):
        temp, repo, base, sha = accepted_repo()
        with temp:
            with self.assertRaisesRegex(GuardError, "not the accepted Renaissance commit"):
                validate_release(repo.root, "0" * 40, base, "master",
                                 accepted=sha, reviewed_base=base)

    def test_wrong_base_blocks(self):
        temp, repo, base, sha = accepted_repo()
        with temp:
            with self.assertRaisesRegex(GuardError, "not the reviewed base"):
                validate_release(repo.root, sha, "0" * 40, "master",
                                 accepted=sha, reviewed_base=base)

    def test_unreviewed_path_blocks(self):
        temp = tempfile.TemporaryDirectory()
        with temp:
            repo = Repo(temp.name)
            repo.write("README.md", "base\n")
            base = repo.commit("base")
            for path in sorted(REVIEWED_PATHS):
                repo.write(path, "reviewed\n")
            repo.write("market_data/live_market/price_lane.py", "producer\n")
            sha = repo.commit("mixed")
            with self.assertRaisesRegex(GuardError, "unreviewed paths"):
                check(repo, sha, base)

    def test_missing_reviewed_path_blocks(self):
        temp = tempfile.TemporaryDirectory()
        with temp:
            repo = Repo(temp.name)
            repo.write("README.md", "base\n")
            base = repo.commit("base")
            paths = sorted(REVIEWED_PATHS)
            for path in paths[:-1]:
                repo.write(path, "reviewed\n")
            sha = repo.commit("incomplete")
            with self.assertRaisesRegex(GuardError, "missing reviewed paths"):
                check(repo, sha, base)

    def test_dirty_tree_blocks(self):
        temp, repo, base, sha = accepted_repo()
        with temp:
            repo.write("market_mcp/server.js", "dirty\n")
            with self.assertRaisesRegex(GuardError, "not clean"):
                check(repo, sha, base)

    def test_head_mismatch_blocks(self):
        temp, repo, base, sha = accepted_repo()
        with temp:
            repo.git("checkout", base)
            with self.assertRaisesRegex(GuardError, "does not match"):
                check(repo, sha, base)

    def test_non_ancestor_of_master_blocks(self):
        temp, repo, base, sha = accepted_repo()
        with temp:
            repo.git("checkout", "-b", "detached", base)
            for path in sorted(REVIEWED_PATHS):
                repo.write(path, "reviewed\n")
            other = repo.commit("renaissance-copy")
            with self.assertRaisesRegex(GuardError, "not an ancestor"):
                validate_release(repo.root, other, base, "master",
                                 accepted=other, reviewed_base=base)

    def test_base_not_ancestor_blocks(self):
        temp, repo, base, sha = accepted_repo()
        with temp:
            repo.git("checkout", "-b", "other", base)
            for path in sorted(REVIEWED_PATHS):
                repo.write(path, "reviewed\n")
            other = repo.commit("renaissance-copy")
            # `other` is on its own branch; sha (the master-side
            # renaissance commit) is not its ancestor.
            with self.assertRaisesRegex(GuardError, "not an ancestor of source"):
                validate_release(repo.root, other, sha, "other",
                                 accepted=other, reviewed_base=sha)

    def test_invalid_sha_blocks(self):
        temp, repo, base, sha = accepted_repo()
        with temp:
            with self.assertRaisesRegex(GuardError, "40-character"):
                validate_release(repo.root, "not-a-sha", base, "master",
                                 accepted=sha, reviewed_base=base)

    def test_pinned_constants_are_the_reviewed_revisions(self):
        self.assertEqual(ACCEPTED_SOURCE_SHA,
                         "c8fcf529698bc57b930495f8c0272b5c1b16b2f4")
        self.assertEqual(REVIEWED_BASE_SHA,
                         "014b48ad9707039871b784d42499602fd899ee74")
        self.assertEqual(len(REVIEWED_PATHS), 22)


if __name__ == "__main__":
    unittest.main()
