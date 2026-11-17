"""The queue check before a resubmit is part of the submission (§3 issue 4)."""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from hpctether import guard, transport

from test_profile import Sites
from test_trust import _load_bin, _run


class TestJobNames(unittest.TestCase):
    def test_literal_spellings(self):
        for line in ("sbatch -J grp4_pt05 job.sh", "sbatch -Jgrp4_pt05 job.sh",
                     "sbatch --job-name grp4_pt05 job.sh", "sbatch --job-name=grp4_pt05 job.sh",
                     "cd $HPC_CASE && sbatch -J grp4_pt05 job.sh"):
            with self.subTest(line=line):
                self.assertEqual(guard.sbatch_job_names(line + "\n"), (["grp4_pt05"], []))

    def test_unnamed_or_variable_is_unchecked(self):
        for line in ("sbatch job.sh", "sbatch -J $NAME job.sh", 'sbatch --job-name="$(x)" j.sh'):
            with self.subTest(line=line):
                names, unchecked = guard.sbatch_job_names(line + "\n")
                self.assertEqual(names, [])
                self.assertEqual(len(unchecked), 1)

    def test_no_sbatch_no_names(self):
        self.assertEqual(guard.sbatch_job_names("squeue -u me\necho sbatch -J x\n"), ([], []))


class TestCheckOnTheCluster(unittest.TestCase):
    """duplicate_check run in bash with a fake squeue on PATH."""

    def setUp(self):
        self.bin = Path(tempfile.mkdtemp())

    def fake_squeue(self, body):
        f = self.bin / "squeue"
        f.write_text("#!/bin/sh\n" + body + "\n")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)

    def run_payload(self, names=("job1",)):
        check = transport.duplicate_check(list(names))
        inner = "\n".join([*transport.strict_prelude(len(check)), *check, "echo BODY-RAN"])
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HPC_USER": "tester"}
        return subprocess.run(["/bin/bash", "-s"], input=f"bash <<'__B__'\n{inner}\n__B__\n",
                              capture_output=True, text=True, env=env)

    def test_empty_queue_runs(self):
        self.fake_squeue("exit 0")
        p = self.run_payload()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("BODY-RAN", p.stdout)

    def test_active_job_stops_everything(self):
        self.fake_squeue('echo "59088559 RUNNING"')
        p = self.run_payload()
        self.assertEqual(p.returncode, transport.EXIT_DUPLICATE)
        self.assertNotIn("BODY-RAN", p.stdout)
        self.assertIn("59088559 RUNNING", p.stderr)

    def test_squeue_failure_fails_closed(self):
        self.fake_squeue("echo 'slurm_load_jobs error' >&2; exit 1")
        p = self.run_payload()
        self.assertEqual(p.returncode, transport.EXIT_DUPLICATE)
        self.assertNotIn("BODY-RAN", p.stdout)
        self.assertIn("unknown", p.stderr)

    def test_squeue_missing_fails_closed(self):
        p = self.run_payload()  # no fake: /usr/bin and /bin have no squeue here
        self.assertEqual(p.returncode, transport.EXIT_DUPLICATE)
        self.assertNotIn("BODY-RAN", p.stdout)

    def test_squeue_asked_for_this_user_name_and_active_states(self):
        self.fake_squeue('echo "$@" >&2; exit 0')
        p = self.run_payload(["grp4_pt05"])
        self.assertIn("-u tester", p.stderr)
        self.assertIn("-n grp4_pt05", p.stderr)
        self.assertIn("PENDING,RUNNING", p.stderr)


class TestHpcRunWiring(unittest.TestCase):
    def test_dry_run_payload_carries_the_check(self):
        mod = _load_bin("hpc-run")
        with Sites("operate"):
            code, out, err = _run(mod, ["--site", "testsite", "--dry-run", "--allow-write", "-"],
                                  "sbatch -J grp4_pt05 /scratch/tester/job.sh\n")
        self.assertEqual(code, 0, err)
        self.assertIn("-n grp4_pt05", out)

    def test_unnamed_submission_gets_a_note(self):
        mod = _load_bin("hpc-run")
        with Sites("operate"):
            code, out, err = _run(mod, ["--site", "testsite", "--dry-run", "--allow-write", "-"],
                                  "sbatch /scratch/tester/job.sh\n")
        self.assertEqual(code, 0, err)
        self.assertIn("hpc-jobs --name", err)
        self.assertNotIn("squeue -u", out)


if __name__ == "__main__":
    unittest.main()
