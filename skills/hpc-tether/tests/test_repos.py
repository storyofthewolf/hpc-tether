"""Mirrored checkouts (site [repos]): the one-way git rule, enforced by the guard.

A listed checkout changes on the cluster only by the sync operations of its
mode, whether or not it sits in a write root, and listing it does not make it
one.
"""

import unittest

from hpctether import guard
from hpctether.site import Repo

SCRATCH = "/scratch/u"
PATHS = {"scratch": SCRATCH, "home": "/home/u"}
ROOTS = [SCRATCH]
PULL = Repo(name="model", local="/l/model", remote="/home/u/model")
PINNED = Repo(name="my-model", local="/l/my-model", remote="/proj/u/my-model", branch="main")
TWO = Repo(name="tool", local="/l/tool", remote="/home/u/tool", mode="two-way")
REPOS = {r.name: r for r in (PULL, PINNED, TWO)}


def report(script, repos=REPOS):
    return guard.classify(script + "\n", roots=ROOTS, paths=PATHS, repos=repos)


def worst(script, repos=REPOS):
    return report(script, repos).worst()


class TestPullOnly(unittest.TestCase):
    def test_sync_operations_are_writes(self):
        for s in ("git -C /home/u/model fetch",
                  "git -C /home/u/model fetch --prune origin",
                  "git -C /home/u/model pull --ff-only",
                  "git -C /home/u/model pull --ff-only origin main",
                  "cd /home/u/model && git pull --ff-only",
                  "cd /home/u/model/src && git pull --ff-only",
                  "git -C /home/u/model switch feature-x",
                  "git -C /home/u/model switch --detach v1.2"):
            with self.subTest(s=s):
                self.assertEqual(worst(s), guard.WRITE)

    def test_listed_checkout_needs_no_write_root(self):
        # /home/u/model is outside ROOTS; unlisted, the same pull is refused.
        self.assertEqual(worst("git -C /home/u/model pull --ff-only"), guard.WRITE)
        self.assertEqual(worst("git -C /home/u/model pull --ff-only", repos={}),
                         guard.FORBIDDEN)

    def test_listing_is_not_a_write_root(self):
        for s in ("cp $HPC_SCRATCH/a /home/u/model/a", "mkdir /home/u/model/x",
                  "echo x > /home/u/model/f"):
            with self.subTest(s=s):
                self.assertEqual(worst(s), guard.FORBIDDEN)

    def test_everything_else_refused(self):
        for s in ("git -C /home/u/model pull",
                  "git -C /home/u/model pull --rebase",
                  "git -C /home/u/model pull --ff-only --rebase",
                  "git -C /home/u/model commit -m x",
                  "git -C /home/u/model add a.py",
                  "git -C /home/u/model merge origin/main",
                  "git -C /home/u/model rebase origin/main",
                  "git -C /home/u/model push origin main",
                  "git -C /home/u/model checkout feature-x",
                  "git -C /home/u/model checkout -- a.py",
                  "git -C /home/u/model switch -c new",
                  "git -C /home/u/model switch -C main",
                  "git -C /home/u/model switch --discard-changes main",
                  "git -C /home/u/model switch -f main",
                  "git -C /home/u/model fetch origin +main:main",
                  "git -C /home/u/model fetch origin main:main",
                  "git -C /home/u/model stash",
                  "git -C /home/u/model branch -D old",
                  "git -C /home/u/model config user.email x@y",
                  "git -C /home/u/model restore a.py",
                  "git -C /home/u/model reset HEAD~1",
                  "cd /home/u/model && git commit -am x"):
            with self.subTest(s=s):
                self.assertEqual(worst(s), guard.FORBIDDEN)

    def test_reads_unchanged(self):
        for s in ("git -C /home/u/model status", "git -C /home/u/model log -3",
                  "git -C /home/u/model branch", "git -C /home/u/model config --get remote.origin.url",
                  "git -C /home/u/model rev-parse HEAD"):
            with self.subTest(s=s):
                self.assertIsNone(worst(s))

    def test_refusal_names_the_mode_and_owner(self):
        r = report("git -C /home/u/model commit -m x")
        msg = r.of_kind(guard.FORBIDDEN)[0].message
        self.assertIn("pull-only", msg)
        self.assertIn("user's setting", msg)

    def test_repo_writes_reported(self):
        r = report("git -C /home/u/model fetch\ngit -C /home/u/tool commit -m x\ngit -C /home/u/model status")
        self.assertEqual(r.repo_writes(), ["model", "tool"])


class TestPinned(unittest.TestCase):
    def test_switch_away_refused(self):
        self.assertEqual(worst("git -C /proj/u/my-model switch dev"), guard.FORBIDDEN)

    def test_switch_to_pin_and_pull_allowed(self):
        self.assertEqual(worst("git -C /proj/u/my-model switch main"), guard.WRITE)
        self.assertEqual(worst("git -C /proj/u/my-model pull --ff-only"), guard.WRITE)


class TestTwoWay(unittest.TestCase):
    def test_two_way_operations(self):
        for s in ("git -C /home/u/tool pull", "git -C /home/u/tool add a.py",
                  "git -C /home/u/tool commit -m fix", "git -C /home/u/tool push origin main",
                  "git -C /home/u/tool merge origin/main",
                  "git -C /home/u/tool switch -c topic"):
            with self.subTest(s=s):
                self.assertEqual(worst(s), guard.WRITE)

    def test_two_way_still_refuses_rewrites(self):
        for s in ("git -C /home/u/tool commit --amend -m x",
                  "git -C /home/u/tool pull --rebase",
                  "git -C /home/u/tool rebase main",
                  "git -C /home/u/tool push --force",
                  "git -C /home/u/tool push origin +main:main",
                  "git -C /home/u/tool reset --hard",
                  "git -C /home/u/tool clean -fd",
                  "git -C /home/u/tool checkout -- a.py",
                  "git -C /home/u/tool switch -C main"):
            with self.subTest(s=s):
                self.assertEqual(worst(s), guard.FORBIDDEN)


class TestPlacement(unittest.TestCase):
    def test_unplaceable_repo_falls_back_to_roots(self):
        # --git-dir, or a cd the scan can't follow, is not "listed": the
        # ordinary write-root rule refuses it here.
        self.assertEqual(worst("git --git-dir=/home/u/model/.git fetch"), guard.FORBIDDEN)
        self.assertEqual(worst("cd $SOMEWHERE && git pull --ff-only"), guard.FORBIDDEN)

    def test_lookalike_sibling_not_listed(self):
        self.assertEqual(worst("git -C /home/u/model2 pull --ff-only"), guard.FORBIDDEN)

    def test_alias_spelling(self):
        r = guard.classify("git -C /gpfs/home/u/model pull --ff-only\n", roots=ROOTS,
                           paths=PATHS, repos=REPOS, aliases={"/gpfs/home/u": "/home/u"})
        self.assertEqual(r.worst(), guard.WRITE)


if __name__ == "__main__":
    unittest.main()


class TestStash(unittest.TestCase):
    """Bare `git stash` writes (it stashes the working tree); it was once read."""

    def test_stash_forms(self):
        self.assertEqual(worst("git -C $HPC_SCRATCH/r stash", repos={}), guard.WRITE)
        self.assertEqual(worst("git -C /data/r stash", repos={}), guard.FORBIDDEN)
        self.assertIsNone(worst("git -C /data/r stash list", repos={}))
        self.assertIsNone(worst("git -C /data/r stash show", repos={}))
