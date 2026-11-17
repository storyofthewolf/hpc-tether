"""describe: a payload in plain words for the approval prompt."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hpctether import describe as d, guard

PATHS = {"scratch": "/s/u", "case": "/w/u/cases", "repo": "/h/u/repo"}
ROOTS = ["/s/u", "/w/u"]


def say(script: str) -> str:
    report = guard.classify(script, roots=ROOTS, scratch=PATHS["scratch"], paths=PATHS)
    return d.describe(script, report, PATHS)


class TestAliasSpelling(unittest.TestCase):
    """A path in the other alias spelling still reads as a role."""
    ALIASES = {"/scratch/u": "/gpfs/u"}

    def test_either_spelling_names_the_role(self):
        paths = {"scratch": "/scratch/u"}
        for word in ("/gpfs/u/t/b.txt", "/scratch/u/t/b.txt"):
            self.assertEqual(d.role_path(word, paths, aliases=self.ALIASES), "scratch:t/b.txt")
        paths = {"scratch": "/gpfs/u"}
        self.assertEqual(d.role_path("/scratch/u/t/b.txt", paths, aliases=self.ALIASES),
                         "scratch:t/b.txt")

    def test_without_aliases_unchanged(self):
        self.assertEqual(d.role_path("/gpfs/u/b", {"scratch": "/scratch/u"}), "/gpfs/u/b")

    def test_describe_uses_aliases(self):
        script = "touch /gpfs/u/t/b.txt\n"
        paths = {"scratch": "/scratch/u"}
        report = guard.classify(script, roots=["/gpfs/u"], scratch=paths["scratch"], paths=paths,
                                aliases=self.ALIASES)
        self.assertEqual(d.describe(script, report, paths, self.ALIASES), "touch scratch:t/b.txt")


class TestPhrases(unittest.TestCase):
    def test_file_operations(self):
        self.assertEqual(say("mkdir -p $HPC_SCRATCH/new\n"), "create directory scratch:new")
        self.assertEqual(say("cp /s/u/a /s/u/b\n"), "copy scratch:a → scratch:b")
        self.assertEqual(say("mv -n $HPC_SCRATCH/a $HPC_SCRATCH/b\n"),
                         "move scratch:a → scratch:b (no overwrite)")
        self.assertEqual(say("echo x >> $HPC_SCRATCH/log\n"), "append to scratch:log")
        self.assertEqual(say("cd $HPC_SCRATCH && tar czf out.tgz d\n"), "create archive out.tgz")

    def test_jobs(self):
        self.assertEqual(say("sbatch -J run_A $HPC_SCRATCH/j.sh\n"), "submit job run_A (j.sh)")
        self.assertEqual(say("JID=$(sbatch --parsable $HPC_SCRATCH/j.sh)\n"), "submit job j.sh")
        self.assertEqual(say("scancel 42\n"), "cancel job 42")
        self.assertEqual(say("scontrol update JobId=7 TimeLimit=1:00:00\n"),
                         "change job 7: TimeLimit=1:00:00")

    def test_code(self):
        self.assertEqual(say("python -c 'print(1)'\n"), "run code the agent wrote (python)")
        self.assertEqual(say("python3 /s/u/x.py\n"), "run x.py (not in a trusted repo)")


class TestTrustedConfirm(unittest.TestCase):
    """A confirm-pattern match is phrased once, as trusted, not also as
    "not a trusted script"."""
    REPO = "/h/u/repo"

    def setUp(self):
        from hpctether import trust
        self.trust = trust.Roots(roots=(self.REPO,), scoped=((self.REPO, ("* --execute*",)),),
                                 paths=tuple(sorted(PATHS.items())))

    def say(self, script):
        report = guard.classify(script, roots=ROOTS, scratch=PATHS["scratch"], paths=PATHS,
                                trust=self.trust)
        return d.describe(script, report, PATHS)

    def test_confirm_match_is_phrased_once(self):
        got = self.say('export PATH="$HPC_PYTHON_BIN:$PATH"\n'
                       "echo yes | python $HPC_REPO/my-script.py update A --set x=true --execute\n")
        self.assertEqual(got, "run trusted my-script.py update A --set x=true --execute "
                              "(matches its repo's confirm pattern)")
        self.assertNotIn("not in a trusted repo", got)

    def test_other_writes_on_the_line_still_phrased(self):
        got = self.say("python $HPC_REPO/my-script.py go --execute && mkdir $HPC_SCRATCH/n\n")
        self.assertIn("run trusted my-script.py go --execute (matches its repo's confirm pattern)", got)
        self.assertIn("create directory scratch:n", got)

    def test_free_use_has_no_confirm_note(self):
        got = self.say("python $HPC_REPO/my-script.py check A && mkdir $HPC_SCRATCH/n\n")
        self.assertIn("run trusted my-script.py check A", got)
        self.assertNotIn("confirm", got)

    def test_reads_are_counted_not_listed(self):
        self.assertEqual(say("squeue -u $USER\nsacct -j 1\n"), "read only (2 lines)")
        self.assertEqual(say("mkdir $HPC_SCRATCH/a\nls $HPC_SCRATCH\n"),
                         "create directory scratch:a (+1 read-only line)")
        self.assertEqual(say("for c in a b; do\n  ls $c\ndone\n"), "read only (1 line)")

    def test_refusals_named(self):
        self.assertEqual(say("rm -f $HPC_SCRATCH/x\n"), "REFUSED, never sent: delete scratch:x")

    def test_many_phrases_capped(self):
        text = say("".join(f"mkdir $HPC_SCRATCH/d{i}\n" for i in range(9)))
        self.assertTrue(text.endswith("; and 3 more"), text)

    def test_long_paths_keep_the_name(self):
        self.assertEqual(d.role_path("/s/u/" + "x/" * 30 + "f.nc", PATHS), "scratch:…/f.nc")
        self.assertEqual(d.role_path("$HPC_REPO", PATHS), "repo")

    def test_a_flagged_line_is_never_dropped(self):
        # Whatever the phrase table lacks, a flagged line still shows up.
        for script in ("sort -o $HPC_SCRATCH/s $HPC_SCRATCH/a\n",
                       "frobnicate --hard\n",
                       "ncks -O -v T $HPC_SCRATCH/a.nc $HPC_SCRATCH/b.nc\n",
                       "srun -n 1 hostname\n"):
            with self.subTest(script=script):
                text = say(script)
                self.assertNotIn("read only", text)
                self.assertNotEqual(text, "nothing to run")


if __name__ == "__main__":
    unittest.main()
