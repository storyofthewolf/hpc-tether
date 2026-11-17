"""Payload assembly tests: prelude exports and the remote watchdog wrapper."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hpctether import transport
from hpctether.site import Site

SITE = Site(
    name="testsite",
    host="cluster.example.edu",
    scheduler="slurm",
    user="tester",
    paths={"scratch": "/scratch/tester", "rundir": "/proj/tester/run"},
)


class TestBuildPayload(unittest.TestCase):
    def test_exports_present(self):
        payload = transport.build_payload(SITE, "echo hi\n")
        for line in (
            "export HPC_SITE=testsite",
            "export HPC_HOST=cluster.example.edu",
            "export HPC_USER=tester",
            "export HPC_SCRATCH=/scratch/tester",
            "export HPC_RUNDIR=/proj/tester/run",
        ):
            self.assertIn(line, payload)

    def test_git_never_prompts(self):
        # A credential prompt in a payload has no terminal and hangs until the
        # watchdog; these make it fail at once.
        payload = transport.build_payload(SITE, "git -C /x fetch\n")
        prelude = payload.split("<<'__HPC_BODY__'")[0]
        self.assertIn("export GIT_TERMINAL_PROMPT=0", prelude)
        self.assertIn(
            'export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes}"', prelude)

    def test_git_prompt_exports_are_valid_bash(self):
        import subprocess
        script = "\n".join(transport.GIT_NO_PROMPT) + '\nprintf "%s|%s" "$GIT_TERMINAL_PROMPT" "$GIT_SSH_COMMAND"\n'
        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                             env={"PATH": "/usr/bin:/bin"}).stdout
        self.assertEqual(out, "0|ssh -o BatchMode=yes")
        kept = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin", "GIT_SSH_COMMAND": "ssh -i k"}).stdout
        self.assertEqual(kept, "0|ssh -i k")

    def test_watchdog_wraps_script(self):
        payload = transport.build_payload(SITE, "echo hi\n", local_timeout=120)
        budget = transport.watchdog_budget(120)
        self.assertIn(
            f"timeout --kill-after={transport.WATCHDOG_KILL_AFTER} {budget} "
            "bash /dev/fd/3 3<<'__HPC_BODY__' </dev/null",
            payload,
        )
        self.assertIn(
            "\n".join(transport.STRICT_PRELUDE)
            + "\nreadonly HPC_SITE HPC_HOST HPC_USER HPC_SCHEDULER HPC_RUNDIR HPC_SCRATCH"
            + "\necho hi\n" + "\n".join(transport.STRICT_EPILOGUE) + "\n__HPC_BODY__",
            payload)
        self.assertIn('exit "$rc"', payload)
        self.assertIn("remote watchdog killed the payload", payload)

    def test_strict_mode_inside_heredoc_only(self):
        payload = transport.build_payload(SITE, "echo hi\n")
        before_heredoc = payload.split("<<'__HPC_BODY__'")[0]
        self.assertNotIn("pipefail", before_heredoc)
        self.assertNotIn("trap ", before_heredoc)

    def test_budget_formula(self):
        self.assertEqual(transport.watchdog_budget(120), 105)
        self.assertEqual(transport.watchdog_budget(60), 45)
        self.assertEqual(transport.watchdog_budget(20), transport.WATCHDOG_MIN)

    def test_delimiter_collision_avoided(self):
        script = "cat <<'__HPC_BODY__'\nx\n__HPC_BODY__\n"
        payload = transport.build_payload(SITE, script)
        self.assertIn("<<'__HPC_BODY___'", payload)

    def test_shell_metacharacters_quoted_in_exports(self):
        site = Site(
            name="s", host="h", scheduler="slurm", user="u",
            paths={"weird": "/p/with space/$x"},
        )
        payload = transport.build_payload(site, "true\n")
        self.assertIn("export HPC_WEIRD='/p/with space/$x'", payload)


class TestManifest(unittest.TestCase):
    def test_round_trip(self):
        import os
        import tempfile

        from hpctether import manifest

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HPC_TETHER_MANIFEST"] = str(Path(tmp) / "m.json")
            try:
                local = Path(tmp) / "file.txt"
                local.write_text("content\n")
                entry = manifest.record(local, "testsite", "/remote/file.txt", 8, 1234)
                self.assertEqual(entry["remote_path"], "/remote/file.txt")
                got = manifest.lookup(local)
                self.assertIsNotNone(got)
                self.assertEqual(got["sha256"], entry["sha256"])
                self.assertEqual(got["site"], "testsite")
                self.assertIsNone(manifest.lookup(Path(tmp) / "other.txt"))
            finally:
                del os.environ["HPC_TETHER_MANIFEST"]


if __name__ == "__main__":
    unittest.main()


class TestWritableRoots(unittest.TestCase):
    """`writable_roots` widens the guard, so its validation is the contract."""

    def _site(self, body: str):
        import tempfile, os
        from hpctether import site as site_mod
        d = tempfile.mkdtemp()
        p = Path(d) / "t.toml"
        p.write_text(
            'name="t"\nhost="h"\nscheduler="slurm"\nuser="u"\n' + body
        )
        orig = site_mod.sites_dir
        site_mod.sites_dir = lambda: Path(d)
        try:
            return site_mod.load("t")
        finally:
            site_mod.sites_dir = orig

    def test_roots_combine_with_scratch(self):
        s = self._site('writable_roots=["/a/b"]\n[paths]\nscratch="/s/c"\n')
        self.assertEqual(sorted(s.guard_roots()), ["/a/b", "/s/c"])

    def test_absent_leaves_only_scratch(self):
        s = self._site('[paths]\nscratch="/s/c"\n')
        self.assertEqual(s.guard_roots(), ["/s/c"])

    def test_shallow_root_refused(self):
        from hpctether.site import SiteError
        for bad in ("/", "/gpfs"):
            with self.assertRaises(SiteError):
                self._site(f'writable_roots=["{bad}"]\n')

    def test_relative_or_dotdot_refused(self):
        from hpctether.site import SiteError
        for bad in ("a/b", "/a/../../b"):
            with self.assertRaises(SiteError):
                self._site(f'writable_roots=["{bad}"]\n')

    def test_non_list_refused(self):
        from hpctether.site import SiteError
        with self.assertRaises(SiteError):
            self._site('writable_roots="/a/b"\n')


class TestScratchRootValidated(unittest.TestCase):
    """The scratch role gates `rm`, so it is validated like a writable root.

    `rm` is write-tier *only* inside scratch and forbidden everywhere else, so
    a site declaring scratch = "/" would make deletion anywhere read as routine
    confined work. Scratch previously received none of these checks while
    gating the most destructive verb in the toolkit.
    """

    _site = TestWritableRoots._site

    def test_shallow_scratch_refused(self):
        from hpctether.site import SiteError
        for bad in ("/", "/scratch"):
            with self.assertRaises(SiteError):
                self._site(f'[paths]\nscratch="{bad}"\n')

    def test_relative_or_dotdot_scratch_refused(self):
        from hpctether.site import SiteError
        for bad in ("relative/path", "/a/../.."):
            with self.assertRaises(SiteError):
                self._site(f'[paths]\nscratch="{bad}"\n')

    def test_normal_scratch_accepted(self):
        s = self._site('[paths]\nscratch="/scratch/user"\n')
        self.assertEqual(s.paths["scratch"], "/scratch/user")

    def test_other_roles_not_constrained(self):
        # Only scratch gates rm; an ordinary role stays a plain path.
        s = self._site('[paths]\nscratch="/s/c"\nhome="/h"\n')
        self.assertEqual(s.paths["home"], "/h")


class TestTempScriptPrefixes(unittest.TestCase):
    """`hpc-run` refuses script files in temp dirs; the list must cover macOS.

    $TMPDIR on macOS is /var/folders/<hash>/T, so `mktemp` output -- the usual
    way a throwaway script gets written at all -- landed outside the prefix
    list and was accepted.
    """

    def _mod(self):
        import importlib.machinery, importlib.util
        p = Path(__file__).resolve().parent.parent / "bin" / "hpc-run"
        loader = importlib.machinery.SourceFileLoader("hpcrun", str(p))
        spec = importlib.util.spec_from_loader("hpcrun", loader)
        m = importlib.util.module_from_spec(spec)
        loader.exec_module(m)
        return m

    def test_macos_per_user_temp_covered(self):
        prefixes = self._mod()._TEMP_PREFIXES
        for p in ("/var/folders/1c/abc/T/s.sh", "/private/var/folders/1c/abc/T/s.sh"):
            self.assertTrue(p.startswith(prefixes), f"{p} not treated as temp")

    def test_real_tmpdir_covered(self):
        # Whatever this machine's $TMPDIR is, a script there must be refused.
        import os, tempfile
        tmpdir = os.path.realpath(os.environ.get("TMPDIR", tempfile.gettempdir()))
        candidate = str(Path(tmpdir) / "s.sh")
        self.assertTrue(
            candidate.startswith(self._mod()._TEMP_PREFIXES),
            f"$TMPDIR {tmpdir!r} is not covered by _TEMP_PREFIXES",
        )

    def test_repo_paths_still_accepted(self):
        prefixes = self._mod()._TEMP_PREFIXES
        for p in ("/Users/x/projects/hpc-tether/skills/hpc-tether/lib/j.sh",
                  "/home/x/lib/job.sh"):
            self.assertFalse(p.startswith(prefixes), f"{p} wrongly treated as temp")


class TestPushCommitScript(unittest.TestCase):
    """hpc-push's compare-and-swap, exercised as the shell it actually ships.

    The script runs remotely, so these tests run the real _COMMIT_SCRIPT text
    through bash against a temp directory rather than reimplementing its logic
    -- a paraphrase would pass while the shipped shell was broken.

    What it must guarantee: the destination is replaced atomically or not at
    all, a concurrent edit is never overwritten, and no failure path leaves the
    destination damaged.
    """

    def _script(self) -> str:
        import importlib.machinery, importlib.util
        p = Path(__file__).resolve().parent.parent / "bin" / "hpc-push"
        loader = importlib.machinery.SourceFileLoader("hpcpush", str(p))
        spec = importlib.util.spec_from_loader("hpcpush", loader)
        m = importlib.util.module_from_spec(spec)
        loader.exec_module(m)
        return m._COMMIT_SCRIPT

    def _run(self, workdir, dest, tmp, expected, backup_dir="bk", backup="b.bak"):
        import shlex, subprocess
        args = " ".join(shlex.quote(a) for a in
                        [dest, tmp, expected, backup_dir, backup])
        proc = subprocess.run(
            ["bash", "-c", f"set -- {args}\n{self._script()}"],
            cwd=workdir, capture_output=True, text=True,
        )
        return proc

    def _sha(self, path: Path) -> str:
        import hashlib
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _workdir(self):
        import tempfile
        d = Path(tempfile.mkdtemp())
        (d / "bk").mkdir()
        return d

    def test_commits_and_backs_up(self):
        d = self._workdir()
        (d / "dest").write_text("old\n")
        (d / "tmp").write_text("new\n")
        proc = self._run(d, "dest", "tmp", self._sha(d / "dest"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("COMMITTED", proc.stdout)
        self.assertEqual((d / "dest").read_text(), "new\n")
        self.assertEqual((d / "bk" / "b.bak").read_text(), "old\n")
        self.assertFalse((d / "tmp").exists(), "temp file left behind")

    def test_concurrent_edit_is_never_overwritten(self):
        """The defect this restructure exists to close."""
        d = self._workdir()
        (d / "dest").write_text("old\n")
        expected = self._sha(d / "dest")
        (d / "dest").write_text("someone else's work\n")  # lands mid-upload
        (d / "tmp").write_text("mine\n")
        proc = self._run(d, "dest", "tmp", expected)
        self.assertEqual(proc.returncode, 65, proc.stdout + proc.stderr)
        self.assertIn("CONFLICT", proc.stdout)
        self.assertEqual((d / "dest").read_text(), "someone else's work\n")
        # The toolkit never deletes, so the temp stays and is named.
        self.assertTrue((d / "tmp").exists())
        self.assertIn("temp left at", proc.stderr)
        self.assertFalse((d / "bk" / "b.bak").exists(),
                         "a backup of a state we did not replace is misleading")

    def test_new_file_needs_no_backup(self):
        d = self._workdir()
        (d / "tmp").write_text("brand new\n")
        proc = self._run(d, "dest", "tmp", "MISSING")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((d / "dest").read_text(), "brand new\n")
        self.assertFalse((d / "bk" / "b.bak").exists())

    def test_destination_appearing_during_upload_conflicts(self):
        d = self._workdir()
        (d / "dest").write_text("raced in\n")
        (d / "tmp").write_text("mine\n")
        proc = self._run(d, "dest", "tmp", "MISSING")
        self.assertEqual(proc.returncode, 65, proc.stdout + proc.stderr)
        self.assertEqual((d / "dest").read_text(), "raced in\n")
        self.assertTrue((d / "tmp").exists())
        self.assertIn("temp left at", proc.stderr)

    def test_existing_backup_is_never_clobbered(self):
        d = self._workdir()
        (d / "bk" / "b.bak").write_text("irreplaceable\n")
        (d / "dest").write_text("old\n")
        (d / "tmp").write_text("new\n")
        proc = self._run(d, "dest", "tmp", self._sha(d / "dest"))
        self.assertEqual(proc.returncode, 70, proc.stdout + proc.stderr)
        self.assertEqual((d / "bk" / "b.bak").read_text(), "irreplaceable\n")
        self.assertEqual((d / "dest").read_text(), "old\n",
                         "destination modified despite a refused backup")

    def test_missing_temp_leaves_destination_alone(self):
        d = self._workdir()
        (d / "dest").write_text("old\n")
        proc = self._run(d, "dest", "tmp", self._sha(d / "dest"))
        self.assertEqual(proc.returncode, 70, proc.stdout + proc.stderr)
        self.assertEqual((d / "dest").read_text(), "old\n")

    def test_rename_is_atomic(self):
        """A reader sees the old file or the new one, never a partial write.

        This is the property that makes the temp-and-rename worth the extra
        round trip: it holds against an interrupted transfer, with no second
        party involved.
        """
        import os, threading
        d = self._workdir()
        (d / "a").write_text("A" * 50000)
        torn, stop = [0], [False]

        def reader():
            while not stop[0]:
                try:
                    data = (d / "a").read_text()
                except (FileNotFoundError, OSError):
                    continue
                if data and (len(data) != 50000 or len(set(data)) != 1):
                    torn[0] += 1

        t = threading.Thread(target=reader)
        t.start()
        try:
            for i in range(100):
                (d / "tmp").write_text(("B" if i % 2 else "A") * 50000)
                os.rename(d / "tmp", d / "a")
        finally:
            stop[0] = True
            t.join()
        self.assertEqual(torn[0], 0, "observed a partially written destination")


class TestTransportNeverDeletes(unittest.TestCase):
    """The deletion invariant lives in the transport, not in hpc-run.

    Whichever verb assembles the payload, a script containing a delete is
    refused with exit 77 before `require_master` and before `ssh` is ever
    invoked. Verified by replacing subprocess.run with a function that fails
    the test if reached.
    """

    def _run_with_ssh_forbidden(self, script):
        import subprocess

        def never(*a, **k):
            raise AssertionError("ssh was invoked for a deleting payload")

        orig = subprocess.run
        subprocess.run = never
        try:
            return transport.run_remote(SITE, script, preflight=True)
        finally:
            subprocess.run = orig

    def test_rm_refused_before_ssh(self):
        for script in ("rm -f -- /x/tmp\n", "setsid rm /x\n", "find /x -maxdepth 1 -delete\n"):
            with self.assertRaises(transport.TransportError) as cm:
                self._run_with_ssh_forbidden(script)
            self.assertEqual(cm.exception.code, transport.EXIT_GUARD, script)
            self.assertIn("never deletes", str(cm.exception))

    def test_non_deleting_payload_reaches_preflight(self):
        # A clean payload must get past the invariant to `ssh -O check`, which
        # here is the point at which our fake subprocess is hit.
        with self.assertRaises(AssertionError):
            self._run_with_ssh_forbidden("squeue -u x\n")


class TestStrictPreludeNamesTheFailure(unittest.TestCase):
    """Under set -e a failing command used to end the payload silently
    (a grep with no match looked like a transport fault)."""

    def _run(self, body):
        import subprocess
        script = "\n".join(transport.strict_prelude()) + "\n" + body + "\n"
        return subprocess.run(["bash"], input=script, capture_output=True, text=True)

    def test_failure_is_named_with_its_payload_line(self):
        p = self._run("echo a\nls / | grep -c no_such_name_xyz\necho never")
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("never", p.stdout)
        self.assertIn("payload stopped at line 2 (exit 1): grep -c no_such_name_xyz", p.stderr)
        self.assertIn("|| true", p.stderr)

    def test_or_true_is_silent_and_strict_mode_kept(self):
        p = self._run("grep -c no_such_name_xyz /dev/null || true\necho reached")
        self.assertEqual((p.returncode, p.stderr), (0, ""))
        self.assertIn("reached", p.stdout)
        p = self._run("echo $UNSET_VAR_XYZ")
        self.assertNotEqual(p.returncode, 0)          # -u still on

    def test_prelude_is_in_the_payload(self):
        self.assertTrue(all(line in transport.build_payload(SITE, "echo hi", local_timeout=60)
                            for line in transport.STRICT_PRELUDE))

    def test_epilogue_follows_the_body_inside_the_heredoc(self):
        payload = transport.build_payload(SITE, "echo hi\n", local_timeout=60)
        self.assertIn("echo hi\n" + "\n".join(transport.STRICT_EPILOGUE) + "\n__HPC_BODY__",
                      payload)


def _bashes():
    """Every distinct bash on this machine: /bin/bash is 3.2 on macOS, and a
    Homebrew bash 5, if installed, stands in for the cluster's 4.4+."""
    import shutil, subprocess
    seen, out = set(), []
    for cand in ("/bin/bash", "/opt/homebrew/bin/bash", "/usr/local/bin/bash",
                 shutil.which("bash")):
        if not cand or not Path(cand).exists():
            continue
        real = Path(cand).resolve()
        if real in seen:
            continue
        seen.add(real)
        v = subprocess.run([cand, "-c", "echo ${BASH_VERSINFO[0]}"],
                           capture_output=True, text=True).stdout.strip()
        out.append((cand, int(v or 0)))
    return out


class TestPayloadStdin(unittest.TestCase):
    """The script is not on the payload's stdin: a command that reads stdin
    used to consume the lines after it, which never ran, and the payload still
    exited 0. Runs the whole build_payload output in each
    local bash, with a stand-in for GNU timeout."""

    def _run(self, bash, body):
        import os, stat, subprocess, tempfile
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "timeout"
            fake.write_text(f'#!{bash}\nshift 2\nexec "$@"\n')
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            # `bash` inside the payload must be the bash under test.
            (Path(d) / "bash").symlink_to(bash)
            env = dict(os.environ, PATH=f"{d}:{os.environ['PATH']}")
            return subprocess.run([bash, "-s"], input=transport.build_payload(SITE, body),
                                  capture_output=True, text=True, env=env)

    def test_stdin_readers_get_eof_and_later_lines_run(self):
        for bash, _ in _bashes():
            for reader in ("cat", "read x || true", "cat | head -1", "wc -l >/dev/null"):
                with self.subTest(bash=bash, reader=reader):
                    p = self._run(bash, f"echo one\n{reader}\necho two\n")
                    self.assertEqual((p.returncode, p.stdout, p.stderr), (0, "one\ntwo\n", ""))

    def test_failure_line_is_still_named(self):
        for bash, _ in _bashes():
            with self.subTest(bash=bash):
                p = self._run(bash, "echo one\ncat\nfalse\necho never\n")
                self.assertEqual((p.returncode, p.stdout), (1, "one\n"))
                self.assertIn("payload stopped at line 3 (exit 1): false", p.stderr)


class TestCutPipelines(unittest.TestCase):
    """A pipeline cut short by head, grep -m or sed q is not a failure; any
    other nonzero status in it still stops the payload.

    Runs the real prelude and epilogue in each local bash, fed through a
    quoted heredoc on stdin the way build_payload delivers them."""

    def run_all(self, body, min_major=3):
        import subprocess
        inner = "\n".join([*transport.strict_prelude(1), "readonly HPC_X=1", body,
                           *transport.STRICT_EPILOGUE])
        results = []
        for bash, major in _bashes():
            if major < min_major:
                continue
            p = subprocess.run([bash, "-s"], input=f"{bash} <<'__B__'\n{inner}\n__B__\n",
                               capture_output=True, text=True, timeout=60)
            results.append((f"{bash} (bash {major})", p))
        if not results:
            self.skipTest(f"no bash >= {min_major} here")
        return results

    def assertContinues(self, body, out, min_major=3):
        for name, p in self.run_all(body, min_major):
            with self.subTest(bash=name):
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(p.stderr, "")
                self.assertTrue(p.stdout.endswith(out), p.stdout)

    def assertStops(self, body, rc, *msg):
        for name, p in self.run_all(body):
            with self.subTest(bash=name):
                self.assertEqual(p.returncode, rc, p.stderr)
                self.assertNotIn("NEVER", p.stdout)
                for m in msg:
                    self.assertIn(m, p.stderr)

    def test_head_grep_m_and_sed_q_continue(self):
        self.assertContinues("yes | head -1\n"
                             "seq 1 1000000 | grep -m1 5\n"
                             "seq 1 1000000 | sed 2q\n"
                             "seq 1 10000000 | head -c 4; echo\n"
                             "echo after", "after\n")

    def test_cut_pipeline_on_the_last_line_exits_0(self):
        self.assertContinues("echo a\nseq 1 1000000 | head -2", "1\n2\n")

    def test_cut_pipeline_in_a_substitution_and_a_function(self):
        self.assertContinues('x=$(seq 1 1000000 | head -1)\n'
                             'f() { yes | head -1; }\nf\necho "x=$x after"', "x=1 after\n")

    def test_failure_before_the_cut_stops(self):
        self.assertStops("echo a\nfalse | head -1\necho NEVER", 1,
                         "payload stopped at line 2 (exit 1): head -1 (pipeline statuses: 1 0)")

    def test_failure_beside_a_cut_stops_with_its_own_status(self):
        self.assertStops("(exit 3) | yes | head -1\necho NEVER", 3,
                         "payload stopped at line 1 (exit 3)", "statuses: 3 141 0")
        self.assertStops("yes | head -1 | grep -c zz; echo NEVER", 1, "statuses: 141 0 1")

    def test_failure_after_a_cut_still_stops(self):
        self.assertStops("yes | head -1\nfalse\necho NEVER", 1,
                         "payload stopped at line 2 (exit 1): false")
        self.assertStops("yes | head -1\nls /no/such/dir_xyz\necho NEVER", 1)

    def test_or_true_still_lets_a_failure_through(self):
        self.assertContinues("yes | head -1\ngrep x /dev/null || true\necho after", "after\n")

    def test_set_plus_e_is_silent_and_honoured(self):
        self.assertContinues("set +e\nfalse\necho after", "after\n")

    def test_exit_codes_pass_through(self):
        self.assertStops("echo a\nexit 5\necho NEVER", 5)
        self.assertStops("set +e\nfalse", 1)

    def test_unset_variable_still_fails(self):
        for name, p in self.run_all("echo $NO_SUCH_VAR_XYZ\necho NEVER"):
            with self.subTest(bash=name):
                self.assertNotEqual(p.returncode, 0)
                self.assertNotIn("NEVER", p.stdout)

    def test_subshell_and_loop_pipelines_continue_on_bash_4(self):
        # bash 3.2 runs no ERR trap for these, so they stop there as before.
        self.assertContinues("(yes | head -1)\n"
                             "seq 1 100000 | while read -r l; do break; done\n"
                             "echo after", "after\n", min_major=4)
