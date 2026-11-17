"""hpc-status: settings and this session's tally, for the user.

Each test points the audit log and the sites directory at a temp directory
itself, so it is isolated under unittest as well as pytest.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "bin" / "hpc-status"


def entry(**kw):
    kw.setdefault("ts", "2026-11-10T21:00:00+00:00")
    kw.setdefault("session", "abcdef12-0000")
    kw.setdefault("site", "cluster")
    kw.setdefault("kind", "payload")
    kw.setdefault("rc", 0)
    kw.setdefault("tokens", [])
    return kw


class TestStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.log = root / "audit.jsonl"
        self.sites = root / "sites"
        self.sites.mkdir()
        self.env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        self.env.update(HPC_TETHER_AUDIT=str(self.log), HPC_TETHER_SITES=str(self.sites))

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, *entries):
        self.log.write_text("".join(json.dumps(e) + "\n" for e in entries))

    def run_status(self, *args, session=None):
        env = dict(self.env)
        if session:
            env["CLAUDE_CODE_SESSION_ID"] = session
        p = subprocess.run([sys.executable, str(BIN), *args], env=env,
                           capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr

    def test_no_site_file_said_plainly(self):
        rc, out, _ = self.run_status()
        self.assertEqual(rc, 0)
        self.assertIn("no site file in", out)
        self.assertIn("no Claude Code session in the audit log yet", out)

    def test_tally_for_the_running_session(self):
        self.write(
            entry(verb="hpc-jobs", argv="--job 1"),
            entry(verb="hpc-run", level="prepare", tokens=["--allow-write"], detail="mkdir x"),
            entry(verb="hpc-run", kind="refused", rc=77, detail="rm x"),
            entry(verb="hpc-read", argv="rundir:log", rc=2),
            entry(verb="hpc-pull", kind="scp", direction="in", files=2, bytes=2048, remote="/a/b"),
            entry(verb="hpc-jobs", session="other-session"),
        )
        rc, out, _ = self.run_status(session="abcdef12-0000")
        self.assertEqual(rc, 0)
        self.assertIn("session abcdef12: 5 cluster calls since 21:00 UTC", out)
        self.assertIn("read: 1 · pulled: 1 · write: 1 · refused: 1 · failed: 1", out)
        self.assertIn("data: in 2 files, 2.0 KB · out 0 files, 0 B", out)
        self.assertIn("last 5:", out)
        self.assertIn("REFUSED, nothing sent", out)

    def test_session_by_prefix_and_last(self):
        self.write(entry(verb="hpc-jobs"), entry(verb="hpc-ls", argv="scratch:"))
        rc, out, _ = self.run_status("--session", "abcd", "--last", "1")
        self.assertEqual(rc, 0)
        self.assertIn("session abcdef12: 2 cluster calls", out)
        self.assertIn("last 1:", out)
        self.assertIn("hpc-ls scratch:", out)
        self.assertNotIn("hpc-jobs ·", out)

    def test_ambiguous_prefix_is_a_usage_error(self):
        self.write(entry(session="abc-1"), entry(session="abc-2"))
        rc, _, err = self.run_status("--session", "abc")
        self.assertEqual(rc, 64)
        self.assertIn("matches 2 sessions", err)

    def test_outside_claude_code_uses_most_recent_session(self):
        self.write(entry(session="first-1"), entry(session="second-2", verb="hpc-jobs"))
        rc, out, _ = self.run_status()
        self.assertIn("session second-2: 1 cluster calls", out)
        self.assertIn("most recent in the log", out)

    def test_session_with_nothing_yet(self):
        self.write(entry(session="someone-else"))
        rc, out, _ = self.run_status(session="fresh-session")
        self.assertIn("session fresh-se: no cluster calls yet", out)


if __name__ == "__main__":
    unittest.main()
