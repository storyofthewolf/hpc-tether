"""Guard classifier tests: the tiers are the contract, so each one is pinned."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hpctether import guard, shell

SCRATCH = "/scratch/tester"
WRITABLE = "/gpfs/fs01/tester"
HOME = "/home/tester"
ROOTS = [SCRATCH, WRITABLE, HOME]
# A payload with no cd runs in the login directory, the `home` role; it is a
# write root here so a bare relative write (`touch marker`) is confined.
PATHS = {"scratch": SCRATCH, "home": HOME}


def worst(script: str) -> str | None:
    return guard.classify(script, roots=ROOTS, scratch=SCRATCH, paths=PATHS).worst()


class TestReadTier(unittest.TestCase):
    def test_plain_reads_pass(self):
        script = "\n".join(
            [
                'squeue -u "$HPC_USER"',
                "cat /etc/motd",
                "tail -n 50 run.log",
                "git status",
                "git log --oneline -5",
                "ls -l $HPC_RUNDIR",
                "du -s $HPC_CASE/case42",
                "find $HPC_RUNDIR -maxdepth 2 -name '*.nc'",
                "grep ERROR atm.log",
            ]
        )
        self.assertIsNone(worst(script))

    def test_comments_and_blanks_ignored(self):
        self.assertIsNone(worst("# rm -rf /\n\n  # find / -name x\n"))

    def test_safe_redirects_pass(self):
        self.assertIsNone(worst("squeue 2>/dev/null\nls >&2\ncat f > /dev/null"))


class TestSubshellClose(unittest.TestCase):
    """The `)` closing a subshell stuck to the last word."""

    def test_pipeline_in_a_subshell_is_a_read(self):
        self.assertIsNone(worst("(seq 3 | head -1)\n(yes | head -1)\n"))

    def test_yes_is_a_read_and_its_redirect_a_write(self):
        self.assertIsNone(worst("yes | head -1\n"))
        self.assertEqual(worst("yes > $HPC_SCRATCH/x\n"), guard.WRITE)

    def test_write_target_loses_the_paren(self):
        self.assertEqual(shell.tokenize("touch $HPC_SCRATCH/x)"), ["touch", "$HPC_SCRATCH/x"])
        self.assertEqual(worst("(touch $HPC_SCRATCH/x)\n"), guard.WRITE)

    def test_subshell_does_not_soften_a_refusal(self):
        for script in ("(rm $HPC_SCRATCH/x)", "(ls; rm /x)", "(touch /etc/x)"):
            with self.subTest(script=script):
                self.assertEqual(worst(script + "\n"), guard.FORBIDDEN)

    def test_quoted_paren_is_kept(self):
        self.assertEqual(shell.tokenize('echo ")"'), ["echo", ")"])


class TestWriteTier(unittest.TestCase):
    def test_sbatch_scancel(self):
        self.assertEqual(worst("sbatch job.sh"), guard.WRITE)
        self.assertEqual(worst("scancel 12345"), guard.WRITE)

    def test_git_writes(self):
        self.assertEqual(worst("git pull --ff-only"), guard.WRITE)
        self.assertEqual(worst("git push origin main"), guard.WRITE)
        self.assertEqual(worst("git checkout main"), guard.WRITE)

    def test_redirect_to_file(self):
        self.assertEqual(worst("squeue > queue.txt"), guard.WRITE)
        self.assertEqual(worst("echo hi >>log.txt"), guard.WRITE)

    def test_mkdir_touch_cp(self):
        self.assertEqual(worst("mkdir -p $HPC_SCRATCH/x"), guard.WRITE)
        self.assertEqual(worst("touch marker"), guard.WRITE)
        self.assertEqual(worst("cp a b"), guard.WRITE)

    def test_mv_confined(self):
        self.assertEqual(
            worst("mv $HPC_SCRATCH/a $HPC_SCRATCH/b"), guard.WRITE
        )

    def test_chmod_plain(self):
        self.assertEqual(worst("chmod +x lib/run.sh"), guard.WRITE)

    def test_scontrol_update(self):
        self.assertEqual(worst("scontrol hold 123"), guard.WRITE)
        self.assertIsNone(worst("scontrol show job 123"))


class TestForbiddenTier(unittest.TestCase):
    def test_rm_outside_scratch(self):
        self.assertEqual(worst("rm -rf $HOME/project"), guard.FORBIDDEN)
        self.assertEqual(worst("rm /gpfs/fs01/shared/x.nc"), guard.FORBIDDEN)

    def test_rm_mixed_paths(self):
        self.assertEqual(
            worst("rm $HPC_SCRATCH/ok $HOME/not-ok"), guard.FORBIDDEN
        )

    def test_rm_bare(self):
        self.assertEqual(worst("rm -rf"), guard.FORBIDDEN)

    def test_mv_outside_scratch(self):
        self.assertEqual(worst("mv $HPC_CASE/a $HPC_CASE/b"), guard.FORBIDDEN)

    def test_git_destructive(self):
        self.assertEqual(worst("git reset --hard HEAD~1"), guard.FORBIDDEN)
        self.assertEqual(worst("git push --force origin main"), guard.FORBIDDEN)
        self.assertEqual(worst("git push -f"), guard.FORBIDDEN)
        self.assertEqual(worst("git clean -fd"), guard.FORBIDDEN)

    def test_always_forbidden_commands(self):
        for cmd in ("dd if=/dev/zero of=x", "truncate -s 0 f", "chown me f",
                    "shred f", "mkfs.ext4 /dev/sda1"):
            self.assertEqual(worst(cmd), guard.FORBIDDEN, cmd)

    def test_recursive_chmod(self):
        self.assertEqual(worst("chmod -R 755 dir"), guard.FORBIDDEN)

    def test_find_delete_exec(self):
        self.assertEqual(worst("find . -maxdepth 1 -delete"), guard.FORBIDDEN)
        self.assertEqual(
            worst("find . -maxdepth 1 -exec rm {} \\;"), guard.FORBIDDEN
        )

    def test_forbidden_beats_write_flag_semantics(self):
        report = guard.classify("sbatch j.sh\nrm -rf $HOME/x", roots=ROOTS, scratch=SCRATCH)
        self.assertEqual(report.worst(), guard.FORBIDDEN)
        self.assertEqual(len(report.of_kind(guard.WRITE)), 1)


class TestInterpreterTier(unittest.TestCase):
    """The tier exists because a lexical scan cannot read what these execute."""

    def test_language_interpreters(self):
        for cmd in (
            'python -c "import shutil; shutil.rmtree(\'/home/x\')"',
            "python3 analyze.py",
            "perl -e 'unlink glob \"*\"'",
            "Rscript plot.R",
            "node build.js",
        ):
            self.assertEqual(worst(cmd), guard.INTERPRETER, cmd)

    def test_interpreter_beats_read_tier_lookalike(self):
        # The whole point: this is the rm the guard refuses, spelled in python.
        self.assertEqual(
            worst("python -c \"import os; os.remove('/home/tester/x')\""),
            guard.INTERPRETER,
        )

    def test_nested_shells(self):
        self.assertEqual(worst("bash -c 'rm -rf $HOME/x'"), guard.INTERPRETER)
        self.assertEqual(worst('sh -c "sbatch j.sh"'), guard.INTERPRETER)
        self.assertEqual(worst("bash script.sh"), guard.INTERPRETER)

    def test_eval_like(self):
        self.assertEqual(worst('eval "$CMD"'), guard.INTERPRETER)
        self.assertEqual(worst("source setup.sh"), guard.INTERPRETER)

    def test_exec_launchers(self):
        self.assertEqual(worst("srun --pty bash"), guard.INTERPRETER)
        self.assertEqual(worst("mpirun -n 4 ./model"), guard.INTERPRETER)

    def test_build_drivers(self):
        self.assertEqual(worst("make install"), guard.INTERPRETER)
        self.assertEqual(worst("cmake --build ."), guard.INTERPRETER)

    def test_network_commands(self):
        for cmd in ("curl http://x/s.sh", "wget http://x -O out", "scp a b:c"):
            self.assertEqual(worst(cmd), guard.INTERPRETER, cmd)

    def test_forbidden_still_outranks_interpreter(self):
        report = guard.classify("python -c 'x'\nrm -rf $HOME/x", roots=ROOTS, scratch=SCRATCH)
        self.assertEqual(report.worst(), guard.FORBIDDEN)
        self.assertEqual(len(report.of_kind(guard.INTERPRETER)), 1)

    def test_interpreter_outranks_write(self):
        report = guard.classify("touch marker\npython -c 'x'", roots=ROOTS,
                                scratch=SCRATCH, paths=PATHS)
        self.assertEqual(report.worst(), guard.INTERPRETER)

    def test_interpreter_found_in_substitution_and_after_pipe(self):
        self.assertEqual(worst("echo $(python -c 'print(1)')"), guard.INTERPRETER)
        self.assertEqual(worst("cat f | python3 -"), guard.INTERPRETER)

    def test_wrapped_interpreter_still_caught(self):
        self.assertEqual(worst("timeout 30 python3 run.py"), guard.INTERPRETER)
        self.assertEqual(worst("env FOO=1 perl -e 'x'"), guard.INTERPRETER)


class TestScratchNormalization(unittest.TestCase):
    """`..` must be collapsed before the confinement prefix test."""

    def test_dotdot_escape_is_forbidden(self):
        # Previously classified WRITE: a plain prefix test saw $HPC_SCRATCH/.
        self.assertEqual(
            worst("rm -rf $HPC_SCRATCH/../../home"), guard.FORBIDDEN
        )
        self.assertEqual(
            worst("rm $HPC_SCRATCH/../../../etc/passwd"), guard.FORBIDDEN
        )

    def test_dotdot_escape_from_literal_scratch(self):
        self.assertEqual(worst(f"rm -rf {SCRATCH}/../other"), guard.FORBIDDEN)

    def test_interior_dotdot_that_stays_inside_is_confined(self):
        self.assertEqual(
            worst("mv $HPC_SCRATCH/a/../b $HPC_SCRATCH/c"), guard.WRITE
        )
        self.assertEqual(worst(f"mv {SCRATCH}/a/../b {SCRATCH}/c"), guard.WRITE)

    def test_trailing_slash_and_dot_forms(self):
        self.assertEqual(worst("mv $HPC_SCRATCH/./old.nc $HPC_SCRATCH/new.nc"), guard.WRITE)
        self.assertEqual(worst(f"mv {SCRATCH}/sub/ {SCRATCH}/sub2"), guard.WRITE)

    def test_scratch_root_itself_still_write(self):
        self.assertEqual(worst("mv $HPC_SCRATCH/a $HPC_SCRATCH"), guard.WRITE)

    def test_lookalike_sibling_path_not_confined(self):
        # /scratch/tester2 must not match the /scratch/tester prefix.
        self.assertEqual(worst("rm -rf /scratch/tester2/x"), guard.FORBIDDEN)


class TestTraversalTier(unittest.TestCase):
    def test_find_without_maxdepth(self):
        self.assertEqual(worst("find $HPC_RUNDIR -name '*.nc'"), guard.TRAVERSAL)

    def test_du_unbounded(self):
        self.assertEqual(worst("du $HPC_RUNDIR"), guard.TRAVERSAL)
        self.assertIsNone(worst("du -sh $HPC_RUNDIR"))
        self.assertIsNone(worst("du --max-depth=1 $HPC_RUNDIR"))

    def test_ls_recursive(self):
        self.assertEqual(worst("ls -lR $HPC_RUNDIR"), guard.TRAVERSAL)

    def test_grep_recursive(self):
        self.assertEqual(worst("grep -r ERROR $HPC_CASE"), guard.TRAVERSAL)
        self.assertEqual(worst("grep -Rn ERROR ."), guard.TRAVERSAL)
        self.assertIsNone(worst("grep -n ERROR atm.log"))


class TestSegments(unittest.TestCase):
    def test_violation_after_pipe(self):
        self.assertEqual(worst("cat f | tee out.txt"), guard.WRITE)

    def test_violation_after_and(self):
        self.assertEqual(worst("cd $HPC_SCRATCH && rm $HOME/x"), guard.FORBIDDEN)

    def test_command_substitution_scanned(self):
        self.assertEqual(worst("echo $(sbatch j.sh)"), guard.WRITE)

    def test_substitution_with_inner_parens_scanned(self):
        # Regression: a `[^()]*` regex stopped at the inner paren and dropped
        # the whole substitution, so its contents were never classified.
        self.assertEqual(
            worst("echo $(python -c 'print(1)')"), guard.INTERPRETER
        )
        self.assertEqual(
            worst("X=$(rm -rf $HOME/x && echo (done))"), guard.FORBIDDEN
        )

    def test_nested_substitution_scanned(self):
        self.assertEqual(
            worst("echo $(echo $(sbatch j.sh))"), guard.WRITE
        )

    def test_backtick_substitution_scanned(self):
        self.assertEqual(worst("echo `sbatch j.sh`"), guard.WRITE)

    def test_substitution_body_pipeline_scanned(self):
        self.assertEqual(worst("echo $(cat f | tee out.txt)"), guard.WRITE)

    def test_wrapper_stripped(self):
        self.assertEqual(worst("timeout 30 sbatch j.sh"), guard.WRITE)
        self.assertEqual(worst("env FOO=1 rm $HOME/x"), guard.FORBIDDEN)

    def test_no_roots_defined(self):
        # With no roots there is no site context, and write confinement fails
        # closed: every write is refused, $HPC_SCRATCH spelling included
        # (it used to pass as write-tier).
        for script in ("mv $HPC_SCRATCH/x $HPC_SCRATCH/y", "mv /anywhere/x /anywhere/y",
                       "mkdir -p $HPC_SCRATCH/x", "echo hi > $HPC_SCRATCH/f"):
            with self.subTest(script=script):
                report = guard.classify(script, roots=None, scratch=SCRATCH)
                self.assertEqual(report.worst(), guard.FORBIDDEN)

    # ---- writable roots ------------------------------------
    #
    # A rename inside the user's own archive was forbidden outright, stopping
    # a remote-control session until the user reached a physical terminal.
    # Inside a declared writable root these are write-tier; outside, forbidden.

    def test_mv_inside_writable_root_is_write_tier(self):
        self.assertEqual(
            worst(f"mv {WRITABLE}/archive/f_temp.nc {WRITABLE}/archive/f.nc"),
            guard.WRITE,
        )

    def test_rm_inside_writable_root_is_still_forbidden(self):
        # A wrong move is undone by moving back, a wrong rm is not.
        self.assertEqual(worst(f"rm {WRITABLE}/archive/stale.nc"), guard.FORBIDDEN)

    def test_mv_outside_writable_root_still_forbidden(self):
        self.assertEqual(worst("mv /gpfs/fs01/someone/a /gpfs/fs01/someone/b"),
                         guard.FORBIDDEN)
        self.assertEqual(worst(f"mv {WRITABLE}/a /etc/passwd"), guard.FORBIDDEN)

    def test_partial_prefix_is_not_confined(self):
        # A sibling directory sharing a name prefix must not be swept in.
        self.assertEqual(worst("rm /gpfs/fs01/tester2/x"), guard.FORBIDDEN)

    def test_traversal_out_of_writable_root_is_forbidden(self):
        self.assertEqual(worst(f"rm {WRITABLE}/../../etc/x"), guard.FORBIDDEN)

    def test_mixed_paths_take_the_stricter_tier(self):
        # One unconfined argument forbids the whole command.
        self.assertEqual(worst(f"mv {WRITABLE}/a /gpfs/fs01/other/b"),
                         guard.FORBIDDEN)

    # ---- mv refusal names the real cause --------------------
    #
    # `mv $D/a $D/b` with D inside a writable root was refused as "outside
    # the site's writable roots"; the agent then told the user to widen
    # writable_roots. The tier stays forbidden; the message must say why.

    def _mv_message(self, script: str) -> str:
        report = guard.classify(script, roots=ROOTS, scratch=SCRATCH)
        self.assertEqual(report.worst(), guard.FORBIDDEN)
        return next(v.message for v in report.violations if "mv " in v.message)

    def test_mv_unexpanded_variable_says_so(self):
        msg = self._mv_message(
            f"D={WRITABLE}/models\nmv $D/a.nc $D/b.nc"
        )
        self.assertIn("'$D/a.nc'", msg)
        self.assertIn("literal absolute path", msg)
        self.assertIn("writable_roots", msg)
        self.assertNotIn("outside the site's writable roots", msg)

    def test_mv_relative_path_says_so(self):
        msg = self._mv_message(f"cd {WRITABLE}/models\nmv a.nc b.nc")
        self.assertIn("is relative", msg)
        self.assertNotIn("outside the write roots", msg)

    def test_mv_truly_outside_lists_roots(self):
        msg = self._mv_message("mv /gpfs/fs01/someone/a /gpfs/fs01/someone/b")
        self.assertIn("outside the write roots", msg)
        self.assertIn(WRITABLE, msg)
        self.assertIn("'/gpfs/fs01/someone/a'", msg)

    def test_refusal_lists_only_outermost_roots(self):
        # Both /x and /x/tmp were listed. Display only: the
        # nested root still confines.
        roots = ("/gpfs/fs01/tester", "/gpfs/fs01/tester/tmp", "/gpfs/fs01/tester2")
        report = guard.classify("mv /gpfs/fs01/someone/a /gpfs/fs01/someone/b", roots=roots)
        msg = next(v.message for v in report.violations if "mv " in v.message)
        self.assertIn("(/gpfs/fs01/tester, /gpfs/fs01/tester2)", msg)
        self.assertNotIn("tester/tmp", msg)

    def test_mv_traversal_says_dotdot(self):
        # `..` is collapsed, and the message names where the path really lands.
        msg = self._mv_message(f"mv {WRITABLE}/../x {WRITABLE}/y")
        self.assertIn("/gpfs/fs01/x is outside the write roots", msg)
        # A `..` that can't be resolved at all says so.
        msg = self._mv_message(f"mv a/../../x {WRITABLE}/y")
        self.assertIn("'..'", msg)

    def test_mv_literal_rewrite_is_write_tier(self):
        # The retry the corrected message asks for must actually pass.
        self.assertEqual(
            worst(f"mv -n {WRITABLE}/models/a.nc {WRITABLE}/models/b.nc"),
            guard.WRITE,
        )

    # ---- quote-aware segment splitting ----------
    #
    # `_SEGMENT_SPLIT_RE.split` was quote-blind, so a grep alternation such as
    # "rm -rf\|if \[ -d" shattered into fragments like `grep -n "rm -rf\`.
    # The unterminated quote left `-rf` reading as short flags, and the guard
    # refused a read-only grep as a "recursive grep". It refused real commands
    # repeatedly in a live session before being traced.

    def test_quoted_pipe_in_grep_pattern_is_clean(self):
        # The literal text "rm -rf" inside a quoted PATTERN is not an rm, and
        # the \| alternations are not pipes.
        seg = 'grep -n "rm -rf\\|if \\[ -d\\|already exists" /p/build.sh | head -10'
        self.assertIsNone(worst(seg))

    def test_quoted_alternation_not_read_as_flags(self):
        seg = 'grep -n "foo_option\\|bar_params" /p/build.py | head'
        self.assertIsNone(worst(seg))

    def test_quoted_separator_does_not_hide_real_command(self):
        # The fix must only remove FALSE segments. A real separator outside
        # quotes still splits, so what follows is still classified.
        self.assertEqual(
            worst('grep -n "a\\|b" f.txt ; sbatch job.sh'), guard.WRITE
        )
        self.assertEqual(
            worst('grep "foo" f.txt ; rm -rf /gpfs/fs01/shared/models'),
            guard.FORBIDDEN,
        )
        self.assertEqual(
            worst('grep -n "x\\|y" f.txt | python3 -c "import os"'),
            guard.INTERPRETER,
        )

    def test_real_recursive_grep_still_caught(self):
        self.assertEqual(worst("grep -r foo /some/tree"), guard.TRAVERSAL)
        self.assertEqual(worst("grep -rn foo /some/tree"), guard.TRAVERSAL)

    def test_ordinary_pipeline_still_clean(self):
        self.assertIsNone(worst("zcat x.gz | grep foo | tail -1"))


class TestAlternateSpellings(unittest.TestCase):
    """A refused operation must stay refused however it is spelled.

    Every case here once reached READ tier. The guard is lexical,
    so its failure mode is not a wrong rule but an unexamined spelling: a
    prefix it stops on, an option whose value it mistakes for the verb. The
    class exists to make "what else spells this?" a standing question rather
    than something rediscovered by review.
    """

    def test_shell_keyword_prefix(self):
        # `shell.split_segments` leaves a segment starting with the keyword.
        for script in (
            "if true; then rm -rf /project/other; fi",
            "while true; do rm /project/other; done",
            "for f in *; do rm /project/$f; done",
        ):
            self.assertEqual(worst(script), guard.FORBIDDEN, script)

    def test_function_body(self):
        for script in (
            "f(){ rm -rf /project/other; }; f",
            "f() { rm -rf /project/other; }; f",
        ):
            self.assertEqual(worst(script), guard.FORBIDDEN, script)

    def test_end_of_options_marker(self):
        self.assertEqual(worst("command -- rm -rf /project/other"), guard.FORBIDDEN)

    def test_wrapper_flag_prefix(self):
        # `env FOO=1 rm` was caught; `env -i rm` was not -- the assignment form
        # was handled and the flag form was not.
        for script in (
            "env -i rm -rf /project/other",
            "nice -n 5 rm /project/other",
            "stdbuf -o0 rm /project/other",
            "timeout 30 rm -rf /project/other",
        ):
            self.assertEqual(worst(script), guard.FORBIDDEN, script)

    def test_wrapper_flag_value_not_read_as_command(self):
        # `xargs -n 1 rm /x` classified the command as `1`.
        for script in (
            "xargs -n 1 rm /project/other",
            "xargs -I{} rm /project/other",
        ):
            self.assertEqual(worst(script), guard.FORBIDDEN, script)

    def test_git_global_options_do_not_hide_subcommand(self):
        # `-C`'s value was returned as the subcommand, so the force-push and
        # reset--hard refusals never ran and both fell through to write tier.
        self.assertEqual(worst("git -C /repo push --force"), guard.FORBIDDEN)
        self.assertEqual(worst("git -c x=y reset --hard"), guard.FORBIDDEN)
        self.assertEqual(worst("git -C /repo clean -fd"), guard.FORBIDDEN)

    def test_force_push_via_refspec(self):
        # A leading '+' force-updates with no --force flag on the line.
        self.assertEqual(worst("git push origin +main:main"), guard.FORBIDDEN)

    def test_dual_mode_git_writes(self):
        for script in (
            "git config user.name x",
            "git branch -D main",
            "git remote add origin url",
            "git tag -d v1",
        ):
            self.assertEqual(worst(script), guard.WRITE, script)

    def test_dual_mode_git_reads_stay_read(self):
        for script in (
            "git config --get user.name",
            "git config --list",
            "git branch",
            "git branch --list",
            "git remote -v",
        ):
            self.assertIsNone(worst(script), script)

    def test_sed_in_place(self):
        # In-place editing is floor: forbidden at every profile.
        self.assertEqual(worst("sed -i s/a/b/ /project/shared/f"), guard.FORBIDDEN)
        self.assertEqual(worst("sed --in-place s/a/b/ /project/f"), guard.FORBIDDEN)
        # A read-only sed is still a read.
        self.assertIsNone(worst("sed -n 1,5p /project/f"))

    def test_find_execdir(self):
        self.assertEqual(
            worst("find /project -maxdepth 1 -execdir rm -rf {} +"), guard.FORBIDDEN
        )

    def test_path_invocation_is_opaque(self):
        for script in (
            "./unreviewed-executable",
            "./run_analysis.sh --flag",
            "bin/helper.py",
            "../tools/x",
            "$HOME/s.sh",
        ):
            self.assertEqual(worst(script), guard.INTERPRETER, script)

    def test_system_binaries_keep_their_real_tier(self):
        # A known command spelled with its full path is not an unknown file:
        # the basename still classifies it.
        self.assertIsNone(worst("/bin/ls -l"))
        self.assertIsNone(worst("/usr/bin/squeue -u me"))
        self.assertEqual(worst("/bin/rm /project/other"), guard.FORBIDDEN)


class TestQuotaReports(unittest.TestCase):
    """Quota reports are read tier."""

    def test_showquota_reads(self):
        for line in ("showquota -h\n", "showquota -a\n", "mmlsquota --block-size=auto\n"):
            with self.subTest(line=line):
                self.assertEqual(guard.classify(line).violations, [])


if __name__ == "__main__":
    unittest.main()


class TestDeletionForbiddenEverywhere(unittest.TestCase):
    """Deletion has no carve-out. Not in scratch, not under a
    flag, not through a wrapper. The user's own words: "those must always be
    blocked." Every case here previously reached WRITE or READ."""

    def test_scratch_is_not_a_carve_out(self):
        for script in (
            f"rm {SCRATCH}/junk.nc",
            "rm $HPC_SCRATCH/junk.nc",
            'rm -f "${HPC_SCRATCH}/a" $HPC_SCRATCH/b',
            "rm -rf $HPC_SCRATCH",
            "rm -rf $HPC_SCRATCH/*",
        ):
            self.assertEqual(worst(script), guard.FORBIDDEN, script)

    def test_other_deletion_verbs(self):
        for script in (
            f"rmdir {SCRATCH}/empty",
            f"unlink {SCRATCH}/link",
            f"shred -u {SCRATCH}/secret",
            "git clean -fdx",
            "find $HPC_SCRATCH -maxdepth 1 -delete",
        ):
            self.assertEqual(worst(script), guard.FORBIDDEN, script)

    def test_wrapped_rm_is_still_rm(self):
        # Each wrapper here previously took rm to READ tier.
        for script in (
            "setsid rm -rf /gpfs/fs01/tester/cases",
            "ionice -c3 rm -rf /gpfs/fs01/tester/cases",
            "ionice -c 3 rm -rf /gpfs/fs01/tester/cases",
            "flock /tmp/l rm -rf /gpfs/fs01/tester/cases",
            "flock -w 5 /tmp/l rm -rf /gpfs/fs01/tester/cases",
            "watch -n 5 rm -rf /gpfs/fs01/tester/x",
            "parallel rm -rf ::: /gpfs/fs01/tester/a /gpfs/fs01/tester/b",
            "taskset -c 0 rm /gpfs/fs01/tester/x",
            "nohup setsid nice rm /gpfs/fs01/tester/x",
        ):
            self.assertEqual(worst(script), guard.FORBIDDEN, script)

    def test_process_substitution_is_scanned(self):
        self.assertEqual(
            worst("diff <(ls /a) <(rm -rf /gpfs/fs01/tester/cases)"),
            guard.FORBIDDEN,
        )
        self.assertEqual(
            worst("tee >(rm -rf /gpfs/fs01/tester/cases) < /dev/null"),
            guard.FORBIDDEN,
        )

    def test_forbidden_message_says_deletes(self):
        report = guard.classify("rm x", roots=ROOTS, scratch=SCRATCH)
        self.assertIn("never deletes", report.violations[0].message)

    def test_contains_deletion(self):
        self.assertIsNotNone(guard.contains_deletion("rm -f -- /x/tmp"))
        self.assertIsNotNone(guard.contains_deletion("setsid rm /x"))
        self.assertIsNotNone(guard.contains_deletion("find /x -maxdepth 1 -delete"))
        self.assertIsNotNone(guard.contains_deletion("git clean -fd"))
        self.assertIsNone(guard.contains_deletion("mv /x /y\nsbatch j.sh\ncp a b"))
        self.assertIsNone(guard.contains_deletion("dd if=/dev/zero of=/x"))  # forbidden, not deletion
        self.assertIsNone(guard.contains_deletion("grep 'rm -rf' notes.txt"))


class TestOtherLowTierSpellings(unittest.TestCase):
    """Other spellings that once reached READ or too low a tier."""

    def test_ssh_hop_is_interpreter(self):
        self.assertEqual(
            worst("ssh cluster32 'rm -rf /gpfs/fs01/tester/cases'"), guard.INTERPRETER
        )
        self.assertEqual(worst("rsync -a /a/ host:/b/"), guard.INTERPRETER)

    def test_in_place_editors_are_forbidden(self):
        for script in (
            "ex -c '%s/a/b/g' -c wq /gpfs/fs01/tester/input.nml",
            "vi /gpfs/fs01/tester/input.nml",
            "ed -s file <<< ',s/a/b/\\nw'",
            "patch -p1 < fix.diff",
            "perl -pi -e 's/a/b/' f",
            "gawk -i inplace '{print}' f",
        ):
            self.assertEqual(worst(script), guard.FORBIDDEN, script)
        # perl -M takes a glued module name; its letters are not flags.
        self.assertEqual(worst("perl -Mstrict -e 'print 1'"), guard.INTERPRETER)

    def test_persistence_is_forbidden(self):
        self.assertEqual(worst("crontab /gpfs/fs01/tester/cron.txt"), guard.FORBIDDEN)
        self.assertEqual(worst("crontab -l"), guard.FORBIDDEN)
        self.assertEqual(worst("at now + 1 hour < job.txt"), guard.FORBIDDEN)

    def test_sbatch_wrap_is_interpreter(self):
        self.assertEqual(worst("sbatch --wrap 'hostname'"), guard.INTERPRETER)
        self.assertEqual(worst("sbatch --wrap='hostname'"), guard.INTERPRETER)
        self.assertEqual(worst("sbatch job.sh"), guard.WRITE)

    def test_user_wide_scancel_is_forbidden(self):
        self.assertEqual(worst("scancel -u tester"), guard.FORBIDDEN)
        self.assertEqual(worst("scancel --user=tester"), guard.FORBIDDEN)
        self.assertEqual(worst("scancel 12345"), guard.WRITE)
        self.assertEqual(worst("scancel -u tester 12345"), guard.WRITE)


class TestSubstitutionMasking(unittest.TestCase):
    """The outer line is split and tokenized with substitutions masked
    (shell.mask_substitutions); their bodies are scanned on their own.

    Before: `x=$(echo y) rm -rf /x` read `y)` as the command and missed the
    `rm` (read tier, no token), and the read-only `n=$(ls $d | grep -c x)`
    was refused as "`$d` runs a file".
    """

    def test_assignment_prefix_does_not_hide_the_verb(self):
        for s in ("x=$(echo y) rm -rf /x",
                  "x=$(echo a b) rm -rf /x",
                  "x=`echo y z` rm /x",
                  "A=$(date) B=$(hostname) rm /x"):
            self.assertEqual(worst(s), guard.FORBIDDEN, s)

    def test_computed_command_name_is_interpreter(self):
        for s in ("$(echo rm) -rf /x", "`echo rm` -rf /x", "x=1 $(which rm) /x"):
            self.assertEqual(worst(s), guard.INTERPRETER, s)

    def test_read_only_assignments_from_substitution_pass(self):
        for s in ("n=$(ls $d | grep -c x)",
                  "f=$(ls $A/x/*.nc)",
                  "x=$(ls /tmp/a)",
                  "x=`ls $d`",
                  "x=$(wc -l $f)",
                  "for c in $(ls $d); do echo $c; done"):
            self.assertIsNone(worst(s), s)

    def test_bodies_still_scanned(self):
        self.assertEqual(worst("x=$(rm -rf /x)"), guard.FORBIDDEN)
        self.assertEqual(worst("n=$(ls $d | tee out.txt)"), guard.WRITE)
        self.assertEqual(worst("diff <(ls a) <(rm -rf b)"), guard.FORBIDDEN)
        self.assertEqual(worst("x=$(echo $(sbatch j.sh))"), guard.WRITE)

    def test_unbalanced_substitution_still_scanned(self):
        self.assertEqual(worst("x=$(echo y; rm -rf /x"), guard.FORBIDDEN)

    def test_quoted_substitution_text_is_harmless(self):
        self.assertIsNone(worst("grep '$(ls)' f"))
        # Text in single quotes is still scanned as a substitution: heredoc
        # bodies are scanned line by line, and in an unquoted heredoc `$(...)`
        # runs even inside apostrophes. So a literal `$(x)` with an unknown
        # `x` is refused as unknown below improvise -- fail closed, not hidden.
        self.assertEqual(worst("grep '$(x)' f"), guard.UNKNOWN)
        self.assertEqual(worst('echo "$(date)" > out.txt'), guard.WRITE)

    def test_mask_roundtrip(self):
        line = "a $(b (c)) `d` <(e) f"
        masked, orig = shell.mask_substitutions(line)
        self.assertNotIn("$(", masked)
        self.assertNotIn("`", masked)
        self.assertEqual(shell.unmask(masked, orig), line)
