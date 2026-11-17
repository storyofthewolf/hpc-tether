"""Argument validation for hpc-probe.

``--job`` and ``--pid`` are interpolated into a nested ``sh -c`` inside the
srun payload, where the inner quoting puts them back into shell source. Neither
was validated, so a ``--pid`` of ``1; printf x`` executed that printf and the
transport's scan allowed the payload. The probe is treated
as an unconditional read-only exception, which is only true if its arguments
cannot carry a command.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import unittest
from pathlib import Path

_BIN = Path(__file__).resolve().parent.parent / "bin" / "hpc-probe"
sys.path.insert(0, str(_BIN.parent.parent))

_spec = importlib.util.spec_from_loader(
    "hpc_probe", importlib.machinery.SourceFileLoader("hpc_probe", str(_BIN))
)
hpc_probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hpc_probe)


class TestPidValidation(unittest.TestCase):
    def test_a_pid_is_digits_only(self):
        for good in ("1", "12345", "999999"):
            self.assertTrue(hpc_probe._PID_RE.match(good), good)

    def test_shell_metacharacters_are_refused(self):
        for bad in (
            "1; printf INJECTED",
            "1$(id)",
            "`id`",
            "1 2",
            "1|id",
            "1&&id",
            "",
            "-1",
            "1\nid",
        ):
            self.assertIsNone(hpc_probe._PID_RE.match(bad), bad)


class TestJobIdValidation(unittest.TestCase):
    def test_real_slurm_shapes_are_accepted(self):
        for good in ("123", "123_4", "123+1", "123_[5-9]", "123_1,2"):
            self.assertTrue(hpc_probe._JOBID_RE.match(good), good)

    def test_shell_metacharacters_are_refused(self):
        for bad in ("123; id", "$(id)", "abc", "", "1 2", "123|id", "12`id`"):
            self.assertIsNone(hpc_probe._JOBID_RE.match(bad), bad)


class TestMainRejectsInjection(unittest.TestCase):
    """Validation must happen before any transport call, not inside the payload."""

    def test_bad_pid_is_a_usage_error(self):
        code = hpc_probe.main(
            ["io", "--job", "123", "--pid", "1; printf INJECTED", "--site", "nope"]
        )
        self.assertEqual(code, hpc_probe.transport.EXIT_USAGE)

    def test_bad_job_is_a_usage_error(self):
        code = hpc_probe.main(["ps", "--job", "123; id", "--site", "nope"])
        self.assertEqual(code, hpc_probe.transport.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
