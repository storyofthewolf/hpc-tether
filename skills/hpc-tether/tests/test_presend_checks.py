"""Checks made before anything is sent: the invoked script is tracked, a
push precheck survives a git failure, a job name is not repeated, and every
refusal is audited."""
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hpctether import audit, guard, transport

from test_profile import Sites
from test_trust import _load_bin, _run


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class TestInvokedScriptIsTracked(unittest.TestCase):
    """An ignored .py under a script root is not trusted."""

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp()) / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q")
        (self.repo / "my-script.py").write_text("print('trusted')\n")
        (self.repo / ".gitignore").write_text("generated.py\nbuild_scripts/\n")
        _git(self.repo, "add", ".")
        _git(self.repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x")

    def run_payload(self, path):
        root = str(self.repo)
        check = transport.trusted_check([root], [(root, str(path))])
        inner = "\n".join([*transport.strict_prelude(len(check)), *check, "echo BODY-RAN"])
        return subprocess.run(["bash", "-s"], input=f"bash <<'__B__'\n{inner}\n__B__\n",
                              capture_output=True, text=True)

    def test_tracked_script_runs(self):
        p = self.run_payload(self.repo / "my-script.py")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("BODY-RAN", p.stdout)

    def test_ignored_script_is_refused(self):
        (self.repo / "generated.py").write_text("print('not trusted')\n")
        p = self.run_payload(self.repo / "generated.py")
        self.assertEqual(p.returncode, transport.EXIT_UNTRUSTED)
        self.assertNotIn("BODY-RAN", p.stdout)
        self.assertIn("not a file git tracks", p.stderr)

    def test_missing_script_is_refused(self):
        self.assertEqual(self.run_payload(self.repo / "nope.py").returncode,
                         transport.EXIT_UNTRUSTED)

    def test_build_payload_checks_each_use(self):
        class Use:
            root = "/r/repo"
            path = "/r/repo/lib/tool.py"
        site = transport.Site(name="t", host="h", scheduler="slurm", user="u")
        payload = transport.build_payload(site, "ls\n", trust_uses=[Use()])
        self.assertIn("git -C /r/repo ls-files --error-unmatch -- lib/tool.py", payload)


class TestPushPrecheckGitFailure(unittest.TestCase):
    """A git failure is not 'untracked'."""

    @classmethod
    def setUpClass(cls):
        cls.script = _load_bin("hpc-push")._PRECHECK_SCRIPT
        cls.dir = Path(tempfile.mkdtemp())

    def run_precheck(self, path, env=None):
        proc = subprocess.run(
            ["/bin/bash", "-c",
             f"set -euo pipefail\nset -- {shlex.quote(str(path))}\n{self.script}"],
            capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.split()

    def test_broken_repository_is_an_error(self):
        broken = self.dir / "broken"
        broken.mkdir()
        (broken / ".git").write_text("gitdir: /no/such/place\n")
        (broken / "f.csv").write_text("a\n")
        self.assertEqual(self.run_precheck(broken / "f.csv")[1], "GIT_ERROR")

    def test_no_git_on_path(self):
        empty = self.dir / "bin"
        empty.mkdir(exist_ok=True)
        for tool in ("dirname", "basename", "sha256sum", "cut"):
            found = subprocess.run(["bash", "-c", f"command -v {tool}"],
                                   capture_output=True, text=True).stdout.strip()
            if found and not (empty / tool).exists():
                os.symlink(found, empty / tool)
        out = self.run_precheck(self.dir / "x.txt", env={"PATH": str(empty)})
        self.assertEqual(out, ["MISSING", "GIT_UNAVAILABLE"])

    def test_unknown_state_refuses_the_push(self):
        from unittest import mock
        mod = _load_bin("hpc-push")
        local = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        local.write("x\n"); local.close()
        try:
            for state in ("GIT_ERROR", "GIT_UNAVAILABLE", "garbage", ""):
                reply = subprocess.CompletedProcess(
                    [], 0, stdout=f"MISSING\n{state}\n", stderr="")
                with self.subTest(state=state), Sites("improvise"), \
                        mock.patch.object(mod.transport, "run_capture", return_value=reply), \
                        mock.patch.object(mod.subprocess, "run") as scp:
                    code, _, err = _run(mod, [local.name, "--site", "testsite", "--to",
                                              "scratch:data.csv", "--yes"])
                    self.assertEqual(code, transport.EXIT_GUARD)
                    self.assertIn("is unknown", err)
                    scp.assert_not_called()
        finally:
            os.unlink(local.name)


class TestRepeatedJobName(unittest.TestCase):
    """The queue is checked once, so one name is submitted once."""

    def test_repeats(self):
        self.assertEqual(guard.sbatch_repeats("sbatch -J a j.sh; sbatch -J a j.sh\n"), ["a"])
        self.assertEqual(guard.sbatch_repeats("sbatch -J a j.sh\nsbatch --job-name=a k.sh\n"), ["a"])
        self.assertEqual(
            guard.sbatch_repeats("for i in 1 2 3; do sbatch -J a j.sh; done\n"), ["a"])
        self.assertEqual(
            guard.sbatch_repeats("for i in 1 2\ndo\n  sbatch -J a j.sh\ndone\n"), ["a"])

    def test_not_repeats(self):
        self.assertEqual(guard.sbatch_repeats("sbatch -J a j.sh; sbatch -J b j.sh\n"), [])
        self.assertEqual(
            guard.sbatch_repeats("for f in x y; do ls $f; done\nsbatch -J a j.sh\n"), [])
        self.assertEqual(guard.sbatch_repeats("squeue -u me\n"), [])

    def test_hpc_run_refuses(self):
        mod = _load_bin("hpc-run")
        with Sites("improvise"):
            code, _, err = _run(mod, ["--site", "testsite", "--allow-write", "-"],
                                "sbatch -J a j.sh; sbatch -J a j.sh\n")
        self.assertEqual(code, transport.EXIT_DUPLICATE)
        self.assertIn("more than once", err)


class _AuditFile:
    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "audit.jsonl"
        self._prev = os.environ.get("HPC_TETHER_AUDIT")
        os.environ["HPC_TETHER_AUDIT"] = str(self.path)
        return self

    def __exit__(self, *exc):
        if self._prev is None:
            os.environ.pop("HPC_TETHER_AUDIT", None)
        else:
            os.environ["HPC_TETHER_AUDIT"] = self._prev
        self._tmp.cleanup()

    def rows(self):
        if not self.path.exists():
            return []
        return [json.loads(l) for l in self.path.read_text().splitlines()]


class TestRefusalsAreAudited(unittest.TestCase):
    """A refusal leaves a line; code past the body limit isn't sent."""

    def setUp(self):
        self.mod = _load_bin("hpc-run")

    def refused(self, script, *flags, level="improvise"):
        with Sites(level), _AuditFile() as log:
            code, _, err = _run(self.mod, ["--site", "testsite", *flags, "-"], script)
            return code, err, log.rows()

    def test_forbidden_payload_is_recorded(self):
        code, _, rows = self.refused("rm /tmp/anything/a\n")
        self.assertEqual(code, transport.EXIT_GUARD)
        self.assertEqual([(r["kind"], r["rc"], r["detail"]) for r in rows],
                         [("refused", transport.EXIT_GUARD, "rm /tmp/anything/a")])

    def test_profile_refusal_is_recorded(self):
        code, _, rows = self.refused("mkdir $HPC_SCRATCH/x\n", "--allow-write", level="observe")
        self.assertEqual(code, transport.EXIT_GUARD)
        self.assertEqual([r["kind"] for r in rows], ["refused"])
        self.assertEqual(rows[0]["body"], "mkdir $HPC_SCRATCH/x\n")
        self.assertEqual(rows[0]["level"], "prepare")

    def test_missing_token_is_recorded(self):
        code, _, rows = self.refused("mkdir $HPC_SCRATCH/x\n")
        self.assertEqual(code, transport.EXIT_GUARD)
        self.assertEqual([r["kind"] for r in rows], ["refused"])

    def test_code_past_the_body_limit_is_not_sent(self):
        script = "# " + "x" * audit.BODY_LIMIT + "\npython3 -c 'print(1)'\n"
        code, err, rows = self.refused(script, "--allow-exec")
        self.assertEqual(code, transport.EXIT_GUARD)
        self.assertIn("audit log keeps at most", err)
        self.assertEqual([r["kind"] for r in rows], ["refused"])


if __name__ == "__main__":
    unittest.main()
