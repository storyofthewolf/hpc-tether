"""Fail-closed guard: below improvise, only commands the guard knows are sent.

Once an unrecognized command passed as a read, so `ncks -O`,
`cdo` and `nccopy` could write with no token. Now it is ``unknown``: profile
improvise and --allow-exec, like an interpreter.
"""

import unittest

from hpctether import commands, guard, profile, transport

from test_guard import worst
from test_profile import Sites
from test_trust import _load_bin, _run


class TestUnknownFailsClosed(unittest.TestCase):
    def test_unknown_command(self):
        self.assertEqual(worst("frobnicate /x"), guard.UNKNOWN)
        self.assertEqual(worst("module load nco"), guard.UNKNOWN)
        self.assertEqual(worst("trap 'rm x' EXIT"), guard.UNKNOWN)

    def test_unknown_needs_improvise(self):
        self.assertEqual(profile.needed_for(guard.classify("frobnicate /x\n")), "improvise")

    def test_known_reads_pass(self):
        for s in ("cat f", "head -5 f", "tail -n 20 f", "wc -l f", "sort f | uniq -c",
                  "ncdump -h f.nc", "sacct -j 1 -X", "squeue -u me", "md5sum f",
                  "date", "echo hi", "pwd", "readlink -f /x", "diff a b", "zcat f.gz"):
            with self.subTest(s=s):
                self.assertIsNone(worst(s))


class TestArgRules(unittest.TestCase):
    def test_ncks_read_and_write_forms(self):
        self.assertIsNone(worst("ncks -H -C -v T -d lat,23 in.nc"))
        self.assertIsNone(worst("ncks --trd -H -C -v FDSC,FSNSC -d ilev,0 -d lat,23 in.nc"))
        self.assertEqual(worst("ncks -v T in.nc out.nc"), guard.WRITE)
        self.assertEqual(worst("ncks -O -v T in.nc out.nc"), guard.WRITE)
        self.assertEqual(worst("ncks -v T in.nc -o out.nc"), guard.WRITE)
        self.assertEqual(worst("ncks --fl_out=out.nc in.nc"), guard.WRITE)

    def test_unknown_flag_errs_toward_write(self):
        # `--made-up VALUE in.nc`: VALUE counts as a file, so two files: write.
        self.assertEqual(worst("ncks --made-up X in.nc"), guard.WRITE)

    def test_sort_and_uniq_outputs(self):
        self.assertIsNone(worst("sort -k 2 f"))
        self.assertEqual(worst("sort -o out f"), guard.WRITE)
        self.assertEqual(worst("sort --output=out f"), guard.WRITE)
        self.assertIsNone(worst("uniq -c f"))
        self.assertEqual(worst("uniq f out"), guard.WRITE)

    def test_nco_cdo_writers(self):
        for c in ("ncrcat a.nc b.nc o.nc", "cdo timmean a.nc o.nc", "nccopy a.nc b.nc",
                  "ncatted -a units,T,o,c,K f.nc", "ncrename -v a,b f.nc"):
            with self.subTest(c=c):
                self.assertEqual(worst(c), guard.WRITE)


class TestShellStructure(unittest.TestCase):
    def test_loop_header_is_not_a_command(self):
        self.assertIsNone(worst("for c in a b; do ls $c; done"))
        self.assertIsNone(worst("for c in a b\ndo\n  ls $c\ndone"))

    def test_loop_words_substitution_still_scanned(self):
        self.assertEqual(worst("for f in $(rm -rf /x); do ls; done"), guard.FORBIDDEN)

    def test_case(self):
        self.assertIsNone(worst("case $x in\n pt01) echo a ;;\n *) ls ;;\nesac"))
        self.assertEqual(worst("case $x in\n pt01) rm -rf /y ;;\nesac"), guard.FORBIDDEN)

    def test_lone_case_label_fails_closed(self):
        self.assertEqual(worst("case $x in\n pt01)\n echo a ;;\nesac"), guard.UNKNOWN)

    def test_subshell(self):
        self.assertIsNone(worst("(cd /x && ls)"))
        self.assertIsNone(worst("(cd /x && pwd)"))
        self.assertEqual(worst("(rm -rf /x)"), guard.FORBIDDEN)
        self.assertEqual(worst("(cd /x && rm -rf y)"), guard.FORBIDDEN)

    def test_redirect_ampersand_is_not_a_separator(self):
        self.assertIsNone(worst("ls 2>&1 | tail -5"))
        self.assertIsNone(worst("ls >&2"))
        self.assertEqual(worst("ls &>out.txt"), guard.WRITE)
        self.assertEqual(worst("sleep 1 & rm -rf /x"), guard.FORBIDDEN)

    def test_continuation_lines_join(self):
        self.assertIsNone(worst("cd /x && \\\nls -la"))
        self.assertEqual(worst("ls && \\\nrm -rf /x"), guard.FORBIDDEN)

    def test_comment_backslash_does_not_hide_next_line(self):
        self.assertEqual(worst("# note \\\nrm -rf /x"), guard.FORBIDDEN)


class TestTables(unittest.TestCase):
    def test_read_list_disjoint_from_every_other_class(self):
        others = (commands.FORBIDDEN_ALWAYS | commands.WRITE_CMDS | commands.INTERPRETERS
                  | commands.SHELLS | commands.EDITORS | commands.EVAL_LIKE
                  | commands.EXEC_LAUNCHERS | commands.BUILD_DRIVERS
                  | commands.NETWORK_CMDS | commands.WRAPPERS
                  | frozenset(commands.ARG_RULES))
        self.assertEqual(commands.READ_COMMANDS & others, frozenset())


class TestHpcRunUnknown(unittest.TestCase):
    def setUp(self):
        self.mod = _load_bin("hpc-run")

    def test_unknown_refused_at_operate_names_improvise(self):
        with Sites("operate"):
            code, _, err = _run(self.mod, ["--site", "testsite", "--dry-run",
                                           "--allow-write", "--allow-exec", "-"],
                                "frobnicate /x\n")
        self.assertEqual(code, transport.EXIT_GUARD)
        self.assertIn("needs profile 'improvise'", err)

    def test_unknown_at_improvise_needs_allow_exec(self):
        with Sites("improvise"):
            code, _, err = _run(self.mod, ["--site", "testsite", "--dry-run", "-"],
                                "frobnicate /x\n")
            self.assertEqual(code, transport.EXIT_GUARD)
            self.assertIn("--allow-exec", err)
            code, _, _ = _run(self.mod, ["--site", "testsite", "--dry-run",
                                         "--allow-exec", "-"], "frobnicate /x\n")
            self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
