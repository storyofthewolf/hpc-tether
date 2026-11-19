"""The approval hook: every hpc-tether token goes to the user."""

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "approve_hpc_writes.py"
spec = importlib.util.spec_from_file_location("approve", HOOK)
approve = importlib.util.module_from_spec(spec)
spec.loader.exec_module(approve)

BIN = "/Users/u/.claude/skills/hpc-tether/bin"


def decide(command, mode="auto", tool="Bash"):
    out = approve.decide({"tool_name": tool, "tool_input": {"command": command},
                          "permission_mode": mode})
    return out and out["hookSpecificOutput"]["permissionDecision"]


class TestAsks(unittest.TestCase):
    def test_every_token_on_every_verb_asks(self):
        for cmd in (f"printf 'mkdir $HPC_SCRATCH/x\\n' | {BIN}/hpc-run --allow-write -",
                    f"{BIN}/hpc-run - --allow-exec < /dev/null",
                    f"{BIN}/hpc-push f.csv --yes",
                    f"{BIN}/hpc-push f.csv --to scratch:f.csv --yes --force",
                    f"{BIN}/hpc-pull rundir:x.nc --budget-add 10G",
                    f"{BIN}/hpc-pull rundir:x.nc --force-size",
                    f"{BIN}/hpc-fetch case:x out --merge",
                    f"{BIN}/hpc-fetch case:x out --overwrite",
                    f"cd /x && python3 {BIN}/hpc-run --allow-write - <<'EOF'\nmkdir a\nEOF",
                    f"timeout 60 {BIN}/hpc-run --allow-write -",
                    "bin/hpc-run --allow-write -",
                    "hpc-run --allow-write=1 -"):
            with self.subTest(cmd=cmd):
                self.assertEqual(decide(cmd), "ask")

    def test_reason_names_each_token(self):
        out = approve.decide({"tool_name": "Bash", "permission_mode": "default",
                              "tool_input": {"command": f"{BIN}/hpc-run --allow-write --allow-exec -"}})
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("--allow-write", reason)
        self.assertIn("--allow-exec", reason)

    def test_asks_in_every_interactive_mode(self):
        for mode in ("default", "acceptEdits", "auto", "plan"):
            with self.subTest(mode=mode):
                self.assertEqual(decide(f"{BIN}/hpc-push f --yes", mode), "ask")


class TestSilent(unittest.TestCase):
    def test_reads_and_trusted_scripts_never_prompt(self):
        for cmd in (f"{BIN}/hpc-check",
                    f"{BIN}/hpc-jobs --job 123",
                    f"{BIN}/hpc-read rundir:atm.log --tail 50",
                    f"printf 'python $HPC_REPO/my-script.py check A\\n' | {BIN}/hpc-run -",
                    f"{BIN}/hpc-pull rundir:x.nc"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(decide(cmd))

    def test_token_words_elsewhere_are_not_tokens(self):
        for cmd in ("echo --yes",
                    "rg '--allow-exec' .",
                    "some-tool --force",
                    # A payload's own text: a trusted script's --force and a
                    # literal --yes are the payload's, not the verb's.
                    f"{BIN}/hpc-run - <<'EOF'\necho --yes --allow-write\nEOF",
                    f"{BIN}/hpc-run - <<EOF\npython $HPC_REPO/datamgr.py purge --force\nEOF"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(decide(cmd))

    def test_any_wrapping_asks(self):
        """The hook matches, it does not parse."""
        for cmd in ("bash -c 'hpc-run --allow-write -'",
                    "env -i hpc-push --yes f",
                    "bash <<'EOF'\nhpc-push --yes f\nEOF",
                    "cat <<EOF | sh\nbin/hpc-run --allow-exec -\nEOF",
                    "cat <<EOF\n$(hpc-push --yes f)\nEOF",
                    "xargs -I{} bin/hpc-push {} --yes < list",
                    'T=--allow-write; bin/hpc-run $T -',
                    'bin/hpc-run --allow-"write" -',
                    "bin/hpc-ru? --allow-write -",
                    "V=hpc-run; bin/$V --allow-exec -",
                    "printf 'x\\n' | sudo -u me bin/hpc-run --allow-write -"):
            with self.subTest(cmd=cmd):
                self.assertEqual(decide(cmd), "ask")
                self.assertEqual(decide(cmd, "bypassPermissions"), "deny")

    def test_a_mention_costs_a_prompt(self):
        """The price of matching: text that only talks about a token asks too."""
        self.assertEqual(decide("grep -- --allow-write bin/hpc-run"), "ask")
        self.assertEqual(decide("git commit -m 'hpc-run --allow-write'"), "ask")

    def test_other_tools_are_ignored(self):
        self.assertIsNone(decide(f"{BIN}/hpc-push f --yes", tool="Edit"))


class TestNoOneToAsk(unittest.TestCase):
    def test_bypass_mode_denies(self):
        self.assertEqual(decide(f"{BIN}/hpc-run --allow-write -", "bypassPermissions"), "deny")

    def test_bypass_denial_names_what_the_command_does(self):
        # A push with --to that would create a file was refused
        # as "replace a file on the cluster".
        out = approve.decide({"tool_name": "Bash", "permission_mode": "bypassPermissions",
                              "tool_input": {"command": f"{BIN}/hpc-push z.txt --to scratch:hpc-test/z.txt --yes"}})
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("will write", reason)
        self.assertNotIn("replace a file", reason)
        self.assertIn("(--yes)", reason)

    def test_bypass_mode_still_silent_for_reads(self):
        self.assertIsNone(decide(f"{BIN}/hpc-jobs", "bypassPermissions"))


class TestAsAHook(unittest.TestCase):
    """Run as Claude Code runs it: JSON on stdin, JSON on stdout, exit 0."""

    def run_hook(self, stdin):
        return subprocess.run([sys.executable, str(HOOK)], input=stdin,
                              capture_output=True, text=True)

    def test_ask_round_trip(self):
        p = self.run_hook(json.dumps({"tool_name": "Bash", "permission_mode": "auto",
                                      "tool_input": {"command": f"{BIN}/hpc-push f --yes"}}))
        self.assertEqual(p.returncode, 0)
        out = json.loads(p.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "ask")

    def test_silent_is_empty_and_exit_zero(self):
        p = self.run_hook(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}))
        self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_garbage_input_is_silent(self):
        self.assertEqual(self.run_hook("not json").returncode, 0)


class TestPlainWords(unittest.TestCase):
    """The prompt says what the command will do, above the command itself."""

    def setUp(self):
        import os
        import shutil
        import tempfile
        from unittest import mock
        self.tmp = tempfile.mkdtemp()
        sites = Path(self.tmp) / "sites"
        sites.mkdir()
        example = HOOK.parent.parent / "skills" / "hpc-tether" / "sites" / "example.toml"
        shutil.copy(example, sites / "example.toml")
        self.env = mock.patch.dict(os.environ, {"HPC_TETHER_SITES": str(sites),
                                                "HPC_TETHER_AUDIT": str(Path(self.tmp) / "a.jsonl")})
        self.env.start()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.addCleanup(self.env.stop)

    def reason(self, command):
        out = approve.decide({"tool_name": "Bash", "tool_input": {"command": command},
                              "permission_mode": "auto"})
        return out["hookSpecificOutput"]["permissionDecisionReason"]

    def test_heredoc_payload_described_by_the_guard(self):
        r = self.reason(f"{BIN}/hpc-run --allow-write - <<'EOF'\n"
                        "mkdir -p $HPC_SCRATCH/new\n"
                        "mv -n $HPC_SCRATCH/a.nc $HPC_SCRATCH/old/a.nc\n"
                        "squeue -u $USER\nEOF")
        first, second = r.split("\n")
        self.assertEqual(first, "hpc-tether: this will create directory scratch:new; "
                                "move scratch:a.nc → scratch:old/a.nc (no overwrite) "
                                "(+1 read-only line).")
        self.assertTrue(second.startswith("Approving --allow-write: change files"))

    def test_job_submission_from_a_substitution(self):
        r = self.reason(f"{BIN}/hpc-run --allow-write - <<'EOF'\n"
                        "JID=$(sbatch --parsable -J run_A $HPC_SCRATCH/job.sh)\necho $JID\nEOF")
        self.assertIn("will submit job run_A (job.sh)", r)

    def test_printf_payload(self):
        r = self.reason(f"printf 'scancel 4242\\n' | {BIN}/hpc-run --allow-write -")
        self.assertIn("will cancel job 4242", r)

    def test_echo_payload(self):
        """The form agents write most."""
        r = self.reason(f"echo 'touch \"$HPC_SCRATCH/hpc-test/a.txt\"' | {BIN}/hpc-run --allow-write -")
        self.assertIn("will touch scratch:hpc-test/a.txt", r)

    def test_printf_lines_payload(self):
        """printf '%s\\n' with one quoted argument per line."""
        r = self.reason(f"printf '%s\\n' 'cp $HPC_SCRATCH/a $HPC_SCRATCH/b' "
                        f"'mv $HPC_SCRATCH/b $HPC_SCRATCH/c' | {BIN}/hpc-run --allow-write -")
        self.assertIn("will copy scratch:a → scratch:b; move scratch:b → scratch:c", r)

    def test_payload_refused_whole_needs_no_prompt(self):
        """The guard refuses it locally and sends nothing."""
        for cmd in (f"echo 'touch /scratch/projects/x' | {BIN}/hpc-run --allow-write -",
                    f"printf '%s\\n' 'touch /x/y' 'mkdir $HPC_SCRATCH/a' | {BIN}/hpc-run --allow-write -"):
            self.assertIsNone(decide(cmd), cmd)

    def test_a_second_payload_still_prompts(self):
        cmd = (f"echo 'touch /x/y' | {BIN}/hpc-run --allow-write - ; "
               f"echo 'mkdir $HPC_SCRATCH/a' | {BIN}/hpc-run --allow-write -")
        self.assertIn("refused by the guard, never sent: touch /x/y", self.reason(cmd))

    def test_refused_lines_are_named(self):
        # Refused whole, so no prompt at all; with a second payload the
        # refused line is named (test_a_second_payload_still_prompts).
        self.assertIsNone(decide(f"{BIN}/hpc-run --allow-write - <<'EOF'\nrm -f $HPC_SCRATCH/x\nEOF"))

    def test_unreadable_payload_still_asks(self):
        r = self.reason(f"cat job.sh | {BIN}/hpc-run --allow-write -")
        self.assertIn("can't read from the command line", r)
        self.assertIn("Approving --allow-write", r)

    def test_push_names_target_and_backup(self):
        # --to may name a new file, which "replace" would misdescribe.
        self.assertIn("will write local f.csv to scratch:f.csv (if a file is already there, "
                      "it is backed up first)",
                      self.reason(f"{BIN}/hpc-push f.csv --to scratch:f.csv --yes"))
        # The prompt read "this replace the cluster file ...".
        self.assertIn("this will replace the cluster file it was pulled from with local f.csv",
                      self.reason(f"{BIN}/hpc-push f.csv --yes"))
        self.assertIn("even though its drift can't be checked",
                      self.reason(f"{BIN}/hpc-push f.csv --yes --force"))

    def test_transfers(self):
        self.assertIn("pull archive:x.nc past the 2 GiB per-transfer cap",
                      self.reason(f"{BIN}/hpc-pull archive:x.nc --force-size"))
        self.assertIn("fetch directory case:A into out, overwriting local files",
                      self.reason(f"{BIN}/hpc-fetch case:A out --overwrite"))
        self.assertIn("raise this session's transfer budget by 4G for rundir:x",
                      self.reason(f"{BIN}/hpc-pull rundir:x --budget-add 4G"))

    def test_summary_failure_never_blocks_the_prompt(self):
        from unittest import mock
        with mock.patch.object(approve, "describe", side_effect=RuntimeError("boom")):
            r = self.reason(f"{BIN}/hpc-push f.csv --yes")
        self.assertTrue(r.startswith("Approving --yes"))


if __name__ == "__main__":
    unittest.main()


class TestPluginDirectoryIsNotATool(unittest.TestCase):
    """`hpc-tether` is the plugin's directory. A command that only names it
    beside another program's --force or --yes asked for approval to
    "overwrite a cluster file"."""

    def _used(self, command):
        import importlib.util
        spec = importlib.util.spec_from_file_location("approve_hook", HOOK)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod._tokens_used(command)

    def test_directory_name_alone_draws_no_prompt(self):
        for c in ("cd ~/Tools/hpc-tether && git push --force",
                  "git -C ~/tools/hpc-tether push --force origin main",
                  "brew upgrade --force && ls ~/.config/hpc-tether/",
                  'grep -rn -- --yes "/x/hpc-tether"'):
            with self.subTest(command=c):
                self.assertEqual(self._used(c), [])

    def test_a_tool_under_that_directory_still_asks(self):
        for c, tok in (("~/tools/hpc-tether/skills/hpc-tether/bin/hpc-push f --yes", "--yes"),
                       ("cd ~/tools/hpc-tether && bin/hpc-run --allow-write - <x", "--allow-write"),
                       ("~/tools/hpc-tether/bin/hpc-* --force f", "--force"),
                       ("hpc-tether-old/bin/tool --force", "--force")):
            with self.subTest(command=c):
                self.assertEqual(self._used(c), [tok])
