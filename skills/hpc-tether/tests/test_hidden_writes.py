"""Payloads that act more than they look: read commands with writing
options, sed programs, wrappers, role reassignment, links, write forms.

Each case once classified lower than it acts.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hpctether import commands, guard

ROOT = "/scratch/u"
OUT = "/elsewhere/u"


def worst(script):
    return guard.classify(
        script + "\n", roots=[ROOT], scratch=ROOT,
        paths={"scratch": ROOT, "home": "/home/u"}).worst()


class TestReadOptions(unittest.TestCase):
    """A read command is a read only with options it lists."""

    def test_writing_forms_are_not_reads(self):
        for payload in (
            f"sed -n 'w {OUT}/f' /dev/null",
            f"sed -n '1,3w {OUT}/f' x",
            f"sed 's/a/b/w {OUT}/f' x",
            "sed -n '1e touch /x' f",
            "sed -f prog.sed f",
            f"xxd -r /dev/null {OUT}/f",
            f"xxd in {OUT}/f",
            "sort --compress-program=evil f",
            "sort -T /elsewhere f",
            "file -C -m magic",
            "date -s tomorrow",
            "hostname newname",
            f"find . -maxdepth 1 -fprint {OUT}/f",
            "git -c core.pager=evil log",
            f"git log --output={OUT}/f",
            "rg --pre evil pattern",
        ):
            with self.subTest(payload=payload):
                self.assertEqual(worst(payload), guard.UNKNOWN)

    def test_attached_output_option(self):
        self.assertEqual(worst(f"sort -o{OUT}/f /dev/null"), guard.FORBIDDEN)
        self.assertEqual(worst(f"sort -o{ROOT}/f /dev/null"), guard.WRITE)
        self.assertEqual(worst(f"sort --output={OUT}/f x"), guard.FORBIDDEN)

    def test_attached_target_directory(self):
        self.assertEqual(
            worst(f"cd {ROOT} && cp -t{OUT} /dev/null {ROOT}/in.txt"), guard.FORBIDDEN)
        self.assertEqual(worst(f"cp -t{ROOT}/d {ROOT}/a.txt"), guard.WRITE)

    def test_ordinary_reads_stay_reads(self):
        for payload in (
            "sed -n '1,20p' f",
            "sed -n '/begin/,/end/p' f | head -5",
            "sed -e 's/a/b/g' -e '/^#/d' f",
            'sed -n "1p;\\$p" f',
            "sed 's|/a/b|/c|;s/x/y/2' f",
            "head -20 f", "head -n5 f", "tail -n +3 f", "tail -5 f",
            "sort -k2,2 -n -t, f | uniq -c | sort -rn",
            "cut -d, -f1,3 f", "wc -l f", "cat -n f", "xxd -l 64 f",
            "stat -c %s f", "date -Iseconds", "date -d yesterday +%F",
            "hostname -s", "diff -u a b", "ncdump -h f.nc",
            "find . -maxdepth 2 -name '*.nc' -mtime -7 -size +10k",
            "git -c color.ui=never log --oneline -3",
            "ls -la /x", "grep -c pattern f", "squeue -u u -o '%i %T' --noheader",
            "gzip -dc f.gz | head", "zcat f.gz | wc -l",
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(worst(payload))

    def test_find_ok_is_forbidden_like_exec(self):
        self.assertEqual(worst("find . -maxdepth 1 -ok rm {} ;"), guard.FORBIDDEN)

    def test_split_options(self):
        opts, operands = commands.split_options(
            ["x", "-ab", "-o/f", "-n", "5", "--long=v", "--", "-z"], "on")
        self.assertEqual(opts, [("-a", None), ("-b", None), ("-o", "/f"),
                                ("-n", "5"), ("--long", "v")])
        self.assertEqual(operands, ["-z"])


class TestSedProgram(unittest.TestCase):
    def test_reads(self):
        for program in ("1,20p", "/a/,/b/p", "s/a/b/g", "$p", "5q", "1~2p",
                        "/x/{p;q}", "s/[/]/x/", "/a/!d", "y/abc/xyz/",
                        r"$!N;s/\n/ /", "/foo/I,+3p", "=", r"s/\(a\)\/b/\1/"):
            with self.subTest(program=program):
                self.assertTrue(commands.sed_program_reads(program))

    def test_not_reads(self):
        for program in ("w /x", "1w /x", "s/a/b/w /x", "s/a/b/e", "e ls", "1e",
                        "r /etc/x", "s/a/b/;w /x", "1,3{w /x\n}",
                        "s/[/]/a/;w /out", "W x", "s/a/b/gw f", "p;e", "/a/",
                        "s/a/b", r"\$p;$w /x"):
            with self.subTest(program=program):
                self.assertFalse(commands.sed_program_reads(program))


class TestWrappers(unittest.TestCase):
    """`env -C` moves a relative write elsewhere, and the same gap behind xargs."""

    def test_env_chdir(self):
        self.assertEqual(worst(f"cd {ROOT} && env -C {OUT} touch f"), guard.UNKNOWN)
        self.assertEqual(worst(f"cd {ROOT} && env --chdir={OUT} touch f"), guard.UNKNOWN)
        self.assertEqual(worst("env -S 'touch /x'"), guard.UNKNOWN)

    def test_env_plain_still_classifies_the_command(self):
        self.assertEqual(worst(f"env FOO=1 touch {ROOT}/f"), guard.WRITE)
        self.assertEqual(worst("env -i rm -rf /x"), guard.FORBIDDEN)

    def test_xargs_write_has_no_placeable_target(self):
        self.assertEqual(worst(f"echo {OUT}/f | xargs touch"), guard.UNKNOWN)
        self.assertIsNone(worst("ls /x | xargs -n 1 basename"))
        self.assertEqual(worst("ls | xargs rm"), guard.FORBIDDEN)


class TestRoleReassignment(unittest.TestCase):
    """A site role is never set in the body, on any segment."""

    def test_refused(self):
        for payload in (
            f"true; HPC_SCRATCH={OUT}; touch $HPC_SCRATCH/f",
            f"cd /x && HPC_SCRATCH={OUT} touch $HPC_SCRATCH/f",
            f"echo a | env HPC_SCRATCH={OUT} cat",
            "true && export HPC_SCRATCH=/x",
            "read HPC_SCRATCH < f",
            "printf -v HPC_SCRATCH %s /x",
            "for HPC_SCRATCH in /a /b; do echo $HPC_SCRATCH; done",
            "declare HPC_SCRATCH=/x",
            "unset HPC_SCRATCH",
            "HPC_SCRATCH+=/x",
            "x=$(HPC_SCRATCH=/x; echo $HPC_SCRATCH)",
            "((HPC_SCRATCH=1))",
        ):
            with self.subTest(payload=payload):
                self.assertEqual(worst(payload), guard.FORBIDDEN)

    def test_using_a_role_is_fine(self):
        self.assertIsNone(worst('echo "HPC_SCRATCH=$HPC_SCRATCH"'))
        self.assertIsNone(worst("ls $HPC_SCRATCH; printenv HPC_SCRATCH"))
        self.assertIsNone(worst("for f in $HPC_SCRATCH/*.nc; do ls $f; done"))


class TestLinks(unittest.TestCase):
    """A link's source must be in a write root too."""

    def test_link_out_of_roots(self):
        self.assertEqual(
            worst(f"cd {ROOT} && ln -s {OUT} link && touch {ROOT}/link/f"),
            guard.FORBIDDEN)
        self.assertEqual(worst(f"ln {OUT}/f {ROOT}/f"), guard.FORBIDDEN)
        self.assertEqual(worst(f"ln -s -t {ROOT}/d {OUT}/f"), guard.FORBIDDEN)

    def test_relative_symlink_target(self):
        self.assertEqual(worst(f"cd {ROOT} && ln -s ../x link"), guard.FORBIDDEN)

    def test_link_inside_roots(self):
        self.assertEqual(worst(f"ln -s {ROOT}/a {ROOT}/b"), guard.WRITE)
        self.assertEqual(worst("ln -s $HPC_SCRATCH/a $HPC_SCRATCH/b"), guard.WRITE)


class TestWriteForms(unittest.TestCase):
    """Compressors keep their input, and write commands that run a program."""

    def test_compressors_keep_their_input(self):
        for cmd in ("gzip", "gunzip", "bzip2", "xz"):
            with self.subTest(cmd=cmd):
                self.assertEqual(worst(f"{cmd} {ROOT}/f"), guard.FORBIDDEN)
                self.assertEqual(worst(f"{cmd} -k {ROOT}/f"), guard.WRITE)
        self.assertEqual(worst(f"gzip -9k {ROOT}/f"), guard.WRITE)
        self.assertEqual(worst(f"gzip --keep {ROOT}/f"), guard.WRITE)

    def test_tar(self):
        self.assertEqual(worst(f"cd {ROOT} && tar xzf a.tar.gz"), guard.WRITE)
        self.assertEqual(worst(f"cd {ROOT} && tar -czf a.tgz d"), guard.WRITE)
        self.assertEqual(worst(f"cd {ROOT} && tar --remove-files -cf a.tar d"),
                         guard.FORBIDDEN)
        for form in ("-I evil -xf a.tar", "--to-command=evil -xf a.tar",
                     "-cf a.tar --checkpoint-action=exec=evil d"):
            with self.subTest(form=form):
                self.assertEqual(worst(f"cd {ROOT} && tar {form}"), guard.UNKNOWN)

    def test_zip_split(self):
        self.assertEqual(worst(f"cd {ROOT} && zip -rm a.zip d"), guard.FORBIDDEN)
        self.assertEqual(worst(f"cd {ROOT} && split --filter=evil f"), guard.UNKNOWN)

    def test_chmod_dash_mode_is_a_mode(self):
        self.assertEqual(worst(f"chmod -w {OUT}/f"), guard.FORBIDDEN)
        self.assertEqual(worst(f"chmod -w {ROOT}/f"), guard.WRITE)


class TestNoAbbreviatedTokens(unittest.TestCase):
    """Argparse took `--allow-w` for `--allow-write`, a spelling
    the approval hook does not match."""

    def test_abbreviation_is_a_usage_error(self):
        import subprocess
        bin_dir = Path(__file__).resolve().parent.parent / "bin"
        for verb, args in (("hpc-run", ["--allow-w", "-"]),
                           ("hpc-run", ["--allow-e", "-"]),
                           ("hpc-push", ["f.csv", "--ye"]),
                           ("hpc-pull", ["x:y", "--force-s"]),
                           ("hpc-fetch", ["x:y", "--mer"])):
            with self.subTest(verb=verb, args=args):
                p = subprocess.run([sys.executable, str(bin_dir / verb), *args],
                                   input="", capture_output=True, text=True)
                self.assertEqual(p.returncode, 2)
                self.assertIn("unrecognized arguments", p.stderr)


if __name__ == "__main__":
    unittest.main()
