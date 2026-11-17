"""The receipts hook: the user sees what the toolkit did, from the audit log."""

import importlib.util
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HOOK = Path(__file__).resolve().parent / "receipts.py"
spec = importlib.util.spec_from_file_location("receipts", HOOK)
receipts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receipts)


def row(**kw):
    kw.setdefault("session", "S1")
    kw.setdefault("site", "cluster")
    kw.setdefault("tokens", [])
    kw.setdefault("kind", "payload")
    kw.setdefault("rc", 0)
    return kw


ROWS = {
    "read": row(verb="hpc-jobs", argv="--job 4242 --brief", seconds=3.0),
    "run_read": row(verb="hpc-run", argv="-", level="observe", seconds=2.9,
                    detail="squeue -u $USER", body="squeue -u $USER\nsacct -j 1 -X"),
    "write": row(verb="hpc-run", argv="--allow-write -", tokens=["--allow-write"],
                 level="prepare", seconds=3.1, detail="mkdir -p $HPC_SCRATCH/new",
                 body="mkdir -p $HPC_SCRATCH/new"),
    "forbidden": row(verb="hpc-run", argv="-", kind="refused", rc=77,
                     refusal="forbidden at every profile", detail="rm -f $HPC_SCRATCH/x", body="rm -f $HPC_SCRATCH/x"),
    "over_profile": row(verb="hpc-run", argv="-", kind="refused", rc=77, level="improvise",
                        detail="python -c 'print(1)'", body="python -c 'print(1)'"),
    "trusted": row(verb="hpc-run", argv="-", level="operate", seconds=40.2,
                  detail="my-script.py check case1",
                  trusted=[{"path": "/h/my-tools/my-script.py", "args": "check case1", "tier": "free"}]),
    "failed_read": row(verb="hpc-read", argv="rundir:atm.log --tail 50", rc=2, seconds=2.5),
    "pull": row(verb="hpc-pull", argv="archive:x.nc", kind="scp", seconds=8, direction="in",
                files=1, bytes=52428800, remote="/scratch/u/archive/x.nc"),
    "push": row(verb="hpc-push", argv="f.csv --yes", tokens=["--yes"], kind="scp", seconds=1.2,
                direction="out", files=1, bytes=2048, remote="/scratch/u/f.csv"),
    "push_precheck": row(verb="hpc-push", argv="c.txt --to /h/my-tools/README.md", step="precheck",
                         seconds=2.8),
    "push_refused": row(verb="hpc-push", argv="c.txt --to /h/my-tools/README.md", kind="refused",
                        rc=77, refusal="git tracks it", detail="push to /h/my-tools/README.md"),
    "timeout": row(verb="hpc-probe", argv="4242", rc=124, seconds=60),
    "budget": row(verb="hpc-pull", argv="x --budget-add 2G", kind="budget",
                  detail="budget +2.0 GiB -> 4.0 GiB", tokens=["--budget-add"]),
}


class Env(unittest.TestCase):
    """A private audit log, sites dir and config dir for each test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.log = self.tmp / "audit" / "audit.jsonl"
        self.log.parent.mkdir()
        self.config = self.tmp / "config"
        (self.config / "sites").mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {
            "HPC_TETHER_AUDIT": str(self.log),
            "HPC_TETHER_SITES": str(self.config / "sites"),
        })
        self.env.start()
        self.cfg = mock.patch.object(receipts, "config_dir", lambda: str(self.config))
        self.cfg.start()

    def tearDown(self):
        self.cfg.stop()
        self.env.stop()
        shutil.rmtree(self.tmp)

    def append(self, *entries):
        with open(self.log, "a") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

    def set_mode(self, mode):
        (self.config / "config.toml").write_text('receipts = "%s"\n' % mode)

    def tool(self, command="bin/hpc-jobs", session="S1", event="PostToolUse"):
        return receipts.on_tool({"hook_event_name": event, "session_id": session,
                                 "tool_name": "Bash", "tool_input": {"command": command}})

    def start(self, session="S1", source="startup", used=False):
        out = receipts.on_start({"hook_event_name": "SessionStart",
                                 "session_id": session, "source": source})
        if used:  # as if the status had already been shown this session
            receipts.mark_announced(session)
        return out

    def skill(self, session="S1", name="hpc-tether:hpc-tether", event="PostToolUse"):
        return receipts.on_tool({"hook_event_name": event, "session_id": session,
                                 "tool_name": "Skill", "tool_input": {"skill": name}})

    def bar(self, session="S1"):
        return receipts.statusline({"session_id": session})


class TestReceiptLines(unittest.TestCase):
    def line(self, key):
        return receipts.receipt(ROWS[key])

    def test_read(self):
        self.assertEqual(self.line("read"),
                         "hpc-tether ▸ cluster · read · hpc-jobs --job 4242 --brief · ok 3.0s")

    def test_hpc_run_shows_first_line_and_the_rest_counted(self):
        self.assertIn("read · hpc-run: squeue -u $USER (+1 lines) · ok", self.line("run_read"))

    def test_write_names_its_approval(self):
        self.assertIn("write, approved --allow-write · hpc-run: mkdir -p", self.line("write"))

    def test_refusals_say_nothing_was_sent(self):
        self.assertIn("REFUSED, nothing sent (forbidden at every profile) · hpc-run: rm -f",
                      self.line("forbidden"))
        self.assertIn("REFUSED, nothing sent (needs profile improvise)", self.line("over_profile"))

    def test_push_refusal_names_its_reason_not_the_precheck(self):
        """A refused push showed 'write · ... · ok'."""
        self.assertEqual(self.line("push_refused"),
                         "hpc-tether ▸ cluster · REFUSED, nothing written (git tracks it) · "
                         "hpc-push c.txt --to /h/my-tools/README.md")
        self.assertIn("read (push precheck)", self.line("push_precheck"))

    def test_trusted_script_named(self):
        self.assertIn("trusted script my-script.py check case1", self.line("trusted"))

    def test_toolkit_stops_are_named(self):
        """A duplicate job name read as 'FAILED rc 79'."""
        self.assertIn("STOPPED by the job-name check (already queued or running, or squeue failed); nothing submitted",
                      receipts.receipt(row(verb="hpc-run", argv="-", rc=79, level="operate")))
        self.assertIn("STOPPED, a script root is not a clean checkout",
                      receipts.receipt(row(verb="hpc-run", argv="-", rc=78, level="operate")))

    def test_failure_and_timeout_stand_out(self):
        self.assertIn("FAILED rc 2", self.line("failed_read"))
        self.assertIn("TIMED OUT", self.line("timeout"))

    def test_transfers_say_direction_size_and_approval(self):
        self.assertIn("pulled 1 file, 50.0 MB ← /scratch/u/archive/x.nc", self.line("pull"))
        self.assertIn("PUSHED 1 file, 2.0 KB → /scratch/u/f.csv, approved --yes", self.line("push"))
        self.assertIn("transfer budget raised", self.line("budget"))

    def test_long_paths_keep_the_file_name_and_what_follows(self):
        entry = row(verb="hpc-nc", seconds=3.1,
                    argv="stats archive:run1/out/run1.history.0001-01-11.nc TS --weights gw")
        self.assertIn("hpc-nc stats archive:…/run1.history.0001-01-11.nc TS --weights gw",
                      receipts.receipt(entry))
        self.assertEqual(receipts.short_path("/a/b/c/d/e/f/g/h/i/j/k/l/m/n/o/p/q/r/s.nc"), "…/s.nc")
        self.assertEqual(receipts.short_path("rundir:atm.log"), "rundir:atm.log")

    def test_every_kind_renders(self):
        for key, entry in ROWS.items():
            with self.subTest(key=key):
                self.assertTrue(receipts.receipt(entry).startswith("hpc-tether ▸ cluster · "))


class TestPathPrelude(unittest.TestCase):
    PRE = 'export PATH="$HPC_PYTHON_BIN:$PATH"'

    def test_summary_skips_the_path_line(self):
        body = self.PRE + "\n\n# note\ncommand -v python\npython -c 'print(1)'"
        line = receipts.receipt(row(verb="hpc-run", argv="--allow-exec -", level="improvise",
                                    detail=self.PRE, body=body))
        self.assertIn("hpc-run: command -v python (+1 lines)", line)
        self.assertNotIn("export PATH", line)

    def test_refusal_shows_the_first_substantive_line(self):
        line = receipts.receipt(row(verb="hpc-run", argv="-", kind="refused", rc=77,
                                    level="improvise", detail=self.PRE,
                                    body=self.PRE + "\npython -c 'print(1)'"))
        self.assertIn("hpc-run: python -c 'print(1)'", line)
        self.assertNotIn("(+", line)

    def test_refused_line_in_detail_is_kept(self):
        line = receipts.receipt(row(verb="hpc-run", argv="-", kind="refused", rc=77,
                                    refusal="forbidden at every profile",
                                    detail="rm -f x", body=self.PRE + "\nls\nrm -f x"))
        self.assertIn("hpc-run: rm -f x (+1 lines)", line)


class TestNotable(unittest.TestCase):
    def test_plain_clean_reads_are_not_notable(self):
        self.assertFalse(receipts.notable(ROWS["read"]))
        self.assertFalse(receipts.notable(ROWS["run_read"]))
        self.assertFalse(receipts.notable(ROWS["push_precheck"]))
        # With --yes on the command line, a push printed its
        # precheck, its PUSHED line and its commit; only PUSHED is needed.
        for step in ("precheck", "commit"):
            ok = row(verb="hpc-push", argv="f --yes", tokens=["--yes"], step=step)
            self.assertFalse(receipts.notable(ok))
            self.assertTrue(receipts.notable(dict(ok, rc=70)))

    def test_pull_size_check_hidden_when_ok(self):
        # The size check preceded every `pulled` line.
        ok = row(verb="hpc-pull", argv="archive:x.nc", step="sizecheck", seconds=2.5)
        self.assertFalse(receipts.notable(ok))
        self.assertTrue(receipts.notable(dict(ok, rc=66)))

    def test_everything_else_is(self):
        for key in ("write", "forbidden", "over_profile", "trusted", "failed_read", "push_refused",
                    "pull", "push", "timeout", "budget"):
            with self.subTest(key=key):
                self.assertTrue(receipts.notable(ROWS[key]))


class TestToolEvent(Env):
    def test_shows_this_sessions_new_entries_once(self):
        self.append(row(verb="hpc-jobs", argv="old", session="S1"))
        self.start(used=True)
        self.append(ROWS["read"], ROWS["write"], row(verb="hpc-jobs", session="OTHER"))
        out = self.tool()
        self.assertEqual(len(out.splitlines()), 2)
        self.assertNotIn("old", out)
        self.assertIsNone(self.tool())

    def test_failure_event_too(self):
        self.start(used=True)
        self.append(ROWS["failed_read"])
        self.assertIn("FAILED", self.tool(event="PostToolUseFailure"))

    def test_bash_without_a_cluster_verb_is_ignored(self):
        self.start(used=True)
        self.append(ROWS["read"])
        self.assertIsNone(self.tool("ls -la"))
        self.assertIsNotNone(self.tool("python3 /p/bin/hpc-jobs --job 1"))

    def test_without_a_cursor_reads_this_sessions_month(self):
        self.append(ROWS["read"])
        self.assertIn("hpc-jobs", self.tool())

    def test_half_written_line_waits(self):
        self.start(used=True)
        with open(self.log, "a") as fh:
            fh.write(json.dumps(ROWS["read"]))
        self.assertIsNone(self.tool())
        with open(self.log, "a") as fh:
            fh.write("\n")
        self.assertIn("hpc-jobs", self.tool())

    def test_notable_mode_drops_plain_reads(self):
        self.set_mode("notable")
        self.start(used=True)
        self.append(ROWS["read"], ROWS["forbidden"])
        out = self.tool()
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("REFUSED", out)

    def test_off_mode_shows_nothing(self):
        self.set_mode("off")
        self.append(ROWS["forbidden"])
        self.assertIsNone(self.tool())
        self.assertIsNone(self.start(used=True))

    def test_unknown_setting_shows_more_not_less(self):
        self.set_mode("quiet")
        self.assertEqual(receipts.setting(), "all")

    def test_resume_keeps_the_cursor(self):
        self.start(used=True)
        self.append(ROWS["read"])
        self.start(source="resume", used=True)
        self.assertIn("hpc-jobs", self.tool())


class TestStartEvent(Env):
    def test_start_is_silent(self):
        self.assertIsNone(self.start())
        (self.config / "sites" / "alpha.toml").write_text("")
        self.assertIsNone(self.start(session="S2"))

    def test_start_records_where_the_plugin_lives(self):
        self.start()
        root = Path(receipts.plugin_root_file()).read_text().strip()
        self.assertEqual(root, str(Path(receipts.__file__).resolve().parent.parent))


class TestFirstUse(Env):
    """The status shows once per session, at the first use: the skill or the
    first cluster command, whichever comes first."""

    def setUp(self):
        super().setUp()
        (self.config / "sites" / "alpha.toml").write_text("")
        self.line = mock.patch.object(receipts, "site_line",
                                      lambda n: n + ": profile observe · connected")
        self.line.start()

    def tearDown(self):
        self.line.stop()
        super().tearDown()

    def test_skill_invocation_shows_the_status(self):
        self.start()
        self.assertEqual(self.skill(), "hpc-tether ▸ active · alpha: profile observe · "
                                       "connected · receipts: all (set in %s)"
                                       % (self.config / "config.toml"))
        self.assertIsNone(self.skill())

    def test_other_skills_are_not_a_use(self):
        self.start()
        self.assertIsNone(self.skill(name="paper-voice"))
        self.assertEqual(self.bar(), "")

    def test_first_cluster_command_brings_the_status_then_receipts(self):
        self.start()
        self.append(ROWS["read"])
        out = self.tool().splitlines()
        self.assertIn("alpha: profile observe", out[0])
        self.assertIn("hpc-jobs", out[1])
        self.append(ROWS["read"])
        self.assertNotIn("alpha:", self.tool())

    def test_after_the_skill_a_command_gets_receipts_only(self):
        self.start()
        self.skill()
        self.append(ROWS["read"])
        self.assertNotIn("alpha:", self.tool())

    def test_status_alone_when_notable_hides_the_receipt(self):
        self.set_mode("notable")
        self.start()
        self.append(ROWS["read"])
        self.assertIn("alpha: profile observe", self.tool())

    def test_off_shows_nothing(self):
        self.set_mode("off")
        self.start()
        self.assertIsNone(self.skill())
        self.assertEqual(self.bar(), "")

    def test_several_sites_one_line_each(self):
        (self.config / "sites" / "beta.toml").write_text("")
        out = self.skill()
        self.assertEqual(out.splitlines()[1:], ["  alpha: profile observe · connected",
                                                "  beta: profile observe · connected"])

    def test_no_site_file_said_at_first_use(self):
        (self.config / "sites" / "alpha.toml").unlink()
        self.assertIsNone(self.start())
        self.assertIn("loaded, but no site file", self.skill())


class TestStatusBar(Env):
    def setUp(self):
        super().setUp()
        (self.config / "sites" / "alpha.toml").write_text("")
        self.calls = []

        def line(n):
            self.calls.append(n)
            return n + ": profile operate · connected"
        self.line = mock.patch.object(receipts, "site_line", line)
        self.line.start()

    def tearDown(self):
        self.line.stop()
        super().tearDown()

    def test_empty_until_used(self):
        self.start()
        self.assertEqual(self.bar(), "")
        self.assertEqual(self.calls, [])

    def test_shows_the_status_once_used(self):
        self.start()
        self.skill()
        self.assertEqual(self.bar(), "hpc-tether ▸ alpha: profile operate · connected")

    def test_only_for_the_session_that_used_it(self):
        self.start()
        self.skill()
        self.assertEqual(self.bar(session="OTHER"), "")

    def test_reuses_the_check_for_a_while(self):
        self.start()
        self.skill()
        self.calls.clear()
        self.bar(); self.bar()
        self.assertEqual(self.calls, ["alpha"])
        with mock.patch.object(receipts, "STATUS_TTL", 0):
            self.bar()
        self.assertEqual(self.calls, ["alpha", "alpha"])

    def test_command_line_mode_prints_the_bar(self):
        self.start()
        receipts.mark_announced("S1")
        with mock.patch.object(receipts.sys, "argv", ["receipts.py", "--statusline"]), \
                mock.patch.object(receipts.sys, "stdin", io.StringIO('{"session_id": "S1"}')), \
                mock.patch.object(receipts.sys, "stdout", io.StringIO()) as out:
            receipts.main()
        self.assertEqual(out.getvalue(), "hpc-tether ▸ alpha: profile operate · connected")


class TestUnderSystemPython(Env):
    """The hook runs under whatever python3 Claude Code finds: 3.9 on macOS."""

    def test_end_to_end(self):
        pythons = sorted({p for p in ("/usr/bin/python3", shutil.which("python3"))
                          if p and os.path.exists(p)})
        self.start()
        for py in pythons:
            with self.subTest(python=py):
                self.append(ROWS["write"])
                event = {"hook_event_name": "PostToolUse", "session_id": "S1",
                         "tool_name": "Bash", "tool_input": {"command": "hpc-run -"}}
                p = subprocess.run([py, str(HOOK)], input=json.dumps(event),
                                   capture_output=True, text=True, env=dict(os.environ))
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn("approved --allow-write", json.loads(p.stdout)["systemMessage"])

    def test_garbage_in_is_silent(self):
        p = subprocess.run(["/usr/bin/python3" if os.path.exists("/usr/bin/python3") else "python3",
                            str(HOOK)], input="not json", capture_output=True, text=True)
        self.assertEqual((p.returncode, p.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
