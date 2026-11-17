"""[repos] origin check: normalizers agree, and the cluster-side
check stops the payload when the checkout mirrors a different repository."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from hpctether import gitsync, transport

SAME = [
    "git@github.com:Owner/Repo.git",
    "https://github.com/owner/repo",
    "https://github.com/owner/repo.git",
    "https://github.com/owner/repo/",
    "https://user@github.com/owner/repo.git",
    "ssh://git@github.com/owner/repo.git",
    "ssh://git@github.com:22/owner/repo",
]
OTHERS = ["https://github.com/owner/repo2", "git@gitlab.com:owner/repo.git",
          "/srv/git/repo.git", "https://github.com/other/repo"]


def shell_norm(url):
    out = subprocess.run(["bash", "-c", gitsync.SHELL_NORMALIZE + '\nhpc_norm "$1"', "_", url],
                         capture_output=True, text=True, check=True)
    return out.stdout


class TestNormalize(unittest.TestCase):
    def test_spellings_of_one_repo_agree(self):
        self.assertEqual({gitsync.normalize_url(u) for u in SAME}, {"github.com/owner/repo"})

    def test_different_repos_differ(self):
        for u in OTHERS:
            self.assertNotEqual(gitsync.normalize_url(u), "github.com/owner/repo")

    def test_shell_matches_python(self):
        for u in SAME + OTHERS:
            with self.subTest(u=u):
                self.assertEqual(shell_norm(u), gitsync.normalize_url(u))


def _repo(path, url):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", url], check=True)


class TestOriginCheck(unittest.TestCase):
    def run_check(self, remote, expected):
        script = "set -Eeuo pipefail\n" + "\n".join(
            transport.origin_check([("model", remote, expected)])) + "\necho RAN\n"
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    def test_match_runs(self):
        with tempfile.TemporaryDirectory() as d:
            _repo(Path(d), "git@github.com:owner/repo.git")
            r = self.run_check(d, "github.com/owner/repo")
            self.assertEqual((r.returncode, r.stdout), (0, "RAN\n"), r.stderr)

    def test_mismatch_stops(self):
        with tempfile.TemporaryDirectory() as d:
            _repo(Path(d), "https://github.com/owner/other")
            r = self.run_check(d, "github.com/owner/repo")
            self.assertEqual(r.returncode, transport.EXIT_ORIGIN)
            self.assertNotIn("RAN", r.stdout)
            self.assertIn("pairs two different repositories", r.stderr)

    def test_not_a_checkout_stops(self):
        with tempfile.TemporaryDirectory() as d:
            r = self.run_check(d, "github.com/owner/repo")
            self.assertEqual(r.returncode, transport.EXIT_ORIGIN)

    def test_local_origin(self):
        with tempfile.TemporaryDirectory() as d:
            _repo(Path(d), "https://github.com/owner/repo.git")
            self.assertEqual(gitsync.local_origin(d), "https://github.com/owner/repo.git")
            with self.assertRaises(gitsync.OriginError):
                gitsync.local_origin(str(Path(d) / "nope"))


if __name__ == "__main__":
    unittest.main()


class TestHpcRunWiring(unittest.TestCase):
    """hpc-run --dry-run: a git write in a [repos] checkout carries the check."""

    def setUp(self):
        from test_profile import Sites
        from test_trust import _load_bin
        self.mod = _load_bin("hpc-run")
        self.tmp = tempfile.TemporaryDirectory()
        self.local = Path(self.tmp.name) / "model"
        self.Sites = Sites

    def tearDown(self):
        self.tmp.cleanup()

    def run_hpc(self, payload, local):
        from test_trust import _run
        sites = self.Sites("prepare")
        sites.text += f'\n[repos]\nmodel = {{ local = "{local}", remote = "/home/tester/model" }}\n'
        with sites:
            return _run(self.mod, ["--site", "testsite", "--dry-run", "--allow-write", "-"], payload)

    def test_check_in_payload(self):
        _repo(self.local, "git@github.com:owner/model.git")
        code, out, err = self.run_hpc("git -C /home/tester/model pull --ff-only\n", self.local)
        self.assertEqual(code, 0, err)
        self.assertIn('!= github.com/owner/model ]', out)
        self.assertIn("hpc_norm", out)

    def test_no_check_for_reads(self):
        _repo(self.local, "git@github.com:owner/model.git")
        code, out, _ = self.run_hpc("git -C /home/tester/model status\n", self.local)
        self.assertEqual(code, 0)
        self.assertNotIn("hpc_norm", out)

    def test_local_without_origin_refused(self):
        code, out, err = self.run_hpc("git -C /home/tester/model fetch\n", self.local)
        self.assertEqual(code, transport.EXIT_ORIGIN)
        self.assertEqual(out, "")
