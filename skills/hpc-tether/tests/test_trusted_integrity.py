"""What keeps a trusted script trusted: its environment, and a clean checkout.

1. A payload that sets a loader variable (PYTHONPATH, PATH, LD_PRELOAD, ...)
   runs no trusted script: those variables decide what code it loads.
2. Before a trusted script runs, its root must be a git checkout with no
   modified or untracked code file (run on the cluster, exit 78).
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from hpctether import guard, transport

from test_trust import PATHS, ROOTS, SCRIPT

R = ["/scratch/tester", "/home/tester"]


def classify(script):
    return guard.classify(script + "\n", roots=R, paths=PATHS, trust=ROOTS)


class TestLoaderEnvironment(unittest.TestCase):
    def test_setting_a_loader_variable_drops_trust(self):
        for s in (f"PYTHONPATH=/scratch/tester/x python {SCRIPT} check A",
                  f"export PYTHONPATH=/scratch/tester/x\npython {SCRIPT} check A",
                  f"PATH=/scratch/tester/bin:$PATH python {SCRIPT} check A",
                  f"export PATH=/scratch/tester/bin:$PATH\npython {SCRIPT} check A",
                  f"LD_PRELOAD=/scratch/tester/x.so python {SCRIPT} check A",
                  f"env PYTHONHOME=/scratch/tester python {SCRIPT} check A",
                  f"BASH_ENV=/scratch/tester/x python {SCRIPT} check A",
                  f"read PYTHONPATH <<< /scratch/tester; export PYTHONPATH\npython {SCRIPT} check A"):
            with self.subTest(s=s):
                r = classify(s)
                self.assertEqual(r.uses, [])
                self.assertEqual(r.worst(), guard.INTERPRETER)
                self.assertTrue(any("script roots are ignored" in n for n in r.notes))

    def test_site_python_prelude_keeps_trust(self):
        for prelude in ('export PATH="$HPC_PYTHON_BIN:$PATH"',
                        "export PATH=$HPC_PYTHON_BIN:$PATH",
                        'export PATH="${HPC_PYTHON_BIN}:${PATH}"'):
            with self.subTest(prelude=prelude):
                r = classify(f"{prelude}\npython {SCRIPT} check A")
                self.assertEqual(len(r.uses), 1)
                self.assertIsNone(r.worst())

    def test_expansions_and_lookalikes_are_not_settings(self):
        for s in (f'echo "$PATH"; python {SCRIPT} check A',
                  f'echo "${{PYTHONPATH:-}}"; python {SCRIPT} check A',
                  f"MANPATH=/x python {SCRIPT} check A",
                  f"# PYTHONPATH is not set here\npython {SCRIPT} check A"):
            with self.subTest(s=s):
                self.assertEqual(len(classify(s).uses), 1)


class TestCleanCheckout(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.repo = self.dir / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        (self.repo / "my-script.py").write_text("print('trusted')\n")
        (self.repo / ".gitignore").write_text("build_scripts/\n__pycache__/\n")
        self.git("add", ".")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x")

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def run_payload(self, root, body="echo BODY-RAN"):
        # Fed through a quoted heredoc on stdin, as build_payload delivers it:
        # `bash -c` numbers lines one short, so it would misreport LINENO.
        check = transport.trusted_check([str(root)])
        inner = "\n".join([*transport.strict_prelude(len(check)), *check, body])
        return subprocess.run(["bash", "-s"], input=f"bash <<'__B__'\n{inner}\n__B__\n",
                              capture_output=True, text=True)

    def test_clean_checkout_runs(self):
        p = self.run_payload(self.repo)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("BODY-RAN", p.stdout)

    def test_untracked_code_stops_everything(self):
        (self.repo / "yaml.py").write_text("import os\n")
        p = self.run_payload(self.repo)
        self.assertEqual(p.returncode, transport.EXIT_UNTRUSTED)
        self.assertNotIn("BODY-RAN", p.stdout)
        self.assertIn("yaml.py", p.stderr)

    def test_modified_code_stops_everything(self):
        (self.repo / "my-script.py").write_text("print('changed')\n")
        p = self.run_payload(self.repo)
        self.assertEqual(p.returncode, transport.EXIT_UNTRUSTED)
        self.assertNotIn("BODY-RAN", p.stdout)

    def test_code_in_a_subdirectory_is_found(self):
        (self.repo / "lib").mkdir()
        (self.repo / "lib" / "helper.SH").write_text("echo x\n")
        self.assertEqual(self.run_payload(self.repo).returncode, transport.EXIT_UNTRUSTED)

    def test_ignored_generated_scripts_are_allowed(self):
        (self.repo / "build_scripts").mkdir()
        (self.repo / "build_scripts" / "case_build.sh").write_text("echo b\n")
        self.assertEqual(self.run_payload(self.repo).returncode, 0)

    def test_untracked_data_is_allowed(self):
        (self.repo / "matrix.csv").write_text("a\n")
        self.assertEqual(self.run_payload(self.repo).returncode, 0)

    def test_not_a_checkout_fails_closed(self):
        p = self.run_payload(self.dir / "plain")
        self.assertEqual(p.returncode, transport.EXIT_UNTRUSTED)
        self.assertNotIn("BODY-RAN", p.stdout)

    def test_strict_mode_line_numbers_still_name_the_payload_line(self):
        p = self.run_payload(self.repo, body="echo one\nfalse\necho three")
        self.assertIn("payload stopped at line 2", p.stderr)

    def test_payload_includes_check_only_for_trusted_uses(self):
        class Use:
            root = str(self.repo)
        site = transport.Site(name="t", host="h", scheduler="slurm", user="u")
        self.assertNotIn("clean checkout", transport.build_payload(site, "ls\n"))
        self.assertIn("clean checkout", transport.build_payload(site, "ls\n", trust_uses=[Use()]))


if __name__ == "__main__":
    unittest.main()
