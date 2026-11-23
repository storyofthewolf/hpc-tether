"""The sandbox: a directory where what the profile allows needs no token.

It never widens a profile and never changes a kind: a write is still write
tier, code is still interpreter tier. Inside ``[sandbox] root`` the token is
dropped (``Violation.token`` False), so nothing asks; outside, everything
asks as before. Plus the per-session ``unattended`` switch.
"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from hpctether import guard, site as site_mod, transport, trust, unattended
from hpctether.site import Repo

from test_trust import REPO, TempSites, _load_bin, _run

SB = "/scratch/tester/sandbox"
PATHS = {"scratch": "/scratch/tester", "home": "/home/tester", "sandbox": SB,
         "repo": "/home/tester/tool"}
TOOL = "/home/tester/tool"
ROOTS = trust.Roots(
    roots=(TOOL,),
    scoped=((TOOL, ("* --execute*", "* --purge*")),),
    paths=tuple(sorted(PATHS.items())),
    scope=((TOOL, ("--within", ("* --purge*",))),),
)


def tokens(script, sandbox=SB):
    r = guard.classify(script + "\n", roots=["/scratch/tester"], paths=PATHS,
                       trust=ROOTS, repos={}, sandbox=sandbox)
    return [(v.kind, v.token) for v in r.violations]


class TestGuardWaiver(unittest.TestCase):
    def test_file_writes(self):
        self.assertEqual(tokens("mkdir -p $HPC_SANDBOX/x"), [("write", False)])
        self.assertEqual(tokens("ncap2 -O -s 'a=1' $HPC_SCRATCH/i.nc $HPC_SANDBOX/o.nc"),
                         [("write", False)])
        self.assertEqual(tokens("mkdir -p $HPC_SCRATCH/other"), [("write", True)])

    def test_each_write_on_its_own(self):
        self.assertEqual(
            tokens("cp $HPC_SCRATCH/a $HPC_SANDBOX/b && cp $HPC_SCRATCH/a $HPC_SCRATCH/c"),
            [("write", False), ("write", True)])

    def test_no_sandbox_no_waiver(self):
        self.assertEqual(tokens("mkdir -p /scratch/tester/sandbox/x", sandbox=None),
                         [("write", True)])

    def test_sbatch_by_script_path(self):
        self.assertEqual(tokens("sbatch $HPC_SANDBOX/x/job.sh"), [("write", False)])
        self.assertEqual(tokens("cd $HPC_SANDBOX/x && sbatch job.sh"), [("write", False)])
        self.assertEqual(tokens("sbatch -t 00:30:00 $HPC_SCRATCH/job.sh"), [("write", True)])

    def test_job_control_by_id_keeps_its_token(self):
        self.assertEqual(tokens("scancel 123"), [("write", True)])

    def test_code_run_from_inside(self):
        self.assertEqual(tokens("cd $HPC_SANDBOX/x && python foo.py"), [("interpreter", False)])
        self.assertEqual(tokens("cd $HPC_SANDBOX/c && ./xmlchange X=1"), [("interpreter", False)])
        # Placed by where it runs, not by where the file is.
        self.assertEqual(tokens("python $HPC_SANDBOX/x/foo.py"), [("interpreter", True)])
        self.assertEqual(tokens("cd $HPC_SCRATCH && python foo.py"), [("interpreter", True)])

    def test_trusted_script_scope_option(self):
        run = "python $HPC_REPO/build.py make x --execute"
        self.assertEqual(tokens(run), [("write", True)])
        self.assertEqual(tokens(run + " --within $HPC_SANDBOX/d"), [("write", False)])
        self.assertEqual(tokens(run + " --within=$HPC_SANDBOX/d"), [("write", False)])
        self.assertEqual(tokens(run + " --within $HPC_SCRATCH/prod"), [("write", True)])

    def test_always_confirm_beats_scope(self):
        self.assertEqual(
            tokens("python $HPC_REPO/datamgr.py retire x --purge --within $HPC_SANDBOX/d"),
            [("write", True)])

    def test_git(self):
        self.assertEqual(tokens("git clone https://e.org/x.git $HPC_SANDBOX/src/x"),
                         [("write", False)])
        self.assertEqual(tokens("git -C $HPC_SANDBOX/clone push"), [("write", True)])

    def test_forbidden_stays_forbidden(self):
        r = guard.classify("rm -rf $HPC_SANDBOX/x\n", roots=["/scratch/tester"],
                           paths=PATHS, repos={}, sandbox=SB)
        self.assertEqual(r.worst(), guard.FORBIDDEN)

    def test_the_role_cannot_be_moved(self):
        r = guard.classify("HPC_SANDBOX=/home/tester; mkdir $HPC_SANDBOX/x\n",
                           roots=["/scratch/tester"], paths=PATHS, repos={}, sandbox=SB)
        self.assertEqual(r.worst(), guard.FORBIDDEN)


SANDBOX_TOML = """\
name = "testsite"
host = "test.example.edu"
scheduler = "slurm"
user = "tester"
profile = "{profile}"

[paths]
scratch = "/scratch/tester"

[sandbox]
root = "/scratch/tester/sandbox"
"""


class TestSiteFile(unittest.TestCase):
    def load(self, text):
        with TempSites(text):
            return site_mod.load("testsite")

    def test_root_and_role(self):
        s = self.load(SANDBOX_TOML.format(profile="prepare"))
        self.assertEqual(s.sandbox, SB)
        self.assertEqual(s.env()["HPC_SANDBOX"], SB)
        self.assertEqual(s.remote_path("sandbox:a/b"), SB + "/a/b")

    def test_must_be_inside_a_write_root(self):
        bad = SANDBOX_TOML.format(profile="prepare").replace(SB, "/projects/x/sandbox")
        with self.assertRaisesRegex(site_mod.SiteError, "not inside a write root"):
            self.load(bad)

    def test_must_clear_protected(self):
        bad = SANDBOX_TOML.format(profile="prepare").replace(
            'profile = "prepare"\n',
            'profile = "prepare"\n[policy]\nprotected = ["/scratch/tester/sandbox/keep"]\n')
        # [policy] table must precede the others in TOML; rebuild cleanly.
        bad = ('name = "testsite"\nhost = "h"\nscheduler = "slurm"\nuser = "tester"\n'
               '[policy]\nprofile = "prepare"\nprotected = ["/scratch/tester/sandbox/keep"]\n'
               '[paths]\nscratch = "/scratch/tester"\n[sandbox]\nroot = "' + SB + '"\n')
        with self.assertRaisesRegex(site_mod.SiteError, "overlaps protected"):
            self.load(bad)

    def test_unknown_key(self):
        with self.assertRaisesRegex(site_mod.SiteError, "unknown key"):
            self.load(SANDBOX_TOML.format(profile="prepare") + "roots = []\n")

    def test_scope_keys_need_a_trusted_repo(self):
        text = SANDBOX_TOML.format(profile="operate") + (
            '\n[repos.tool]\nremote = "/home/tester/tool"\nscope_option = "--within"\n')
        with self.assertRaisesRegex(site_mod.SiteError, "trusted"):
            self.load(text)


class TestHpcRun(unittest.TestCase):
    def setUp(self):
        self.mod = _load_bin("hpc-run")

    def run_at(self, profile, payload, *flags):
        with TempSites(SANDBOX_TOML.format(profile=profile)):
            return _run(self.mod, ["--site", "testsite", "--dry-run", *flags, "-"], payload)

    def test_sandbox_write_needs_no_token(self):
        code, _, err = self.run_at("prepare", "mkdir -p $HPC_SANDBOX/x\n")
        self.assertEqual(code, 0, err)

    def test_write_outside_still_needs_it(self):
        code, _, err = self.run_at("prepare", "mkdir -p $HPC_SCRATCH/x\n")
        self.assertEqual(code, transport.EXIT_GUARD)
        self.assertIn("--allow-write was not given", err)

    def test_never_widens_the_profile(self):
        # Code run inside the sandbox still needs improvise.
        code, _, err = self.run_at("operate", "cd $HPC_SANDBOX/x && python a.py\n")
        self.assertEqual(code, transport.EXIT_GUARD)
        self.assertIn("REFUSED by profile", err)
        code, _, err = self.run_at("improvise", "cd $HPC_SANDBOX/x && python a.py\n")
        self.assertEqual(code, 0, err)


class TestHpcPush(unittest.TestCase):
    def test_in_sandbox(self):
        mod = _load_bin("hpc-push")
        with TempSites(SANDBOX_TOML.format(profile="prepare")):
            target = site_mod.load("testsite")
        self.assertTrue(mod._in_sandbox(SB + "/x/a.yaml", target))
        self.assertFalse(mod._in_sandbox("/scratch/tester/a.yaml", target))

    def test_push_into_sandbox_needs_no_token(self):
        mod = _load_bin("hpc-push")
        local = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        local.write("a\n")
        local.close()
        replies = iter([
            # precheck: new file, untracked, directory missing
            mock.Mock(returncode=0, stdout="MISSING\nUNTRACKED\nNODIR\n", stderr=""),
            mock.Mock(returncode=0, stdout="", stderr=""),          # mkdir -p
            mock.Mock(returncode=0, stdout="COMMITTED\n", stderr=""),  # commit
        ])
        calls = []

        def fake(target, script, **kw):
            calls.append(script)
            return next(replies)

        try:
            with TempSites(SANDBOX_TOML.format(profile="prepare")), \
                    mock.patch.object(mod.transport, "run_capture", side_effect=fake), \
                    mock.patch.object(mod.subprocess, "run",
                                      return_value=mock.Mock(returncode=0)), \
                    mock.patch.object(mod.manifest, "record", create=True):
                code, _, err = _run(mod, [local.name, "--site", "testsite",
                                          "--to", "sandbox:new/dir/a.csv"])
        finally:
            os.unlink(local.name)
        self.assertNotIn("refusing without --allow-write", err)
        self.assertTrue(any("mkdir -p" in c and "/sandbox/new/dir" in c for c in calls), calls)


class TestUnattended(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"HPC_TETHER_AUDIT": self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_on_off(self):
        self.assertIsNone(unattended.current("s1"))
        unattended.switch_on("s1", None)
        self.assertIsNotNone(unattended.current("s1"))
        self.assertIsNone(unattended.current("s2"))  # per session
        self.assertTrue(unattended.switch_off("s1"))
        self.assertIsNone(unattended.current("s1"))
        self.assertTrue(unattended.state_file("s1").exists())  # nothing deleted

    def test_expires(self):
        unattended.switch_on("s1", datetime.now(timezone.utc) - timedelta(minutes=1))
        self.assertIsNone(unattended.current("s1"))

    def test_parse_until_is_the_next_one(self):
        now = datetime(2026, 11, 22, 23, 0, tzinfo=timezone.utc)
        end = unattended.parse_until(now.astimezone().strftime("%H:%M"), now)
        self.assertGreater(end, now)
        with self.assertRaises(ValueError):
            unattended.parse_until("25:00")

    def test_tool(self):
        mod = _load_bin("hpc-unattended")
        code, out, _ = _run(mod, ["on", "08:00", "--session", "s9"])
        self.assertEqual(code, 0)
        self.assertIn("UNATTENDED until 08:00", out)
        code, out, _ = _run(mod, ["off", "--session", "s9"])
        self.assertIn("attended again", out)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": ""}):
            code, _, err = _run(mod, ["on"])
        self.assertEqual(code, transport.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
