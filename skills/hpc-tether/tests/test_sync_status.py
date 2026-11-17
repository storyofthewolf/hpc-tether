"""hpc-sync-status: per-repo measurement, the remote script, and NAME/--all.

Local repos are real (a bare origin plus a clone); the cluster side is the
remote script run locally, or canned output. Nothing connects."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from hpctether import transport

from test_profile import Sites
from test_trust import _load_bin, _run

MOD = _load_bin("hpc-sync-status")


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


class Fixture:
    """origin (bare) <- local clone with two commits on main and one on feature."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        self.local = root / "local"
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@x"}
        os.environ.update(env)
        git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=root)
        git("clone", "-q", str(self.origin), str(self.local), cwd=root)
        git("switch", "-q", "-c", "main", cwd=self.local)
        for i in (1, 2):
            (self.local / "f").write_text(str(i))
            git("add", "f", cwd=self.local)
            git("commit", "-q", "-m", f"c{i}", cwd=self.local)
        git("push", "-q", "origin", "main", cwd=self.local)
        self.c1 = git("rev-parse", "HEAD~1", cwd=self.local)
        self.c2 = git("rev-parse", "HEAD", cwd=self.local)
        git("switch", "-q", "-c", "feature", cwd=self.local)
        (self.local / "g").write_text("x")
        git("add", "g", cwd=self.local)
        git("commit", "-q", "-m", "feat", cwd=self.local)
        git("push", "-q", "origin", "feature", cwd=self.local)
        self.feat = git("rev-parse", "HEAD", cwd=self.local)
        git("switch", "-q", "main", cwd=self.local)
        self.url = str(self.origin)

    def hpc(self, head, branch="main", dirty=0, origin=None):
        return {"head": head, "branch": branch, "subject": "s", "dirty": dirty,
                "origin": origin or self.url}


class TestMeasure(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()

    def tearDown(self):
        self.f.tmp.cleanup()

    def m(self, hpc, pin=None):
        return MOD.measure("r", self.f.local, "/c/r", hpc, pin)

    def test_in_sync(self):
        self.assertEqual(self.m(self.f.hpc(self.f.c2))["differs"], [])

    def test_cluster_behind(self):
        d = self.m(self.f.hpc(self.f.c1))["differs"]
        self.assertEqual(len(d), 1)
        self.assertIn("1 behind origin/main", d[0])

    def test_feature_branch_on_cluster_is_not_a_difference(self):
        rec = self.m(self.f.hpc(self.f.feat, branch="feature"))
        self.assertEqual(rec["differs"], [])

    def test_commit_made_on_cluster(self):
        d = self.m(self.f.hpc("ab" * 20))["differs"]
        self.assertTrue(any("local clone doesn't have" in x for x in d), d)

    def test_dirty_both_and_unpushed(self):
        (self.f.local / "f").write_text("dirty")
        d = " | ".join(self.m(self.f.hpc(self.f.c2, dirty=3))["differs"])
        self.assertIn("local has 1 uncommitted", d)
        self.assertIn("cluster has 3 uncommitted", d)

    def test_origin_mismatch(self):
        d = self.m(self.f.hpc(self.f.c2, origin="https://github.com/x/other"))["differs"]
        self.assertTrue(any("different repositories" in x for x in d))

    def test_pin(self):
        d = self.m(self.f.hpc(self.f.feat, branch="feature"), pin="main")["differs"]
        self.assertTrue(any("pinned to 'main'" in x for x in d))

    def test_unknown_branch_on_origin(self):
        d = self.m(self.f.hpc(self.f.c2, branch="local-only"))["differs"]
        self.assertTrue(any("no branch 'local-only'" in x for x in d))

    def test_remote_error(self):
        rec = self.m({"error": "cannot cd to /c/r"})
        self.assertEqual(rec["error_side"], "hpc")


class TestRemoteScript(unittest.TestCase):
    def test_runs_and_parses(self):
        f = Fixture()
        try:
            missing = str(Path(f.tmp.name) / "nope")
            script = "set -Eeuo pipefail\n" + MOD.remote_script([str(f.local), missing])
            out = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            parsed = MOD.parse_remote(out.stdout)
            self.assertEqual(parsed[str(f.local)]["head"], f.c2)
            self.assertEqual(parsed[str(f.local)]["branch"], "main")
            self.assertEqual(parsed[str(f.local)]["origin"], f.url)
            self.assertEqual(parsed[str(f.local)]["dirty"], 0)
            self.assertIn("cannot cd", parsed[missing]["error"])
        finally:
            f.tmp.cleanup()


class TestMain(unittest.TestCase):
    """NAME and --all through main(), with the cluster replaced by a local run."""

    def setUp(self):
        self.f = Fixture()
        self.saved = MOD.transport.run_capture

        def fake(site, script, timeout=60, trust_uses=None):
            p = subprocess.run(["bash", "-c", "set -Eeuo pipefail\n" + script],
                               capture_output=True, text=True)
            return transport.Result(p.returncode, p.stdout, p.stderr)
        MOD.transport.run_capture = fake

    def tearDown(self):
        MOD.transport.run_capture = self.saved
        self.f.tmp.cleanup()

    def sites(self, extra):
        s = Sites("observe")
        s.text += extra
        return s

    def test_name_and_all(self):
        # The "cluster checkout" is the local clone itself: in sync.
        entry = f'\n[repos]\nr = {{ local = "{self.f.local}", remote = "{self.f.local}" }}\n'
        with self.sites(entry):
            code, out, err = _run(MOD, ["--site", "testsite", "r"])
            self.assertEqual(code, 0, out + err)
            self.assertIn("in sync", out)
            code, out, _ = _run(MOD, ["--site", "testsite", "--all", "--json"])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(out)["agree"])

    def test_unknown_name_and_no_table(self):
        with self.sites(""):
            code, _, err = _run(MOD, ["--site", "testsite", "r"])
            self.assertEqual(code, transport.EXIT_USAGE)
            self.assertIn("no [repos] table", err)

    def test_all_reports_each(self):
        bad = Path(self.f.tmp.name) / "nope"
        entry = (f'\n[repos]\nr = {{ local = "{self.f.local}", remote = "{self.f.local}" }}\n'
                 f's = {{ local = "{self.f.local}", remote = "{bad}" }}\n')
        with self.sites(entry):
            code, out, _ = _run(MOD, ["--site", "testsite", "--all"])
        self.assertEqual(code, 1)
        self.assertIn("in sync", out)
        self.assertIn("ERROR: cannot cd", out)


if __name__ == "__main__":
    unittest.main()
