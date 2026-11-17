"""Queue-parsing tests for hpc-jobs.

Regression coverage for the bug where the queue reported "(none)" while a job
was running: the separator was written as the escape sequence ``\x1f`` rather
than the byte, bash passed it through literally, and the parser silently
dropped every row it could not split.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_BIN = Path(__file__).resolve().parent.parent / "bin" / "hpc-jobs"
sys.path.insert(0, str(_BIN.parent.parent))

_spec = importlib.util.spec_from_loader(
    "hpc_jobs", importlib.machinery.SourceFileLoader("hpc_jobs", str(_BIN))
)
hpc_jobs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hpc_jobs)


class TestSeparator(unittest.TestCase):
    def test_sep_is_the_real_byte(self):
        """The separator must be one 0x1f byte, not a 4-char escape sequence.

        bash leaves ``\\x1f`` in a double-quoted string as literal text, so an
        escape sequence here makes squeue emit a separator the parser can
        never find.
        """
        self.assertEqual(hpc_jobs.SEP, "\x1f")
        self.assertEqual(len(hpc_jobs.SEP), 1)

    def test_format_string_carries_the_byte(self):
        self.assertIn("\x1f", hpc_jobs.SQUEUE_FORMAT)
        self.assertNotIn("\\x1f", hpc_jobs.SQUEUE_FORMAT)

    def test_payload_scripts_carry_the_byte(self):
        for script in (hpc_jobs._QUEUE_SCRIPT, hpc_jobs._JOB_SCRIPT):
            self.assertIn("\x1f", script)
            self.assertNotIn("\\x1f", script)


class TestParseSqueue(unittest.TestCase):
    def test_parses_a_running_job(self):
        line = "\x1f".join(
            ["58083357", "exovolc_ben1_hunga_10x", "RUNNING", "1:36:42", "1"]
        )
        (rec,) = hpc_jobs._parse_squeue(line + "\n")
        self.assertEqual(rec["jobid"], "58083357")
        self.assertEqual(rec["name"], "exovolc_ben1_hunga_10x")
        self.assertEqual(rec["state"], "RUNNING")
        self.assertEqual(rec["elapsed"], "1:36:42")
        self.assertEqual(rec["nodes"], "1")

    def test_pending_reason_is_shown(self):
        """A PENDING job showed no reason for waiting."""
        import contextlib, io
        S = hpc_jobs.SEP
        self.assertTrue(hpc_jobs.SQUEUE_FORMAT.endswith(S + "%r"))
        (rec,) = hpc_jobs._parse_squeue(S.join(["7", "j", "PENDING", "0:00", "1", "Priority"]) + "\n")
        self.assertEqual(rec["reason"], "Priority")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            hpc_jobs._print_table([rec], "queue")
        self.assertIn("REASON", out.getvalue())
        self.assertIn("Priority", out.getvalue())
        # an old five-field row still parses, and sacct's table keeps six columns
        (old,) = hpc_jobs._parse_squeue(S.join(["7", "j", "RUNNING", "1:00", "1"]) + "\n")
        self.assertIsNone(old["reason"])

    def test_job_name_containing_spaces_survives(self):
        line = "\x1f".join(["1", "my job name", "PENDING", "0:00", "2"])
        (rec,) = hpc_jobs._parse_squeue(line + "\n")
        self.assertEqual(rec["name"], "my job name")

    def test_genuinely_empty_queue_is_empty_not_an_error(self):
        self.assertEqual(hpc_jobs._parse_squeue(""), [])
        self.assertEqual(hpc_jobs._parse_squeue("\n  \n"), [])

    def test_literal_escape_output_raises_rather_than_reporting_empty(self):
        """The exact bug: bash emitted ``\\x1f`` as text.

        This must be loud. Reporting "(none)" here is indistinguishable from a
        genuinely empty queue, and under --name it would green-light a
        duplicate submission onto a live job's output directory.
        """
        bad = "58083357\\x1fexovolc_ben1_hunga_10x\\x1fRUNNING\\x1f1:36:42\\x1f1\n"
        with self.assertRaises(hpc_jobs.ParseError) as ctx:
            hpc_jobs._parse_squeue(bad)
        self.assertIn("58083357", str(ctx.exception))

    def test_partial_parse_now_fails_closed(self):
        """An unreadable row fails the whole parse, even beside readable ones.

        This reverses the earlier `test_partial_parse_keeps_good_records`,
        which asserted the good rows were kept and the bad line dropped. That
        is a fail-open: the dropped row may be the very job a `--name` check is
        looking for, so "the rows I could read" gets reported as the queue and
        a duplicate submission is green-lit on a partial read. An unknown must
        read as unknown.
        """
        good = "\x1f".join(["1", "ok", "RUNNING", "0:10", "1"])
        with self.assertRaises(hpc_jobs.ParseError) as ctx:
            hpc_jobs._parse_squeue(good + "\ngarbage-line\n")
        self.assertIn("garbage-line", str(ctx.exception))

    def test_clean_block_still_parses(self):
        rows = hpc_jobs._parse_squeue(
            "\x1f".join(["1", "ok", "RUNNING", "0:10", "1"]) + "\n"
            + "\x1f".join(["2", "two", "PENDING", "0:00", "4"]) + "\n"
        )
        self.assertEqual([r["name"] for r in rows], ["ok", "two"])

    def test_empty_block_is_an_empty_queue(self):
        # Distinct from a failed query, which now exits 70 remotely and never
        # reaches this parser with empty stdout.
        self.assertEqual(hpc_jobs._parse_squeue(""), [])


if __name__ == "__main__":
    unittest.main()


class TestJobQueryFailsClosed(unittest.TestCase):
    """A failed scheduler query must never be indistinguishable from no rows.

    ``_QUEUE_SCRIPT`` already failed closed; ``_JOB_SCRIPT`` suppressed both
    queries with ``|| true``, so an outage rendered as an empty job. The asymmetry with the queue script is that here one source may
    legitimately be empty -- a finished job leaves squeue and lives in sacct --
    so the requirement is that an ERROR is reported, not that rows exist.
    """

    def test_neither_query_is_silenced(self):
        script = hpc_jobs._JOB_SCRIPT
        self.assertNotIn("|| true", script)
        self.assertNotIn("2>/dev/null", script)

    def test_both_failing_exits_remote_query(self):
        self.assertIn("exit 70", hpc_jobs._JOB_SCRIPT)

    def run_job_script(self, squeue_body):
        """Run _JOB_SCRIPT with stand-in squeue and sacct first on PATH."""
        with tempfile.TemporaryDirectory() as d:
            for name, body in (("squeue", squeue_body),
                               ("sacct", "echo '42|COMPLETED|01:00:00|0:0'")):
                p = os.path.join(d, name)
                with open(p, "w") as f:
                    f.write("#!/bin/bash\n" + body + "\n")
                os.chmod(p, 0o755)
            env = dict(os.environ, PATH=d + os.pathsep + os.environ["PATH"],
                       HPC_HOST="testhost")
            return subprocess.run(["bash", "-ec", hpc_jobs._JOB_SCRIPT, "_", "42"],
                                  capture_output=True, text=True, env=env)

    def test_unknown_job_id_is_an_answer_not_a_failure(self):
        """squeue exits 1 for a job that has left the queue; that is 'not
        queued', not an outage, and its complaint is not shown."""
        p = self.run_job_script(
            "echo 'slurm_load_jobs error: Invalid job id specified' >&2; exit 1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stderr, "")
        self.assertIn("42|COMPLETED", p.stdout)

    def test_other_squeue_errors_are_shown_and_count(self):
        p = self.run_job_script("echo 'slurm_load_jobs error: Socket timed out' >&2; exit 1")
        self.assertIn("Socket timed out", p.stderr)
        self.assertIn("squeue failed for job 42", p.stderr)
        self.assertIn("42|COMPLETED", p.stdout)

    def test_partial_failure_says_unavailable_not_empty(self):
        self.assertIn("accounting history is unavailable, not empty",
                      hpc_jobs._JOB_SCRIPT)
