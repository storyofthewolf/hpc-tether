"""Audit log: every payload the transport handles leaves a line.

Nothing here opens a connection. The refusal path is exercised for real
(nothing is sent, so nothing needs faking); the sent path is exercised by
replacing subprocess.run with a stub that returns a completed process.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hpctether import audit, transport
from hpctether.site import Site

_spec = importlib.util.spec_from_loader(
    "hpc_audit",
    importlib.machinery.SourceFileLoader("hpc_audit", str(ROOT / "bin" / "hpc-audit")),
)
hpc_audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hpc_audit)

SITE = Site(name="t", host="h.example", scheduler="slurm", user="u",
            paths={"scratch": "/s/u"})


class _Log:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "audit.jsonl"
        self.previous = os.environ.get("HPC_TETHER_AUDIT")
        os.environ["HPC_TETHER_AUDIT"] = str(self.path)
        return self

    def __exit__(self, *exc):
        if self.previous is None:
            os.environ.pop("HPC_TETHER_AUDIT", None)
        else:
            os.environ["HPC_TETHER_AUDIT"] = self.previous
        self.tmp.cleanup()

    def rows(self):
        if not self.path.is_file():
            return []
        return [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]


class TestRecord(unittest.TestCase):
    def test_writes_a_line_with_context(self):
        with _Log() as log:
            os.environ["CLAUDE_CODE_SESSION_ID"] = "sess-123"
            try:
                audit.record("payload", "t", "h", "squeue -u u", 0, seconds=1.234, payload="squeue -u u\n")
            finally:
                del os.environ["CLAUDE_CODE_SESSION_ID"]
            rows = log.rows()
            self.assertEqual(len(rows), 1)
            r = rows[0]
            self.assertEqual(r["session"], "sess-123")
            self.assertEqual(r["kind"], "payload")
            self.assertEqual(r["rc"], 0)
            self.assertEqual(r["seconds"], 1.23)
            self.assertEqual(len(r["payload_sha256"]), 64)
            self.assertIn("ts", r)
            self.assertEqual(oct(log.path.stat().st_mode & 0o777), "0o600")

    def test_never_raises(self):
        previous = os.environ.get("HPC_TETHER_AUDIT")
        os.environ["HPC_TETHER_AUDIT"] = "/nonexistent-root-dir/x/audit.jsonl"
        try:
            audit.record("payload", "t", "h", "x", 0)  # must not raise
        finally:
            if previous is None:
                os.environ.pop("HPC_TETHER_AUDIT", None)
            else:
                os.environ["HPC_TETHER_AUDIT"] = previous

    def test_first_line_skips_python_path_prelude(self):
        script = 'export PATH="$HPC_PYTHON_BIN:$PATH"\npython -c "print(1)"\n'
        self.assertEqual(audit.first_line(script), 'python -c "print(1)"')
        self.assertEqual(audit.first_line("export PATH=/x:$PATH\n"), "export PATH=/x:$PATH")

    def test_first_line_skips_prelude(self):
        self.assertEqual(audit.first_line("# c\nset -- 'a'\n\ntail -5 f\n"), "tail -5 f")


class TestTransportLogs(unittest.TestCase):
    def test_refusal_is_logged_and_nothing_sent(self):
        with _Log() as log:
            with self.assertRaises(transport.TransportError):
                transport.run_remote(SITE, "rm -rf /x\n", preflight=False)
            rows = log.rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["kind"], "refused")
            self.assertEqual(rows[0]["rc"], 77)

    def test_sent_payload_is_logged(self):
        with _Log() as log:
            orig = subprocess.run

            def fake(argv, **kw):
                return subprocess.CompletedProcess(argv, 3, stdout="out", stderr="")

            subprocess.run = fake
            try:
                res = transport.run_remote(SITE, "tail -5 f\n", preflight=False, capture=True)
            finally:
                subprocess.run = orig
            self.assertEqual(res.returncode, 3)
            rows = log.rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["kind"], "payload")
            self.assertEqual(rows[0]["detail"], "tail -5 f")
            self.assertEqual(rows[0]["rc"], 3)
            self.assertEqual(rows[0]["host"], "h.example")


class _Dir:
    """HPC_TETHER_AUDIT pointed at a directory: the monthly-rotation mode."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.previous = os.environ.get("HPC_TETHER_AUDIT")
        os.environ["HPC_TETHER_AUDIT"] = str(self.dir)
        return self

    def __exit__(self, *exc):
        if self.previous is None:
            os.environ.pop("HPC_TETHER_AUDIT", None)
        else:
            os.environ["HPC_TETHER_AUDIT"] = self.previous
        self.tmp.cleanup()

    def write_month(self, month: str, details: list[str]) -> None:
        lines = [
            json.dumps({"ts": f"{month}-05T00:00:00+00:00", "kind": "payload",
                        "rc": 0, "detail": d})
            for d in details
        ]
        (self.dir / f"audit-{month}.jsonl").write_text("\n".join(lines) + "\n")


class TestMonthlyFiles(unittest.TestCase):
    def test_record_writes_the_month_file(self):
        with _Dir() as d:
            audit.record("payload", "t", "h", "squeue", 0)
            month = datetime.now(timezone.utc).strftime("%Y-%m")
            path = d.dir / f"audit-{month}.jsonl"
            self.assertTrue(path.is_file())
            self.assertEqual([p.name for p in d.dir.glob("*.jsonl")], [path.name])
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")

    def test_month_of_uses_the_entry_timestamp(self):
        self.assertEqual(audit.month_of("2031-02-28T12:00:00+00:00"), "2031-02")

    def test_audit_path_for_a_named_month(self):
        with _Dir() as d:
            self.assertEqual(audit.audit_path("2030-07"), d.dir / "audit-2030-07.jsonl")

    def test_jsonl_override_is_one_exact_file(self):
        with _Log() as log:
            audit.record("payload", "t", "h", "squeue", 0)
            self.assertTrue(log.path.is_file())
            self.assertEqual(audit.audit_path("2030-07"), log.path)
            self.assertEqual(audit.existing_months(), [])

    def test_existing_months_are_sorted(self):
        with _Dir() as d:
            d.write_month("2030-03", ["a"])
            d.write_month("2029-12", ["b"])
            self.assertEqual(audit.existing_months(), ["2029-12", "2030-03"])


class TestHpcAuditAcrossMonths(unittest.TestCase):
    def _months(self, d):
        """Two months strictly older than the current one, plus the current one."""
        now = datetime.now(timezone.utc)
        older = f"{now.year - 2:04d}-11"
        mid = f"{now.year - 1:04d}-12"
        current = now.strftime("%Y-%m")
        d.write_month(older, ["old-one"])
        d.write_month(mid, ["mid-one"])
        d.write_month(current, ["now-one"])
        return older, mid, current

    def test_default_reads_the_current_month_only(self):
        with _Dir() as d:
            self._months(d)
            paths = hpc_audit.select_paths(None, False)
            rows = [r for p in paths for r in hpc_audit._rows(p)]
            self.assertEqual([r["detail"] for r in rows], ["now-one"])

    def test_since_reads_forward_in_order(self):
        with _Dir() as d:
            _older, mid, _current = self._months(d)
            paths = hpc_audit.select_paths(mid, False)
            rows = [r for p in paths for r in hpc_audit._rows(p)]
            self.assertEqual([r["detail"] for r in rows], ["mid-one", "now-one"])

    def test_all_reads_every_month(self):
        with _Dir() as d:
            self._months(d)
            paths = hpc_audit.select_paths(None, True)
            rows = [r for p in paths for r in hpc_audit._rows(p)]
            self.assertEqual([r["detail"] for r in rows],
                             ["old-one", "mid-one", "now-one"])

    def test_main_prints_across_months(self):
        with _Dir() as d:
            self._months(d)
            self.assertEqual(hpc_audit.main(["--all", "--json"]), 0)

    def test_bad_since_is_a_usage_error(self):
        with _Dir() as d:
            self._months(d)
            self.assertEqual(hpc_audit.main(["--since", "2029-13"]), 64)
            self.assertEqual(hpc_audit.main(["--since", "nov"]), 64)


if __name__ == "__main__":
    unittest.main()


class TestAuditView(unittest.TestCase):
    """What hpc-audit prints for a refused entry: the recorded reason,
    so the agent need not guess it."""

    def test_why_prints_the_recorded_refusal(self):
        self.assertEqual(hpc_audit.why({"kind": "refused", "refusal": "needs --allow-exec",
                                        "level": "improvise"}), "needs --allow-exec")

    def test_why_says_not_recorded_for_older_entries(self):
        self.assertEqual(hpc_audit.why({"kind": "refused", "level": "prepare"}),
                         "not recorded (the payload needed profile prepare)")
        self.assertEqual(hpc_audit.why({"kind": "refused"}), "not recorded")

    def test_why_is_silent_for_sent_entries(self):
        self.assertIsNone(hpc_audit.why({"kind": "payload", "rc": 0}))

    def test_summary_skips_an_old_path_prelude(self):
        row = {"kind": "payload", "verb": "hpc-run",
               "detail": 'export PATH="$HPC_PYTHON_BIN:$PATH"',
               "body": 'export PATH="$HPC_PYTHON_BIN:$PATH"\npython -c "print(1)"\n'}
        self.assertEqual(hpc_audit.summary(row), 'python -c "print(1)"')
