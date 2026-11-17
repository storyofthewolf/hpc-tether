"""The interpreter check in hpctether/__init__.py.

The full re-exec path needs a <3.11 interpreter and is checked by hand
(`PATH=/usr/bin:... /usr/bin/python3 bin/hpc-check`); these pin the parts
that run everywhere.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hpctether


class TestPythonCheck(unittest.TestCase):
    def test_current_interpreter_passes(self):
        self.assertGreaterEqual(sys.version_info[:2], hpctether.MIN_PYTHON)
        hpctether._ensure_python()          # returns, does not exit

    def test_version_probe(self):
        self.assertEqual(hpctether._version_of(sys.executable), tuple(sys.version_info[:2]))
        self.assertIsNone(hpctether._version_of("/nonexistent/python3"))

    def test_package_source_parses_on_old_pythons(self):
        # __init__ must run on 3.9 to print its message, so no 3.10+ syntax.
        import ast
        src = (Path(hpctether.__file__)).read_text()
        ast.parse(src, feature_version=(3, 8))


if __name__ == "__main__":
    unittest.main()
