"""hpc-run refuses a payload that names an undefined `$HPC_*` role."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from hpctether import transport

from test_profile import Sites
from test_trust import _load_bin, _run


class _Audit:
    """A private audit log for one test, read back as rows."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._prev = os.environ.get("HPC_TETHER_AUDIT")
        os.environ["HPC_TETHER_AUDIT"] = self._tmp.name
        return self

    def __exit__(self, *exc):
        if self._prev is None:
            os.environ.pop("HPC_TETHER_AUDIT", None)
        else:
            os.environ["HPC_TETHER_AUDIT"] = self._prev
        self._tmp.cleanup()

    def rows(self):
        out = []
        for f in sorted(Path(self._tmp.name).glob("audit-*.jsonl")):
            out += [json.loads(line) for line in f.read_text().splitlines() if line]
        return out


def run(level, payload, *tokens, python_bin=True):
    mod = _load_bin("hpc-run")
    sites = Sites(level)
    if python_bin:
        sites.text = sites.text.replace("[paths]\n", '[paths]\npython_bin = "/opt/py/bin"\n')
    with sites, _Audit() as log:
        code, _, err = _run(mod, ["--site", "testsite", *tokens, "-"], payload)
        return code, err, log.rows()


class TestUndefinedRole(unittest.TestCase):
    """A payload naming a `$HPC_*` the site does not define (an agent's guess
    such as `$HPC_MYTOOLS`) is refused with that name and the defined ones,
    not with the generic "python executes arbitrary source"."""

    def test_refused_with_the_name_and_the_defined_ones(self):
        code, err, rows = run("improvise", "cd $HPC_MYTOOLS && python my-script.py check A\n")
        self.assertEqual(code, transport.EXIT_USAGE)
        self.assertIn("$HPC_MYTOOLS is not defined", err)
        self.assertIn("$HPC_SCRATCH", err)
        self.assertIn("$HPC_SITE  testsite", err)
        self.assertNotIn("arbitrary source", err)
        self.assertEqual(rows[-1]["kind"], "refused")
        self.assertEqual(rows[-1]["refusal"], "undefined $HPC_MYTOOLS")
        self.assertIn("$HPC_MYTOOLS", rows[-1]["detail"])

    def test_missing_python_bin_says_so(self):
        code, err, _ = run("improvise", 'export PATH="$HPC_PYTHON_BIN:$PATH"\npython -V\n',
                           "--allow-exec", python_bin=False)
        self.assertEqual(code, transport.EXIT_USAGE)
        self.assertIn("sets no python_bin", err)

    def test_braced_spelling_caught(self):
        code, err, _ = run("prepare", "ls ${HPC_NOPE}/x\n")
        self.assertEqual(code, transport.EXIT_USAGE)
        self.assertIn("$HPC_NOPE", err)

    def test_defined_and_assigned_names_pass(self):
        mod = _load_bin("hpc-run")
        payload = 'HPC_MINE=/x\necho "$HPC_MINE" "$HPC_SITE" "$HPC_USER"\nls $HPC_SCRATCH\n'
        self.assertEqual(mod._undefined_roles(payload, _site()), [])

    def test_prelude_names_match_transport(self):
        # hpc-run's list of defined names must be the prelude's.
        s = _site()
        script = transport.build_payload(s, "true\n", 30)
        for name in (*transport.FIXED_ROLES, *s.env()):
            self.assertIn(f"export {name}=", script)


def _site():
    from hpctether import site as site_mod
    with Sites("improvise"):
        return site_mod.load("testsite")


if __name__ == "__main__":
    unittest.main()
