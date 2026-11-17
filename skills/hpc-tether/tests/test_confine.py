"""Write confinement: part of the floor, so it holds at every profile.

Every write the guard can identify must land in a write root (scratch plus
writable_roots); anything outside, or anything it can't place, is forbidden.
"""

import unittest

from hpctether import guard, transport

from test_profile import Sites
from test_trust import _load_bin, _run

SCRATCH = "/scratch/u"
CASE = "/proj/u/cases"
HOME = "/home/u"
ROOTS = [SCRATCH, "/proj/u"]
PATHS = {"scratch": SCRATCH, "case": CASE, "home": HOME}


def worst(script, aliases=None, roots=ROOTS):
    return guard.classify(script + "\n", roots=roots, paths=PATHS, aliases=aliases).worst()


class TestResolution(unittest.TestCase):
    def test_every_site_role_expands(self):
        self.assertEqual(worst("mkdir -p $HPC_CASE/new"), guard.WRITE)
        self.assertEqual(worst("mkdir -p ${HPC_SCRATCH}/x"), guard.WRITE)

    def test_outside_is_forbidden(self):
        for s in ("mkdir /proj/other/x", "touch /tmp/x", "echo hi > /tmp/f",
                  "cp $HPC_SCRATCH/a /proj/other/b", "tee /etc/x"):
            with self.subTest(s=s):
                self.assertEqual(worst(s), guard.FORBIDDEN)

    def test_lookalike_sibling_is_outside(self):
        self.assertEqual(worst("mkdir /proj/u2/x"), guard.FORBIDDEN)

    def test_unknown_variable_is_forbidden(self):
        self.assertEqual(worst("D=/proj/u/x\nmkdir $D/y"), guard.FORBIDDEN)

    def test_glob_inside_root(self):
        self.assertEqual(worst("gzip -k $HPC_SCRATCH/out/*.txt"), guard.WRITE)
        self.assertEqual(worst("gzip $HPC_SCRATCH/*/../../x*"), guard.FORBIDDEN)


class TestWorkingDirectory(unittest.TestCase):
    def test_same_line_cd_places_relative_target(self):
        self.assertEqual(worst("cd $HPC_SCRATCH && touch marker"), guard.WRITE)
        self.assertEqual(worst("cd /proj/other && touch marker"), guard.FORBIDDEN)

    def test_no_cd_means_login_directory(self):
        # The login directory (home role) is not a root here.
        self.assertEqual(worst("touch marker"), guard.FORBIDDEN)
        self.assertEqual(worst("touch marker", roots=ROOTS + [HOME]), guard.WRITE)

    def test_cd_elsewhere_makes_relative_unknown(self):
        self.assertEqual(worst("cd $HPC_SCRATCH\ntouch marker"), guard.FORBIDDEN)
        self.assertEqual(worst("cd $HPC_SCRATCH; touch marker"), guard.FORBIDDEN)

    def test_cd_holds_along_an_and_chain(self):
        self.assertEqual(worst("cd $HPC_SCRATCH/r && git log -1 && git pull"), guard.WRITE)
        self.assertEqual(worst("cd $HPC_SCRATCH && ls | head && touch m"), guard.WRITE)

    def test_chain_ends_at_or_semicolon_or_background(self):
        for s in ("cd $HPC_SCRATCH && ls || touch m", "cd $HPC_SCRATCH && ls; touch m",
                  "cd $HPC_SCRATCH && sleep 1 & touch m"):
            with self.subTest(s=s):
                self.assertEqual(worst(s), guard.FORBIDDEN)

    def test_chain_ends_at_another_cd(self):
        self.assertEqual(worst("cd $HPC_SCRATCH && cd .. && touch m"), guard.FORBIDDEN)

    def test_cwd_writers(self):
        self.assertEqual(worst("cd $HPC_SCRATCH && tar xzf /proj/u/a.tgz"), guard.WRITE)
        self.assertEqual(worst("cd /proj/other && tar xzf /proj/u/a.tgz"), guard.FORBIDDEN)
        self.assertEqual(worst("unzip /proj/u/a.zip -d $HPC_SCRATCH/x"), guard.WRITE)
        self.assertEqual(worst("unzip /proj/u/a.zip -d /tmp/x"), guard.FORBIDDEN)


class TestTargets(unittest.TestCase):
    def test_cp_writes_only_its_destination(self):
        self.assertEqual(worst("cp /data/shared/in.nc $HPC_SCRATCH/"), guard.WRITE)
        self.assertEqual(worst("cp $HPC_SCRATCH/a /data/shared/"), guard.FORBIDDEN)
        self.assertEqual(worst("cp -t $HPC_SCRATCH /data/a /data/b"), guard.WRITE)

    def test_chmod_mode_is_not_a_path(self):
        self.assertEqual(worst("chmod 644 $HPC_SCRATCH/f"), guard.WRITE)

    def test_ln_single_operand_writes_cwd(self):
        self.assertEqual(worst("cd $HPC_SCRATCH && ln -s $HPC_SCRATCH/data/x"), guard.WRITE)
        self.assertEqual(worst("cd /data && ln -s /proj/u/x"), guard.FORBIDDEN)

    def test_compress_to_stdout_writes_nothing(self):
        self.assertIsNone(worst("gzip -c /data/f.txt"))

    def test_nco_output(self):
        self.assertEqual(worst("ncks -v T /data/in.nc $HPC_SCRATCH/o.nc"), guard.WRITE)
        self.assertEqual(worst("ncks -v T /data/in.nc /data/o.nc"), guard.FORBIDDEN)
        self.assertEqual(worst("ncks -v T /data/in.nc -o /data/o.nc"), guard.FORBIDDEN)
        self.assertEqual(worst("ncatted -a units,T,o,c,K /data/f.nc"), guard.FORBIDDEN)
        self.assertEqual(worst("ncatted -a units,T,o,c,K $HPC_SCRATCH/f.nc"), guard.WRITE)

    def test_sort_output(self):
        self.assertEqual(worst("sort -o /data/x /proj/u/y"), guard.FORBIDDEN)

    def test_git(self):
        self.assertEqual(worst("git -C /proj/u/repo pull --ff-only"), guard.WRITE)
        self.assertEqual(worst("git -C /data/repo pull"), guard.FORBIDDEN)
        self.assertEqual(worst("cd $HPC_SCRATCH/repo && git pull"), guard.WRITE)
        self.assertEqual(worst("git clone https://x/y.git $HPC_SCRATCH/y"), guard.WRITE)
        self.assertEqual(worst("git clone https://x/y.git /data/y"), guard.FORBIDDEN)
        self.assertIsNone(worst("git -C /data/repo status"))

    def test_job_control_has_no_file_target(self):
        self.assertEqual(worst("scancel 123"), guard.WRITE)


class TestAliases(unittest.TestCase):
    def test_alias_makes_the_other_spelling_confined(self):
        roots = ROOTS + ["/gpfs/home/u"]
        self.assertEqual(worst("mkdir /home/u/x", roots=roots), guard.FORBIDDEN)
        self.assertEqual(worst("mkdir /home/u/x", roots=roots,
                               aliases={"/home/u": "/gpfs/home/u"}), guard.WRITE)

    def test_root_spelled_by_alias(self):
        roots = ROOTS + ["/home/u"]
        self.assertEqual(worst("mkdir /gpfs/home/u/x", roots=roots,
                               aliases={"/home/u": "/gpfs/home/u"}), guard.WRITE)

    def test_alias_does_not_reach_a_sibling(self):
        self.assertEqual(worst("mkdir /home/u2/x", roots=ROOTS + ["/gpfs/home/u"],
                               aliases={"/home/u": "/gpfs/home/u"}), guard.FORBIDDEN)


class TestFloorAtEveryLevel(unittest.TestCase):
    def test_unconfined_write_refused_even_at_improvise(self):
        mod = _load_bin("hpc-run")
        with Sites("improvise"):
            code, _, err = _run(mod, ["--site", "testsite", "--dry-run",
                                      "--allow-write", "--allow-exec", "-"],
                                "mkdir /proj/other/x\n")
        self.assertEqual(code, transport.EXIT_GUARD)
        self.assertIn("outside the write roots", err)


class TestForbiddenFooter(unittest.TestCase):
    """The footer must not call a fixable form the user's alone."""

    def _err(self, script):
        mod = _load_bin("hpc-run")
        with Sites("improvise"):
            code, _, err = _run(mod, ["--site", "testsite", "--dry-run", "-"], script)
        self.assertEqual(code, transport.EXIT_GUARD)
        return err

    def test_hinted_refusal_puts_the_hint_first(self):
        err = self._err("gzip $HPC_SCRATCH/c.txt\n")
        self.assertIn("Add -k", err)
        self.assertIn("Apply the hint above and retry", err)
        self.assertNotIn("These are the user's to run by hand", err)

    def test_unhinted_refusal_keeps_the_hand_run_note(self):
        err = self._err("rm $HPC_SCRATCH/c.txt\n")
        self.assertIn("These are the user's to run by hand", err)
        self.assertNotIn("Apply the hint", err)


if __name__ == "__main__":
    unittest.main()


ROOT = "/home/u/code/my-tools"


def worst_sr(script, cwd_roots=ROOTS + [HOME]):
    return guard.classify(script + "\n", roots=cwd_roots, paths=PATHS,
                          script_roots=[ROOT]).worst()


class TestCodeIntoScriptRoot(unittest.TestCase):
    """Code reaches a script root only through git, at every profile."""

    def test_code_file_refused(self):
        for s in (f"cp $HPC_SCRATCH/x.py {ROOT}/x.py",
                  f"cp $HPC_SCRATCH/x.py {ROOT}/",
                  f"mv $HPC_SCRATCH/x.py {ROOT}/lib/",
                  f"echo 'import os' > {ROOT}/evil.py",
                  f"touch {ROOT}/new.py",
                  f"ln -s $HPC_SCRATCH/x.py {ROOT}/x.py",
                  f"mv {ROOT}/notes.txt {ROOT}/notes.py",
                  f"cp $HPC_SCRATCH/run.sh {ROOT}/"):
            with self.subTest(s=s):
                self.assertEqual(worst_sr(s), guard.FORBIDDEN)

    def test_directory_target_with_a_dot_still_checks_sources(self):
        # `v2.0` may be a directory: the file written would be v2.0/a.py.
        self.assertEqual(worst_sr(f"cp $HPC_SCRATCH/a.py {ROOT}/v2.0"), guard.FORBIDDEN)

    def test_extensionless_source_refused(self):
        self.assertEqual(worst_sr(f"cp -r $HPC_SCRATCH/pkg {ROOT}/"), guard.FORBIDDEN)

    def test_unpacking_refused(self):
        self.assertEqual(worst_sr(f"cd {ROOT} && tar xzf $HPC_SCRATCH/a.tgz"), guard.FORBIDDEN)
        self.assertEqual(worst_sr(f"unzip $HPC_SCRATCH/a.zip -d {ROOT}/x"), guard.FORBIDDEN)

    def test_data_files_pass(self):
        for s in (f"cp $HPC_SCRATCH/grp4.csv {ROOT}/data/",
                  f"cp $HPC_SCRATCH/m.toml {ROOT}/data/m.toml",
                  f"echo x > {ROOT}/notes.txt",
                  f"mkdir -p {ROOT}/data/new",
                  f"chmod 644 {ROOT}/my-script.py"):
            with self.subTest(s=s):
                self.assertEqual(worst_sr(s), guard.WRITE)

    def test_git_is_how_code_arrives(self):
        self.assertEqual(worst_sr(f"git -C {ROOT} pull --ff-only"), guard.WRITE)

    def test_code_outside_a_script_root_is_fine(self):
        self.assertEqual(worst_sr("cp $HPC_SCRATCH/a.py $HPC_SCRATCH/b.py"), guard.WRITE)

    def test_inactive_roots_still_protected(self):
        # At prepare, script roots don't run, but the floor still holds.
        mod = _load_bin("hpc-run")
        with Sites("prepare"):
            code, _, err = _run(mod, ["--site", "testsite", "--dry-run", "--allow-write", "-"],
                                "cp /scratch/tester/x.py /home/tester/my-tools/x.py\n")
        self.assertEqual(code, transport.EXIT_GUARD)


class TestProtected(unittest.TestCase):
    """[policy] protected: never written, even inside a write root."""

    MODEL = "/proj/u/models/upstream"

    def _worst(self, script, aliases=None):
        report = guard.classify(script + "\n", roots=ROOTS, paths=PATHS,
                                aliases=aliases, protected=[self.MODEL])
        return report

    def test_write_into_protected_is_forbidden(self):
        r = self._worst(f"cp $HPC_SCRATCH/a.F90 {self.MODEL}/src/a.F90")
        self.assertEqual(r.worst(), guard.FORBIDDEN)
        self.assertIn("protects", " ".join(v.message for v in r.violations))

    def test_mv_out_of_protected_is_forbidden(self):
        self.assertEqual(self._worst(f"mv {self.MODEL}/x $HPC_SCRATCH/x").worst(),
                         guard.FORBIDDEN)

    def test_rest_of_the_root_is_still_writable(self):
        self.assertEqual(self._worst("mkdir -p /proj/u/models/other").worst(), guard.WRITE)

    def test_protected_through_an_alias(self):
        r = self._worst("touch /alias/u/models/upstream/x", aliases={"/alias/u": "/proj/u"})
        self.assertEqual(r.worst(), guard.FORBIDDEN)

    def test_reading_protected_is_fine(self):
        self.assertEqual(self._worst(f"ls {self.MODEL}; cat {self.MODEL}/README").violations,
                         [])
