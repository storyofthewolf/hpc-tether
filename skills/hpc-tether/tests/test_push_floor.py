"""hpc-push and the floor: write roots, script roots, git-tracked files."""

import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hpctether import transport

from test_trust import _load_bin, _run

SITE = """\
name = "testsite"
host = "test.example.edu"
scheduler = "slurm"
user = "tester"
profile = "prepare"
writable_roots = ["/home/tester"]
script_roots = ["/home/tester/my-tools"]

[aliases]
"/gpfs/home/tester" = "/home/tester"

[paths]
scratch = "/scratch/tester"
"""


class _Site:
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        Path(self._tmp.name, "testsite.toml").write_text(SITE)
        self._prev = os.environ.get("HPC_TETHER_SITES")
        os.environ["HPC_TETHER_SITES"] = self._tmp.name
        return self

    def __exit__(self, *exc):
        if self._prev is None:
            os.environ.pop("HPC_TETHER_SITES", None)
        else:
            os.environ["HPC_TETHER_SITES"] = self._prev
        self._tmp.cleanup()


class TestPushLocalRefusals(unittest.TestCase):
    """Refused before anything is sent: no network needed."""

    def setUp(self):
        self.mod = _load_bin("hpc-push")
        self.local = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        self.local.write("a,b\n"); self.local.close()

    def tearDown(self):
        os.unlink(self.local.name)

    def push(self, dest, *extra):
        with _Site():
            return _run(self.mod, [self.local.name, "--site", "testsite", "--to", dest, *extra])

    def test_outside_write_roots(self):
        code, _, err = self.push("/projects/shared/x.csv", "--allow-write")
        self.assertEqual(code, transport.EXIT_GUARD)
        self.assertIn("outside the write roots", err)

    def test_old_token_name_still_parses(self):
        # --yes was hpc-push's name for --allow-write; a usage error (64)
        # would mean the alias is gone.
        code, _, err = self.push("/projects/shared/x.csv", "--yes")
        self.assertEqual(code, transport.EXIT_GUARD)
        self.assertIn("outside the write roots", err)

    def test_code_into_script_root(self):
        for dest in ("/home/tester/my-tools/x.py", "/home/tester/my-tools/tool"):
            with self.subTest(dest=dest):
                code, _, err = self.push(dest, "--allow-write")
                self.assertEqual(code, transport.EXIT_GUARD)
                self.assertIn("script root", err)

    def test_alias_spelling_of_a_script_root_is_caught(self):
        code, _, err = self.push("/gpfs/home/tester/my-tools/x.py", "--allow-write")
        self.assertEqual(code, transport.EXIT_GUARD)
        self.assertIn("script root", err)

    def test_data_into_script_root_passes_the_floor(self):
        # Without --allow-write it stops at the token, which comes after the floor
        # and after the read-only precheck.
        mod = self.mod
        reply = subprocess.CompletedProcess([], 0, stdout="MISSING\nUNTRACKED\n", stderr="")
        with mock.patch.object(mod.transport, "run_capture", return_value=reply), \
                mock.patch.object(mod.subprocess, "run") as scp:
            code, _, err = self.push("/home/tester/my-tools/data/m.csv")
        self.assertEqual(code, transport.EXIT_GUARD)
        self.assertIn("refusing without --allow-write", err)
        self.assertIn("would create", err)
        self.assertNotIn("unbacked", err)
        scp.assert_not_called()


class TestPushTracked(unittest.TestCase):
    def push(self, reply_text, *extra):
        mod = _load_bin("hpc-push")
        local = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        local.write("x\n"); local.close()
        reply = subprocess.CompletedProcess([], 0, stdout=reply_text, stderr="")
        try:
            with _Site(), mock.patch.object(mod.transport, "run_capture", return_value=reply), \
                    mock.patch.object(mod.subprocess, "run") as scp:
                code, _, err = _run(mod, [local.name, "--site", "testsite", "--to",
                                          "/home/tester/repo/data.csv", *extra])
        finally:
            os.unlink(local.name)
        scp.assert_not_called()
        return code, err

    def test_tracked_destination_refused_before_upload(self):
        code, err = self.push("abc123\nTRACKED\n", "--allow-write", "--force")
        self.assertEqual(code, transport.EXIT_GUARD)
        self.assertIn("git tracks", err)

    def test_tracked_refusal_comes_before_the_token(self):
        """A tracked README was refused for a missing --allow-write,
        so the agent had to ask the user to approve a push bound to fail."""
        code, err = self.push("abc123\nTRACKED\n")
        self.assertEqual(code, transport.EXIT_GUARD)
        self.assertIn("git tracks", err)
        self.assertNotIn("--allow-write", err)

    def test_refusal_is_audited_with_its_reason(self):
        import json
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"HPC_TETHER_AUDIT": os.path.join(d, "a.jsonl")}):
            self.push("abc123\nTRACKED\n", "--allow-write")
            rows = [json.loads(l) for l in open(os.path.join(d, "a.jsonl"))]
        self.assertEqual((rows[-1]["kind"], rows[-1]["refusal"]), ("refused", "git tracks it"))

    def test_missing_token_names_a_replace_and_the_backup(self):
        code, err = self.push("abc123\nUNTRACKED\n", "--force")
        self.assertEqual(code, transport.EXIT_GUARD)
        self.assertIn("would replace (the current copy is backed up first)", err)


class TestPrecheckScript(unittest.TestCase):
    """The remote precheck, run in a real local bash against a real repo."""

    @classmethod
    def setUpClass(cls):
        cls.script = _load_bin("hpc-push")._PRECHECK_SCRIPT
        cls.dir = Path(tempfile.mkdtemp())
        repo = cls.dir / "repo"
        repo.mkdir()
        git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,  # noqa: E731
                                        capture_output=True)
        git("init", "-q")
        (repo / "tracked.py").write_text("x = 1\n")
        (repo / ".gitignore").write_text("*.csv\n")
        (repo / "ignored.csv").write_text("a\n")
        git("add", "tracked.py", ".gitignore")
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x")
        cls.repo = repo

    def run_precheck(self, path):
        proc = subprocess.run(["bash", "-c", f"set -- {shlex.quote(str(path))}\n{self.script}"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.split()

    def test_tracked(self):
        self.assertEqual(self.run_precheck(self.repo / "tracked.py")[1], "TRACKED")

    def test_ignored_is_untracked(self):
        self.assertEqual(self.run_precheck(self.repo / "ignored.csv")[1], "UNTRACKED")

    def test_new_file_in_repo(self):
        self.assertEqual(self.run_precheck(self.repo / "new.csv"), ["MISSING", "UNTRACKED"])

    def test_outside_any_repo(self):
        self.assertEqual(self.run_precheck(self.dir / "x.txt"), ["MISSING", "UNTRACKED"])

    def test_new_directory(self):
        self.assertEqual(self.run_precheck(self.repo / "no" / "such" / "f.csv")[1], "UNTRACKED")


if __name__ == "__main__":
    unittest.main()
