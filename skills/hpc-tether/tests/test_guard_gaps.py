"""Gaps closed after the guard was frozen: the few an agent might plausibly
write, and false refusals of harmless reads. Everything else is a known limit (reference/design.md, "Known limits")."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from hpctether import guard, transport

from test_guard import worst


class TestSedExpression(unittest.TestCase):
    def test_expression_value_as_next_token_is_validated(self):
        for s in ("sed --expression 'w /x' f", "sed --expression=w\\ /x f",
                  "sed -e p -e 'w /x' f", "sed --expression p --expression 'e ls' f",
                  "sed -n -e 'w /x' f", "sed -ew\\ /x f"):
            with self.subTest(s=s):
                self.assertEqual(worst(s), guard.UNKNOWN)

    def test_file_option_stays_unknown(self):
        for s in ("sed -f prog.sed f", "sed --file prog.sed f", "sed --file=prog.sed f"):
            with self.subTest(s=s):
                self.assertEqual(worst(s), guard.UNKNOWN)

    def test_reading_expressions_pass(self):
        for s in ("sed --expression p f", "sed --expression=p f", "sed -e p -e 5q f",
                  "sed -n --expression '1,5p' f", "sed -e 's/a/b/' f"):
            with self.subTest(s=s):
                self.assertIsNone(worst(s))


class TestBehindXargs(unittest.TestCase):
    def test_commands_with_writing_forms_are_unknown(self):
        for s in ("echo -o/out f | xargs sort", "ls | xargs -n1 sort", "ls | parallel sort",
                  "ls | xargs sed -n p", "ls | xargs jq .", "ls | xargs xxd",
                  "ls | xargs -I{} cat {} | xargs file"):
            with self.subTest(s=s):
                self.assertEqual(worst(s), guard.UNKNOWN)

    def test_short_list_still_passes(self):
        for s in ("ls | xargs grep -l foo", "ls | xargs wc -l", "ls | xargs -n1 cat",
                  "ls | xargs head -3", "ls | xargs tail -2", "ls | xargs stat",
                  "ls | xargs md5sum", "ls | xargs basename", "ls | xargs dirname",
                  "ls | xargs echo", "ls | parallel -j2 wc -l", "ls | xargs -n 1 ls -l"):
            with self.subTest(s=s):
                self.assertIsNone(worst(s))

    def test_a_deletion_behind_xargs_is_still_forbidden(self):
        self.assertEqual(worst("ls | xargs rm"), guard.FORBIDDEN)


class TestSymlinkedEntrypoint(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp())
        self.repo = base / "repo"
        self.repo.mkdir()
        self.scratch = base / "scratch"
        self.scratch.mkdir()
        (self.scratch / "evil.py").write_text("print('untracked')\n")
        (self.repo / "good.py").write_text("print('trusted')\n")
        os.symlink(self.scratch / "evil.py", self.repo / "link.py")
        for args in (["init", "-q"], ["add", "."],
                     ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x"]):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def run_payload(self, name):
        root = str(self.repo)
        check = transport.trusted_check([root], [(root, str(self.repo / name))])
        inner = "\n".join([*transport.strict_prelude(len(check)), *check, "echo BODY-RAN"])
        return subprocess.run(["bash", "-s"], input=f"bash <<'__B__'\n{inner}\n__B__\n",
                              capture_output=True, text=True)

    def test_committed_symlink_is_refused(self):
        p = self.run_payload("link.py")
        self.assertEqual(p.returncode, transport.EXIT_UNTRUSTED)
        self.assertNotIn("BODY-RAN", p.stdout)
        self.assertIn("symlink", p.stderr)

    def test_regular_tracked_file_runs(self):
        p = self.run_payload("good.py")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("BODY-RAN", p.stdout)


class TestJobNamesInSubstitutions(unittest.TestCase):
    TWICE = "a=$(sbatch --parsable -J x job.sh); b=$(sbatch --parsable -J x job.sh)\n"

    def test_repeat_inside_substitutions(self):
        self.assertEqual(guard.sbatch_repeats(self.TWICE), ["x"])
        self.assertEqual(guard.sbatch_repeats(
            "a=$(sbatch -J x j.sh)\nb=`sbatch -J x j.sh`\n"), ["x"])
        self.assertEqual(guard.sbatch_repeats(
            "sbatch -J x j.sh\nb=$(sbatch -J x j.sh)\n"), ["x"])

    def test_loop_depth_is_kept(self):
        self.assertEqual(guard.sbatch_repeats(
            "for i in 1 2 3; do id=$(sbatch --parsable -J x j.sh); done\n"), ["x"])
        self.assertEqual(guard.sbatch_repeats(
            "for i in 1 2\ndo\n  id=$(sbatch --parsable -J x j.sh)\ndone\n"), ["x"])

    def test_one_submission_in_a_substitution_is_not_a_repeat(self):
        self.assertEqual(guard.sbatch_repeats("a=$(sbatch --parsable -J x j.sh)\n"), [])
        self.assertEqual(guard.sbatch_repeats(
            "a=$(sbatch -J x j.sh); b=$(sbatch -J y j.sh)\n"), [])
        self.assertEqual(guard.sbatch_repeats(
            "for f in a b; do echo $(basename $f); done\na=$(sbatch -J x j.sh)\n"), [])

    def test_names_inside_substitutions_are_checked(self):
        self.assertEqual(guard.sbatch_job_names(self.TWICE), (["x"], []))
        self.assertEqual(guard.sbatch_job_names("id=$(sbatch --parsable -J nm j.sh)\n"),
                         (["nm"], []))

    def test_unnamed_submission_in_a_substitution_is_unchecked(self):
        names, unchecked = guard.sbatch_job_names("id=$(sbatch --parsable j.sh)\n")
        self.assertEqual(names, [])
        self.assertEqual(len(unchecked), 1)


class TestFalseRefusalsFixed(unittest.TestCase):
    def test_reads_that_were_refused(self):
        for s in ("comm -12 a b", "comm -23 a b", "join -1 1 -2 1 a b", "join -t, -1 2 a b",
                  "diff -L old -L new a b", "rg --pretty pattern file", "rg --pre-x pat f",
                  "git diff -O orderfile", "git log -O orderfile", "date --date '-1 day' +%F",
                  "date --reference f", "rev -0 f", "head -20 f", "sort -n f",
                  r"sed 's[foo[bar[' f", "sed -n 's/[[:alpha:]/]/x/p' f",
                  "sed -n 's/[^[:space:]]/x/' f", "sed -n 's/[[.hyphen.]/]/x/p' f"):
            with self.subTest(s=s):
                self.assertIsNone(worst(s))

    def test_the_guards_those_refusals_sat_next_to_still_hold(self):
        for s in ("rg --pre cmd pat f", "rg --pre=cmd pat f", "rg --pre-glob '*.gz' pat f",
                  "rg --hostname-bin x pat f", "git grep -O pat", "git grep -Oless pat",
                  "git grep --open-files-in-pager=less pat", "git diff --ext-diff",
                  "file -p f", "sed 's/a/b/w x' f", "sed -n 's/[[:alpha:]/]/x/w y' f",
                  "comm -4 a b >/x"):
            with self.subTest(s=s):
                self.assertIsNotNone(worst(s))
