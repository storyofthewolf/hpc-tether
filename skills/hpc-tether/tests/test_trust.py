"""Pre-built scripts: ``script_roots`` in the site file, and how the guard
classifies a ``.py`` under one.

Nothing here opens a connection. Every test that loads a site points
``HPC_TETHER_SITES`` at a temporary directory, so the user's real sites/ is
never read.

The weight is on both sides of one line: a ``.py`` under a declared root, in
any spelling the scan can name for certain, runs with no token whatever its
arguments (per-file pins and argument patterns made the same
script pass or fail depending on spelling); hand-rolled code and anything the
scan cannot resolve stays interpreter tier.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hpctether import audit, guard, transport, trust  # noqa: E402
from hpctether import site as site_mod  # noqa: E402

REPO = "/home/tester/my-tools"
OTHER = "/home/tester/analysis"
SCRIPT = f"{REPO}/my-script.py"
PATHS = {"repo": REPO, "scratch": "/scratch/tester", "home": "/home/tester"}

SITE_TOML = f"""\
name = "testsite"
host = "test.example.edu"
scheduler = "slurm"
user = "tester"
profile = "improvise"
script_roots = ["{REPO}", "{OTHER}/"]
script_confirm = ["*my-script.py *--execute*"]

[paths]
scratch = "/scratch/tester"
repo    = "{REPO}"
"""


class TempSites:
    """Context manager: a temp sites/ directory holding one site file."""

    def __init__(self, toml: str = SITE_TOML):
        self.toml = toml

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        directory = Path(self._tmp.name)
        (directory / "testsite.toml").write_text(self.toml)
        self._previous = os.environ.get("HPC_TETHER_SITES")
        os.environ["HPC_TETHER_SITES"] = str(directory)
        return directory

    def __exit__(self, *exc) -> None:
        if self._previous is None:
            os.environ.pop("HPC_TETHER_SITES", None)
        else:
            os.environ["HPC_TETHER_SITES"] = self._previous
        self._tmp.cleanup()


ROOTS = trust.Roots(
    roots=(REPO, OTHER),
    confirm=("*my-script.py *--execute*",),
    paths=tuple(sorted(PATHS.items())),
)


def classify(script, roots=ROOTS):
    return guard.classify(
        script, roots=["/scratch/tester"], scratch="/scratch/tester",
        trust=roots, paths=PATHS,
    )


def kinds(script, roots=ROOTS):
    return [v.kind for v in classify(script, roots).violations]


class TestSiteFile(unittest.TestCase):
    def test_loads_roots_and_confirm(self):
        with TempSites():
            target = site_mod.load("testsite")
        self.assertEqual(target.script_roots, [REPO, OTHER])
        self.assertEqual(target.script_confirm, ["*my-script.py *--execute*"])

    def test_absent_means_none(self):
        toml = "\n".join(l for l in SITE_TOML.splitlines() if not l.startswith("script_"))
        with TempSites(toml):
            target = site_mod.load("testsite")
        self.assertEqual(target.script_roots, [])
        self.assertFalse(trust.Roots.for_site(target))

    def test_bad_roots_are_refused(self):
        for bad in ('"/home"', '"relative/dir"', '"/home/tester/../x"', "7"):
            with self.subTest(bad=bad):
                toml = SITE_TOML.replace(f'"{OTHER}/"', bad)
                with TempSites(toml):
                    with self.assertRaises(site_mod.SiteError):
                        site_mod.load("testsite")

    def test_stale_trust_file_is_not_a_site(self):
        previous = os.environ.pop("HPC_TETHER_SITE", None)
        try:
            with TempSites() as directory:
                (directory / "testsite.trust.toml").write_text("")
                self.assertEqual(site_mod.available(), ["testsite"])
                self.assertEqual(site_mod.default_site_name(), "testsite")
        finally:
            if previous is not None:
                os.environ["HPC_TETHER_SITE"] = previous


class TestPrebuiltIsReadTier(unittest.TestCase):
    """Every spelling the scan can name for certain, any arguments."""

    def test_spellings(self):
        for script in (
            f"python {SCRIPT} check A",
            f"python3 {SCRIPT}",
            f"python3.13 {SCRIPT} status",
            "python $HPC_REPO/my-script.py check",
            "python ${HPC_REPO}/build.py verify A",
            "$HPC_PYTHON_BIN/python $HPC_REPO/scan.py --update",
            f"python {OTHER}/sub/dir/plot.py --anything goes",
            f"{SCRIPT} check A",
            "cd $HPC_REPO && python my-script.py check A",
            f"cd {OTHER} && python ./sub/plot.py",
            "cd /home/tester && python my-tools/my-script.py check A",
            f"OMP_NUM_THREADS=4 python {SCRIPT} check A",
            f"python -u {SCRIPT} check A",
            f"python -- {SCRIPT} check A",
            "python ~/my-tools/my-script.py check A",
            "cd ~/my-tools && python my-script.py check A",
            "cd ~ && python my-tools/my-script.py check A",
            'echo "$HOME"\ncd ~/my-tools && python my-script.py check A',
            "cd $HPC_REPO && echo yes | python my-script.py make A",
            "cd $HPC_REPO && printf 'y\\n' | python my-script.py make A | tail -5",
            "cd $HPC_REPO && python my-script.py check A | grep x",
        ):
            with self.subTest(script=script):
                self.assertEqual(kinds(script), [])

    def test_use_records_absolute_path_root_and_args(self):
        report = classify("cd $HPC_REPO && python my-script.py check A")
        self.assertEqual(report.uses, [trust.Use(SCRIPT, REPO, "check A", "free")])

    def test_use_args_keep_substitution_text(self):
        # The outer line is scanned with substitutions masked; the audit must
        # still record the arguments as written.
        report = classify(f"python {SCRIPT} check $(cat cases.txt) `date`")
        self.assertEqual(report.uses,
                         [trust.Use(SCRIPT, REPO, "check $(cat cases.txt) `date`", "free")])
        self.assertEqual(kinds(f"python {SCRIPT} check $(rm -rf x)"), [guard.FORBIDDEN])

    def test_confirm_pattern_is_write_tier(self):
        self.assertEqual(kinds(f"python {SCRIPT} submit A --execute"), [guard.WRITE])
        self.assertEqual(classify(f"python {SCRIPT} submit A --execute").uses[0].tier,
                         "write")

    def test_redirect_and_writes_around_it_still_count(self):
        self.assertEqual(kinds(f"python {SCRIPT} check A > /scratch/tester/out.txt"),
                         [guard.WRITE])
        self.assertEqual(kinds(f"mkdir -p /scratch/tester/x && python {SCRIPT} check A"),
                         [guard.WRITE])


class TestHandRolledStaysStrict(unittest.TestCase):
    def test_inline_and_outside_code_is_interpreter_tier(self):
        for script in (
            "python -c 'print(1)'",
            f"python -c 'print(1)' {SCRIPT}",
            f"python3 -cprint(1) {SCRIPT} check",
            f"python3 -uc print(1) {SCRIPT}",
            f"python -m json.tool {SCRIPT}",
            "python - <<EOF\nprint(1)\nEOF",
            "python /home/tester/code/loose.py",
            "python /scratch/tester/agent_wrote_this.py",
            f"python {REPO}notreally/x.py",
            f"python {REPO}/../my-tools/my-script.py",
            f"python {REPO}/run*.py",
            f"python {REPO}/notes.txt",
            "python ~tester/my-tools/my-script.py",
            "HOME=/tmp/evil\npython ~/my-tools/my-script.py",
            "python $HOME/my-tools/my-script.py",
            "python my-script.py check A",
            "bash -c 'python my-script.py'",
            f"perl {REPO}/tool.py",
        ):
            with self.subTest(script=script):
                self.assertIn(guard.INTERPRETER, kinds(script))

    def test_unresolvable_cd_leaves_relative_untrusted(self):
        for script in (
            "cd ~tester/my-tools && python my-script.py check A",
            "export HOME=/tmp/evil\ncd ~/my-tools && python my-script.py check A",
            "read HOME\ncd ~ && python my-tools/my-script.py check A",
            "cd $HPC_REPO; python my-script.py check A",
            "cd $HPC_REPO\npython my-script.py check A",
            "true || cd $HPC_REPO && python my-script.py check A",
            "cd $HPC_REPO && ls && python my-script.py check A",
            "cd /scratch/tester && python my-script.py check A",
            "cd() { :; }\ncd $HPC_REPO && python my-script.py check A",
            "echo $(cd $HPC_REPO && python my-script.py check A)",
            # the pipeline after the cd resolves; past it, nothing does
            "cd $HPC_REPO && echo yes | ls && python my-script.py check A",
            "cd $HPC_REPO && echo yes; echo | python my-script.py check A",
            "cd $HPC_REPO && echo yes || echo | python my-script.py check A",
            "cd $HPC_REPO && echo yes & echo | python my-script.py check A",
            "echo | cd $HPC_REPO && echo | python my-script.py check A",
            "cd $HPC_REPO && echo yes |& python my-script.py check A",
        ):
            with self.subTest(script=script):
                self.assertIn(guard.INTERPRETER, kinds(script))

    def test_tilde_needs_a_home_role(self):
        no_home = trust.Roots(roots=(REPO,), paths=(("repo", REPO),))
        self.assertIn(guard.INTERPRETER,
                      kinds("cd ~/my-tools && python my-script.py", roots=no_home))
        self.assertIn(guard.INTERPRETER,
                      kinds("python ~/my-tools/my-script.py", roots=no_home))

    def test_bare_name_is_never_a_root_script(self):
        report = classify("cd $HPC_REPO && my-script.py check A")
        self.assertEqual(report.uses, [])

    def test_no_roots_means_interpreter_tier(self):
        self.assertEqual(kinds(f"python {SCRIPT} check A", roots=None),
                         [guard.INTERPRETER])

    def test_role_reassignment_is_refused(self):
        report = classify("HPC_REPO=/tmp/evil\npython $HPC_REPO/my-script.py check C")
        self.assertIn(guard.FORBIDDEN, [v.kind for v in report.violations])
        self.assertEqual(report.uses, [])

    def test_forbidden_anywhere_voids_the_payload(self):
        # Built by concatenation: no deletion verb on a real command line.
        deleting = "r" + "m -f /scratch/tester/x"
        report = classify(f"python {SCRIPT} check C\n{deleting}")
        self.assertIn(guard.FORBIDDEN, [v.kind for v in report.violations])
        self.assertEqual(report.uses, [])
        self.assertIsNotNone(guard.contains_deletion(f"python {SCRIPT}\n{deleting}"))


class TestAudit(unittest.TestCase):
    def test_records_name_path_root_args_tier(self):
        uses = [trust.Use(SCRIPT, REPO, "check A", "free")]
        self.assertEqual(audit.trusted_records(uses),
                         [{"path": SCRIPT, "root": REPO, "args": "check A",
                           "tier": "free"}])
        self.assertIsNone(audit.trusted_records([]))


def _load_bin(name):
    modname = name.replace("-", "_") + "_t"
    spec = importlib.util.spec_from_loader(
        modname,
        importlib.machinery.SourceFileLoader(modname, str(ROOT / "bin" / name)),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(module, argv, stdin=""):
    saved = sys.stdin
    sys.stdin = io.StringIO(stdin)
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = module.main(argv)
    finally:
        sys.stdin = saved
    return code, out.getvalue(), err.getvalue()


class TestHpcRun(unittest.TestCase):
    def setUp(self):
        self.mod = _load_bin("hpc-run")

    def test_prebuilt_passes_with_no_token_and_no_pin_check(self):
        with TempSites():
            code, out, _ = _run(self.mod, ["--site", "testsite", "--dry-run", "-"],
                                "python $HPC_REPO/my-script.py check A\n")
        self.assertEqual(code, 0)
        self.assertIn("my-script.py", out)
        self.assertNotIn("sha256sum", out)

    def test_no_trust_falls_back_to_interpreter_tier(self):
        with TempSites():
            code, _, err = _run(self.mod,
                                ["--site", "testsite", "--dry-run", "--no-trust", "-"],
                                "python $HPC_REPO/my-script.py check A\n")
        self.assertEqual(code, transport.EXIT_GUARD)
        self.assertIn("--allow-exec", err)

    def test_refusal_explains_how_a_script_is_recognized(self):
        with TempSites():
            code, _, err = _run(self.mod, ["--site", "testsite", "-"],
                                "cd $HOME/my-tools && python my-script.py check A\n")
        self.assertEqual(code, transport.EXIT_GUARD)
        self.assertIn("trusted repos", err)
        self.assertIn(REPO, err)


class TestHpcTrust(unittest.TestCase):
    def setUp(self):
        self.mod = _load_bin("hpc-trust")

    def test_list(self):
        with TempSites():
            code, out, _ = _run(self.mod, ["--site", "testsite", "list"])
        self.assertEqual(code, 0)
        self.assertIn(REPO, out)
        self.assertIn("*my-script.py *--execute*", out)
        self.assertNotIn("local checkouts", out)    # no [repos] entry has local

    def test_list_shows_local_checkouts(self):
        toml = SITE_TOML + ('\n[repos.my-tools]\nremote = "/home/tester/py/my-tools"\n'
                            'local = "/l/my-tools"\n'
                            '\n[repos.far]\nremote = "/home/tester/py/far"\n')
        with TempSites(toml):
            code, out, _ = _run(self.mod, ["--site", "testsite", "list"])
        self.assertEqual(code, 0, out)
        self.assertIn("local checkouts", out)
        self.assertIn("my-tools: /l/my-tools  <->  /home/tester/py/my-tools", out)
        self.assertNotIn("far:", out)

    def test_check_reports_tiers(self):
        with TempSites():
            code, out, _ = _run(self.mod, ["--site", "testsite", "check", "-"],
                                f"python {SCRIPT} check A\npython -c 'x'\n")
        self.assertEqual(code, 0)
        self.assertIn(f"free        {SCRIPT} check A", out)
        self.assertIn("interpreter", out)


if __name__ == "__main__":
    unittest.main()


class TestScopedConfirm(unittest.TestCase):
    """A repo's own confirm patterns apply to that repo only (2026-11 layout):
    a pattern written for one repo's flags must not catch another repo's."""

    SCOPED = trust.Roots(roots=(REPO, OTHER), scoped=((REPO, ("* --execute*",)),),
                         paths=tuple(sorted(PATHS.items())))

    def test_pattern_applies_in_its_repo(self):
        self.assertEqual(kinds(f"python {SCRIPT} submit A --execute", self.SCOPED),
                         [guard.WRITE])

    def test_pattern_does_not_reach_another_repo(self):
        other = OTHER.rstrip("/") + "/tool.py"
        self.assertEqual(kinds(f"python {other} run --execute", self.SCOPED), [])
