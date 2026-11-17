"""Session transfer budget and the transfer record it reads.

An agent once pulled 136 files with 136 hpc-pull calls. Each
was far under the 2 GiB per-call cap, and nothing bounded the sum. The budget
bounds the sum; the audit log's transfer fields are both the record of what
moved and the running total the budget reads.

Nothing here opens a connection. Each test points the audit log at its own
temporary directory and sets its own session id.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hpctether import audit, budget  # noqa: E402
from hpctether.site import Site  # noqa: E402


def _load(name: str):
    spec = importlib.util.spec_from_loader(
        name.replace("-", "_"),
        importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(ROOT / "bin" / name)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hpc_pull = _load("hpc-pull")
hpc_fetch = _load("hpc-fetch")
hpc_audit = _load("hpc-audit")

SITE = Site(name="t", host="h.example", scheduler="slurm", user="u",
            paths={"scratch": "/s/u"})
GB = 1024**3
MB = 1024**2


class _Session:
    """A fresh audit directory and a Claude Code session id for one test."""

    def __init__(self, session: str = "sess-1"):
        self.session = session

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = {"HPC_TETHER_AUDIT": self.tmp.name, "CLAUDECODE": "1",
               "CLAUDE_CODE_SESSION_ID": self.session}
        self.patch = mock.patch.dict(os.environ, env)
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()
        self.tmp.cleanup()

    def rows(self) -> list[dict]:
        out = []
        for p in sorted(Path(self.tmp.name).glob("audit-*.jsonl")):
            out += [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
        return out

    def pulled(self, nbytes: int, files: int = 1, remote: str = "/r/d/f"):
        audit.record("scp", SITE.name, SITE.host, "x", 0,
                     extra=audit.transfer_fields("in", files, nbytes, remote, "/l/f"))


class TestParseSize(unittest.TestCase):
    def test_spellings(self):
        self.assertEqual(budget.parse_size("10G"), 10 * GB)
        self.assertEqual(budget.parse_size("500M"), 500 * MB)
        self.assertEqual(budget.parse_size("1.5GB"), int(1.5 * GB))
        self.assertEqual(budget.parse_size("2GiB"), 2 * GB)
        self.assertEqual(budget.parse_size("4096"), 4096)
        self.assertEqual(budget.parse_size("3g"), 3 * GB)

    def test_rejects(self):
        for bad in ["", "ten", "-1G", "0", "5X"]:
            with self.assertRaises(ValueError, msg=bad):
                budget.parse_size(bad)


class TestTotals(unittest.TestCase):
    def test_counts_both_directions_and_grants(self):
        rows = [
            {"direction": "in", "files": 3, "bytes": 30},
            {"direction": "in", "files": 1, "bytes": 5},
            {"direction": "out", "files": 1, "bytes": 7},
            {"kind": "budget", "rc": 0, "budget_add": 100},
            {"kind": "budget", "rc": 1, "budget_add": 999},  # not granted
            {"kind": "payload", "rc": 0},  # no transfer fields: ignored
        ]
        t = budget.totals(rows)
        self.assertEqual((t["in_files"], t["in_bytes"]), (4, 35))
        self.assertEqual((t["out_files"], t["out_bytes"]), (1, 7))
        self.assertEqual(t["budget"], budget.DEFAULT_BUDGET + 100)


class TestAdmit(unittest.TestCase):
    def test_no_session_no_budget(self):
        with _Session(session=""):
            self.assertIsNone(budget.session_totals())
            self.assertIsNone(budget.admit(SITE, 50 * GB, 0, "pull"))

    def test_within_budget_passes_silently(self):
        with _Session() as s:
            s.pulled(1 * GB)
            self.assertIsNone(budget.admit(SITE, 512 * MB, 0, "pull"))
            self.assertEqual(len(s.rows()), 1)  # nothing added for a pass

    def test_the_loop_is_stopped(self):
        """Many small pulls: each fine alone, the sum is refused."""
        with _Session() as s:
            for _ in range(20):
                s.pulled(100 * MB)
            why = budget.admit(SITE, 100 * MB, 0, "pull")
            self.assertIsNotNone(why)
            self.assertIn("--budget-add", why)
            last = s.rows()[-1]
            self.assertEqual((last["kind"], last["rc"]), ("refused", 77))

    def test_grant_is_recorded_and_persists(self):
        with _Session() as s:
            s.pulled(2 * GB)
            self.assertIsNotNone(budget.admit(SITE, 1 * GB, 0, "fetch"))
            self.assertIsNone(budget.admit(SITE, 1 * GB, 5 * GB, "fetch"))
            grant = s.rows()[-1]
            self.assertEqual(grant["kind"], "budget")
            self.assertEqual(grant["budget_add"], 5 * GB)
            s.pulled(1 * GB)
            # 3 GiB used of 7 GiB: the next pull needs no new grant
            self.assertIsNone(budget.admit(SITE, 3 * GB, 0, "pull"))

    def test_insufficient_grant_is_refused_and_not_recorded(self):
        with _Session() as s:
            s.pulled(2 * GB)
            self.assertIsNotNone(budget.admit(SITE, 5 * GB, 1 * GB, "fetch"))
            self.assertFalse([r for r in s.rows() if r["kind"] == "budget"])

    def test_other_sessions_do_not_count(self):
        with _Session(session="other") as s:
            s.pulled(10 * GB)
        with _Session(session="mine"):
            self.assertIsNone(budget.admit(SITE, 1 * GB, 0, "pull"))

    def test_pushes_are_not_budgeted(self):
        with _Session():
            audit.record("scp", "t", "h", "x", 0,
                         extra=audit.transfer_fields("out", 1, 50 * GB, "/r/f", "/l/f"))
            self.assertIsNone(budget.admit(SITE, 1 * GB, 0, "pull"))


class TestTokens(unittest.TestCase):
    def test_budget_add_is_a_token_in_either_spelling(self):
        for argv in (["hpc-pull", "/r/f", "--budget-add", "5G"],
                     ["hpc-pull", "/r/f", "--budget-add=5G"]):
            with mock.patch.object(sys, "argv", argv):
                self.assertIn("--budget-add", audit._context()["tokens"])


class TestPullEndToEnd(unittest.TestCase):
    """hpc-pull's own path, with the remote stat and scp stubbed."""

    def run_pull(self, size: int, *extra: str) -> tuple[int, list]:
        calls = []

        def fake_scp(argv, timeout, check):
            calls.append(argv)
            Path(argv[-1]).write_bytes(b"x" * 10)
            return mock.Mock(returncode=0)

        with tempfile.TemporaryDirectory() as work, \
                mock.patch.object(hpc_pull.site_mod, "load", return_value=SITE), \
                mock.patch.object(hpc_pull, "remote_stat", return_value=(size, 0)), \
                mock.patch.object(hpc_pull.subprocess, "run", side_effect=fake_scp), \
                mock.patch.object(hpc_pull.manifest, "record",
                                  return_value={"sha256": "0" * 64}):
            rc = hpc_pull.main(["/r/run/f.dat", str(Path(work) / "f.dat"), *extra])
        return rc, calls

    def test_records_what_landed(self):
        with _Session() as s:
            rc, calls = self.run_pull(10)
            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 1)
            row = s.rows()[-1]
            self.assertEqual((row["direction"], row["files"], row["bytes"]), ("in", 1, 10))
            self.assertEqual(row["remote"], "/r/run/f.dat")

    def test_over_budget_sends_nothing(self):
        with _Session() as s:
            s.pulled(int(1.9 * GB))
            rc, calls = self.run_pull(200 * MB)
            self.assertEqual(rc, 77)
            self.assertEqual(calls, [])

    def test_budget_add_lets_it_through(self):
        with _Session() as s:
            s.pulled(int(1.9 * GB))
            rc, calls = self.run_pull(200 * MB, "--budget-add", "1G")
            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 1)

    def test_bad_budget_add_is_usage(self):
        with _Session():
            rc, calls = self.run_pull(10, "--budget-add", "lots")
            self.assertEqual((rc, calls), (64, []))


class TestFetchLanded(unittest.TestCase):
    def test_counts_only_listed_files_on_disk(self):
        with tempfile.TemporaryDirectory() as d:
            dst = Path(d)
            (dst / "sub").mkdir()
            (dst / "a").write_bytes(b"12345")
            (dst / "sub" / "b").write_bytes(b"12")
            (dst / "stale-local").write_bytes(b"x" * 100)  # --merge leftover
            fields = hpc_fetch.landed(dst, [(5, "a"), (2, "sub/b"), (9, "never-arrived")], "/r/run")
        self.assertEqual((fields["files"], fields["bytes"]), (2, 7))
        self.assertEqual((fields["direction"], fields["remote"]), ("in", "/r/run"))


class TestTransferSummary(unittest.TestCase):
    def test_one_line_per_directory_not_per_file(self):
        rows = []
        for i in range(68):
            rows.append({"session": "abcdef123456", "site": "cluster", "kind": "scp",
                         "ts": f"2026-09-22T17:{26 + i // 10:02d}:00+00:00",
                         **audit.transfer_fields("in", 1, 500_000,
                                                 f"/g/opacity/rundir/run_a/out_a_bin{i:04d}.dat",
                                                 f"/w/hpcdata/run_a/out_a_bin{i:04d}.dat")})
        rows.append({"session": "abcdef123456", "site": "cluster", "kind": "rsync",
                     "ts": "2026-09-22T17:40:00+00:00",
                     **audit.transfer_fields("in", 3, 3 * MB, "/g/opacity/rundir/run_b",
                                             "/w/hpcdata/run_b")})
        rows.append({"session": "abcdef123456", "kind": "refused", "rc": 77,
                     "detail": "session budget: 2.1 GB > 2.0 GB"})
        out = hpc_audit.summarize_transfers(rows)
        self.assertEqual(len(out), 3)  # header + two directories
        self.assertIn("71 files", out[0])
        self.assertIn("17:26–17:40 UTC", out[0])
        self.assertIn("1 refused over budget", out[0])
        self.assertTrue(any("68 files" in l and "rundir/run_a " in l for l in out[1:]))
        self.assertTrue(any("3 files" in l and "run_b" in l for l in out[1:]))

    def test_nothing_moved(self):
        self.assertEqual(hpc_audit.summarize_transfers([{"kind": "payload"}]),
                         ["no transfers recorded"])


if __name__ == "__main__":
    unittest.main()
