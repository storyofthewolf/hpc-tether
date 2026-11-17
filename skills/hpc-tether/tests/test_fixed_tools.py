"""hpc-read across files, hpc-ls, and hpc-jobs --brief.

The remote scripts run here as the shell they ship as: the real strict
prelude, then the script, in local bash against a temp directory. Nothing
connects. hpc-ls needs GNU find's -printf, which macOS lacks, so a small
stand-in `find` implementing the one form hpc-ls uses goes first on PATH
when the local find is not GNU.
"""

from __future__ import annotations

import gzip
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hpctether import site as site_mod, transport

from test_trust import TempSites, _load_bin, _run

ROOT = Path(__file__).resolve().parent.parent

#: `find [-H] PATH [-mindepth 1] -maxdepth N [! -name '.*'] -printf FMT`, the
#: one form hpc-ls sends, with the directives it uses.
_FIND_SHIM = r'''#!/usr/bin/env python3
import os, sys
a = sys.argv[1:]
follow = a[:1] == ["-H"]
if follow: a = a[1:]
path, a = a[0], a[1:]
mindepth = 0; hide = False; fmt = None
while a:
    if a[0] == "-mindepth": mindepth = int(a[1]); a = a[2:]
    elif a[0] == "-maxdepth": a = a[2:]
    elif a[:3] == ["!", "-name", ".*"]: hide = True; a = a[3:]
    elif a[0] == "-printf": fmt = a[1]; a = a[2:]
    else: sys.exit("shim: unsupported " + a[0])
if path.startswith("-"): sys.exit("shim: path read as an expression")
def rec(p, name):
    st = os.lstat(p)
    kind = "l" if os.path.islink(p) else "d" if os.path.isdir(p) else "f"
    out = fmt
    for k, v in (("%y", kind), ("%s", str(st.st_size)), ("%T@", "%.10f" % st.st_mtime),
                 ("%f", name), ("%l", os.readlink(p) if kind == "l" else "")):
        out = out.replace(k, v)
    sys.stdout.write(out.replace("\\0", "\0"))
if mindepth == 0:
    rec(path, os.path.basename(path.rstrip("/")) or "/")
else:
    for name in os.listdir(path):
        if hide and name.startswith("."): continue
        rec(os.path.join(path, name), name)
'''


def _gnu_find() -> bool:
    p = subprocess.run(["find", "--version"], capture_output=True, text=True)
    return p.returncode == 0 and "GNU" in p.stdout


def run_script(script: str, path_dir: str | None = None) -> subprocess.CompletedProcess:
    """The script under the real prelude and epilogue, as build_payload sends it."""
    inner = "\n".join([*transport.strict_prelude(1), "readonly HPC_X=1", script,
                       *transport.STRICT_EPILOGUE])
    env = dict(os.environ, HPC_HOST="test.example.edu")
    if path_dir:
        env["PATH"] = path_dir + os.pathsep + env["PATH"]
    return subprocess.run(["/bin/bash", "-s"], input=f"/bin/bash <<'__B__'\n{inner}\n__B__\n",
                          capture_output=True, text=True, env=env, timeout=60)


class TempTree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = Path(self._tmp.name).resolve()
        self.shim_dir = None
        if not _gnu_find():
            self.shim_dir = str(self.d / "_bin")
            os.mkdir(self.shim_dir)
            shim = Path(self.shim_dir) / "find"
            shim.write_text(_FIND_SHIM.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
            shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
        self.top = self.d / "run"
        self.top.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, text, mtime=None):
        p = self.top / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        if mtime:
            os.utime(p, (mtime, mtime))
        return p


class TestReadScript(TempTree):
    def setUp(self):
        super().setUp()
        self.mod = _load_bin("hpc-read")
        self.write("a.log", "start\nERROR one\nok\nERROR two\nERROR three\n")
        self.write("b.log", "ERROR b\n")
        self.write("-n.log", "-n\nERROR dash\n")
        self.write("c.txt", "ERROR txt\n")
        self.write("sub/s.log", "ERROR deep\n")

    def grep(self, pattern, paths, glob="", max_files=20, per=0):
        return run_script(self.mod.build_script("grep", 0, 100000, pattern, max_files, per,
                                                glob, [str(p) for p in paths]))

    def test_glob_greps_each_file_with_its_name_and_no_subdirectory(self):
        p = self.grep("ERROR", [self.top], glob="*.log")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("a.log:2:ERROR one", p.stdout)
        self.assertIn("b.log:1:ERROR b", p.stdout)
        self.assertIn("-n.log:2:ERROR dash", p.stdout)
        self.assertNotIn("txt", p.stdout)
        self.assertNotIn("deep", p.stdout)

    def test_per_file_cap(self):
        p = self.grep("ERROR", [self.top], glob="*.log", per=1)
        self.assertIn("a.log:2:ERROR one", p.stdout)
        self.assertNotIn("ERROR two", p.stdout)

    def test_file_cap_counts_the_rest(self):
        p = self.grep("ERROR", [self.top], glob="*.log", max_files=1)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(p.stdout.splitlines()), 1)
        self.assertIn("2 more not searched", p.stderr)

    def test_named_files_and_a_dash_pattern(self):
        p = self.grep("-n", [self.top / "a.log", self.top / "-n.log", self.top / "gone.log"])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("-n.log:1:-n", p.stdout)
        self.assertIn("not a readable file:", p.stderr)

    def test_one_file_keeps_the_old_format(self):
        p = self.grep("ERROR", [self.top / "a.log"])
        self.assertEqual(p.stdout, "2:ERROR one\n4:ERROR two\n5:ERROR three\n")

    def test_no_matches_and_no_files(self):
        self.assertEqual(self.grep("zzz", [self.top], glob="*.log").stdout, "(no matches)\n")
        p = self.grep("x", [self.top], glob="*.nc")
        self.assertEqual(p.returncode, 66)
        self.assertIn("no file in", p.stderr)

    def test_bad_regex_is_an_error(self):
        self.assertEqual(self.grep("[", [self.top / "a.log"]).returncode, 2)

    def test_glob_text_is_never_run(self):
        marker = self.d / "RAN"
        p = self.grep("x", [self.top], glob=f"*$(touch {marker})*")
        self.assertFalse(marker.exists())
        self.assertNotEqual(p.returncode, 0)

    def test_tail_of_a_dash_named_file(self):
        p = run_script(self.mod.build_script("tail", 1, 1000, "", 20, 0, "",
                                             [str(self.top / "-n.log")]))
        self.assertEqual((p.returncode, p.stdout), (0, "ERROR dash\n"))

    def gz(self, name, text):
        p = self.top / name
        p.write_bytes(gzip.compress(text.encode()))
        return p

    def test_tail_and_head_of_a_gzipped_log(self):
        p = self.gz("atm.log.1.gz", "one\ntwo\nERROR three\n")
        for mode, want in (("tail", "ERROR three\n"), ("head", "one\n")):
            r = run_script(self.mod.build_script(mode, 1, 1000, "", 20, 0, "", [str(p)]))
            self.assertEqual((r.returncode, r.stdout), (0, want), r.stderr)

    def test_grep_mixes_gzipped_and_plain_files(self):
        z = self.gz("atm.log.2.gz", "ok\nERROR gz\n")
        p = self.grep("ERROR", [self.top / "b.log", z])
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("b.log:1:ERROR b", p.stdout)
        self.assertIn("atm.log.2.gz:2:ERROR gz", p.stdout)


class TestReadCommandLine(unittest.TestCase):
    def setUp(self):
        self.mod = _load_bin("hpc-read")

    def call(self, *argv):
        sent = []

        def fake(target, script, timeout=60, trust_uses=None):
            sent.append(script)
            return transport.Result(0, "", "")

        with TempSites(), mock.patch.object(self.mod.transport, "run_capture", fake):
            code, out, err = _run(self.mod, ["--site", "testsite", *argv])
        return code, err, sent

    def args_of(self, script):
        return shlex.split(script.splitlines()[0])[2:]

    def test_tail_takes_one_file(self):
        code, err, sent = self.call("scratch:a.log", "scratch:b.log", "--tail", "5")
        self.assertEqual((code, sent), (transport.EXIT_USAGE, []))
        self.assertIn("need --grep", err)

    def test_pattern_only_in_the_last_component(self):
        code, err, _ = self.call("scratch:*/a.log", "--grep", "x")
        self.assertEqual(code, transport.EXIT_USAGE)
        self.assertIn("last component", err)

    def test_glob_becomes_directory_and_pattern(self):
        code, _, sent = self.call("scratch:case42/*.log", "--grep", "ERROR")
        self.assertEqual(code, 0)
        self.assertEqual(self.args_of(sent[0]),
                         ["grep", "0", "262144", "ERROR", "20", "50", "*.log",
                          "/scratch/tester/case42"])

    def test_several_files_get_the_default_per_file_cap(self):
        _, _, sent = self.call("scratch:a", "/x/b", "--grep", "E", "--max-files", "5")
        self.assertEqual(self.args_of(sent[0])[4:],
                         ["5", "50", "", "/scratch/tester/a", "/x/b"])

    def test_one_file_grep_is_uncapped(self):
        _, _, sent = self.call("scratch:a", "--grep", "E")
        self.assertEqual(self.args_of(sent[0])[5], "0")

    def test_max_files_bounded(self):
        code, _, _ = self.call("scratch:*.log", "--grep", "E", "--max-files", "999")
        self.assertEqual(code, transport.EXIT_USAGE)

    def test_dollar_and_relative_paths_refused(self):
        self.assertEqual(self.call("$HPC_SCRATCH/a", "--tail", "1")[0], transport.EXIT_USAGE)
        self.assertEqual(self.call("a.log", "--tail", "1")[0], transport.EXIT_USAGE)


class TestRemotePath(unittest.TestCase):
    def site(self, **paths):
        return site_mod.Site(name="t", host="h", scheduler="slurm", user="u", paths=paths)

    def test_forms(self):
        s = self.site(rundir="/proj/run/")
        self.assertEqual(s.remote_path("/a/b"), "/a/b")
        self.assertEqual(s.remote_path("rundir:c/d"), "/proj/run/c/d")
        for bad in ("$HPC_RUNDIR/x", "x/y", "nope:x"):
            with self.assertRaises(site_mod.SiteError):
                s.remote_path(bad)

    def test_a_relative_role_is_refused(self):
        with self.assertRaises(site_mod.SiteError):
            self.site(odd="-rf").remote_path("odd:x")


class TestLsScript(TempTree):
    def setUp(self):
        super().setUp()
        self.mod = _load_bin("hpc-ls")
        self.write("b.nc", "x" * 3000, mtime=1_700_000_000)
        self.write("a.log", "y", mtime=1_700_000_300)
        self.write("-weird name.txt", "zz", mtime=1_700_000_100)
        self.write(".hidden", "h", mtime=1_700_000_200)
        (self.top / "sub").mkdir()
        os.utime(self.top / "sub", (1_600_000_000, 1_600_000_000))
        os.symlink("/elsewhere/target dir", self.top / "link")

    def ls(self, path=None, sort="name", limit=200, show_all=False):
        p = run_script(self.mod.build_script(str(path or self.top), sort, limit, show_all),
                       self.shim_dir)
        return p

    def names(self, p):
        kind, entries, total = self.mod.parse(p.stdout)
        return kind, [e["name"] for e in entries], total, entries

    def test_lists_one_level_by_name_without_dot_names(self):
        p = self.ls()
        self.assertEqual(p.returncode, 0, p.stderr)
        kind, names, total, entries = self.names(p)
        self.assertEqual(kind, "dir")
        self.assertEqual(names, ["-weird name.txt", "a.log", "b.nc", "link", "sub"])
        self.assertEqual(total, 5)
        by = {e["name"]: e for e in entries}
        self.assertEqual((by["b.nc"]["type"], by["b.nc"]["size"]), ("file", 3000))
        self.assertEqual((by["link"]["type"], by["link"]["target"]),
                         ("link", "/elsewhere/target dir"))
        self.assertEqual(by["sub"]["type"], "dir")

    def test_all_shows_dot_names(self):
        self.assertIn(".hidden", self.names(self.ls(show_all=True))[1])

    def test_sort_by_time_and_size(self):
        # The link was made just now: newest.
        self.assertEqual(self.names(self.ls(sort="time"))[1],
                         ["link", "a.log", "-weird name.txt", "b.nc", "sub"])
        self.assertEqual(self.names(self.ls(sort="size"))[1][0], "b.nc")

    def test_limit_counts_the_rest(self):
        p = self.ls(limit=2)
        _, names, total, _ = self.names(p)
        self.assertEqual((len(names), total), (2, 5))

    def test_one_file_and_one_link(self):
        _, names, total, entries = self.names(self.ls(self.top / "a.log"))
        self.assertEqual((names, total, entries[0]["size"]), (["a.log"], 1, 1))
        kind, names, _, entries = self.names(self.ls(self.top / "link"))
        self.assertEqual((kind, entries[0]["target"]), ("entry", "/elsewhere/target dir"))

    def test_missing_path(self):
        p = self.ls(self.top / "nope")
        self.assertEqual(p.returncode, 66)
        self.assertIn("no such file or directory", p.stderr)

    def test_empty_directory(self):
        (self.top / "empty").mkdir()
        self.assertEqual(self.names(self.ls(self.top / "empty"))[1:3], ([], 0))


class TestLsCommandLine(unittest.TestCase):
    def setUp(self):
        self.mod = _load_bin("hpc-ls")

    def call(self, stdout, *argv, rc=0):
        sent = []

        def fake(target, script, timeout=60, trust_uses=None):
            sent.append(script)
            return transport.Result(rc, stdout, "")

        with TempSites(), mock.patch.object(self.mod.transport, "run_capture", fake):
            code, out, err = _run(self.mod, ["--site", "testsite", *argv])
        return code, out, err, sent

    LISTING = ("###DIR###\n"
               "d/4096/1700000000.5/sub/\0"
               "f/1536/1700000060.0/a.log/\0"
               "l/12/1700000120.0/cur/../x/y\0"
               "###TOTAL### 7\n")

    def test_table_and_more_line(self):
        code, out, _, sent = self.call(self.LISTING, "scratch:case", "--limit", "3")
        self.assertEqual(code, 0)
        self.assertEqual(shlex.split(sent[0].splitlines()[0])[2:],
                         ["/scratch/tester/case", "-t/ -k4,4", "3", "0"])
        self.assertIn("/scratch/tester/case: 7 entries", out)
        self.assertIn("dir   4.0K  2023-11-14 22:13:20  sub/\n", out)
        self.assertIn("1.5K", out)
        self.assertIn("cur -> ../x/y", out)
        self.assertIn("4 more not shown", out)

    def test_json(self):
        code, out, _, _ = self.call(self.LISTING, "/a", "--json", "--bytes")
        data = json.loads(out)
        self.assertEqual((data["total"], data["shown"]), (7, 3))
        self.assertEqual(data["entries"][1]["size"], 1536)

    def test_garbage_is_a_remote_failure(self):
        code, _, err, _ = self.call("hello\n", "/a")
        self.assertEqual(code, self.mod.EXIT_REMOTE)

    def test_remote_code_passes_through(self):
        self.assertEqual(self.call("", "/a", rc=66)[0], 66)

    def test_usage(self):
        self.assertEqual(self.call("", "rel/path")[0], transport.EXIT_USAGE)
        self.assertEqual(self.call("", "/a", "--limit", "0")[0], transport.EXIT_USAGE)


class TestJobsBrief(unittest.TestCase):
    def setUp(self):
        self.mod = _load_bin("hpc-jobs")
        S = self.mod.SEP

        self.running = ("###SQUEUE###\n" + S.join(["123", "run1", "RUNNING", "1:02:03", "1"])
                        + "\n###SACCT###\n123|run1|RUNNING|01:02:00||0:0\n"
                        "123.batch|batch|RUNNING|01:02:00||0:0\n")
        self.done = ("###SQUEUE###\n###SACCT###\n"
                     "123|run1|FAILED|02:00:00||1:0\n123.batch|batch|FAILED|02:00:00|9G|1:0\n")
        self.cancelled = ("###SQUEUE###\n###SACCT###\n"
                          "123|run1|CANCELLED by 999|00:10:00||0:15\n")

    def call(self, stdout, *argv):
        sent = []

        def fake(target, script, timeout=60, trust_uses=None):
            sent.append(script)
            return transport.Result(0, stdout, "")

        with TempSites(), mock.patch.object(self.mod.transport, "run_capture", fake):
            code, out, err = _run(self.mod, ["--site", "testsite", *argv])
        return code, out, err, sent

    def test_running_job_is_one_line_from_squeue(self):
        code, out, _, sent = self.call(self.running, "--job", "123", "--brief")
        self.assertEqual((code, out), (0, "123 RUNNING 1:02:03 0:0\n"))
        self.assertTrue(sent[0].startswith("set -- 123\n"))

    def test_finished_job_from_sacct(self):
        self.assertEqual(self.call(self.done, "--job", "123", "--brief")[1],
                         "123 FAILED 02:00:00 1:0\n")
        self.assertEqual(self.call(self.cancelled, "--job", "123", "--brief")[1],
                         "123 CANCELLED 00:10:00 0:15\n")

    def test_unknown_job(self):
        self.assertEqual(self.call("###SQUEUE###\n###SACCT###\n", "--job", "9", "--brief")[1],
                         "9 NOT_FOUND - -\n")

    def test_json(self):
        rec = json.loads(self.call(self.done, "--job", "123", "--brief", "--json")[1])
        self.assertEqual(rec, {"jobid": "123", "state": "FAILED", "elapsed": "02:00:00",
                               "exit": "1:0"})

    def test_brief_needs_job(self):
        self.assertEqual(self.call("", "--brief")[0], transport.EXIT_USAGE)

    def test_job_and_user_are_validated(self):
        for argv in (["--job=-u x"], ["--job", "123;id"], ["--user=-a"]):
            code, _, _, sent = self.call("", *argv)
            self.assertEqual((code, sent), (transport.EXIT_USAGE, []), argv)

    def test_values_are_bound_to_their_options(self):
        self.assertIn('squeue --jobs="$jobid"', self.mod._JOB_SCRIPT)
        self.assertIn('sacct --jobs="$jobid"', self.mod._JOB_SCRIPT)
        self.assertIn('squeue --user="$HPC_USER"', self.mod._QUEUE_SCRIPT)


class TestOptionsEnd(unittest.TestCase):
    """Every agent-supplied path or pattern in a fixed tool's remote script
    follows `--`, or (find) is an absolute path."""

    def test_scripts(self):
        read = _load_bin("hpc-read")._SCRIPT
        for needle in ('tail -n "$n" -- "$p"', 'head -n "$n" -- "$p"',
                       'gzip -cd -- "$p"', '"$g" "${opts[@]}" -- "$pat"',
                       'cd -- "$d"'):
            self.assertIn(needle, read)
        sync = _load_bin("hpc-sync-status")._REMOTE_SCRIPT
        self.assertIn('cd -- "$p"', sync)
        self.assertIn("stat -c '%s %Y' -- \"$p\"", _load_bin("hpc-pull")._STAT_SCRIPT)


class PullSizeCheckStep(unittest.TestCase):
    """The size check is marked so receipts can hide it."""

    def test_remote_stat_marks_its_audit_line(self):
        from unittest import mock
        pull = _load_bin("hpc-pull")
        result = mock.Mock(returncode=0, stdout="10 20\n", stderr="")
        with mock.patch.object(pull.transport, "run_capture", return_value=result) as rc:
            self.assertEqual(pull.remote_stat(mock.Mock(), "/x", timeout=5), (10, 20))
        self.assertEqual(rc.call_args.kwargs["audit_extra"], {"step": "sizecheck"})


if __name__ == "__main__":
    unittest.main()
