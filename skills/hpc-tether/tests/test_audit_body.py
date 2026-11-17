"""Every hpc-run payload is kept whole in the audit log."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hpctether import audit, transport

from test_profile import Sites
from test_trust import _load_bin, _run
from test_undefined_names import run as run_payload


class TestRecordBody(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "a.jsonl"
        self._prev = os.environ.get("HPC_TETHER_AUDIT")
        os.environ["HPC_TETHER_AUDIT"] = str(self.log)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("HPC_TETHER_AUDIT", None)
        else:
            os.environ["HPC_TETHER_AUDIT"] = self._prev
        self.tmp.cleanup()

    def last(self):
        return json.loads(self.log.read_text().splitlines()[-1])

    def test_body_kept_when_given(self):
        audit.record("payload", "s", "h", "python - <<EOF", 0, payload="x", body="python - <<EOF\nprint(1)\nEOF\n")
        self.assertIn("print(1)", self.last()["body"])
        self.assertNotIn("body_truncated", self.last())

    def test_no_body_by_default(self):
        audit.record("payload", "s", "h", "ls", 0, payload="ls\n")
        self.assertNotIn("body", self.last())

    def test_body_truncated_at_limit(self):
        audit.record("payload", "s", "h", "x", 0, body="x" * (audit.BODY_LIMIT + 10))
        row = self.last()
        self.assertEqual(len(row["body"]), audit.BODY_LIMIT)
        self.assertTrue(row["body_truncated"])
        self.assertEqual(row["body_length"], audit.BODY_LIMIT + 10)

    def test_body_at_limit_is_not_marked(self):
        audit.record("payload", "s", "h", "x", 0, body="x" * audit.BODY_LIMIT)
        self.assertNotIn("body_truncated", self.last())
        self.assertNotIn("body_length", self.last())

    def test_log_file_is_private(self):
        self.log.write_text("")
        os.chmod(self.log, 0o644)
        audit.record("payload", "s", "h", "ls", 0, payload="ls\n")
        self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)

    def test_full_prints_every_body_and_marks_a_cut(self):
        audit.record("payload", "s", "h", "export PATH=x", 0,
                     body="export PATH=x\nsqueue -u me\n", extra={"level": "observe"})
        audit.record("payload", "s", "h", "y", 0, body="y" * (audit.BODY_LIMIT + 5),
                     extra={"level": "observe"})
        code, out, _ = _run(_load_bin("hpc-audit"), ["--full"])
        self.assertEqual(code, 0)
        self.assertIn("    | squeue -u me", out)
        self.assertIn(f"cut at {audit.BODY_LIMIT} of {audit.BODY_LIMIT + 5} characters", out)

    def test_code_selects_improvise_only(self):
        audit.record("payload", "s", "h", "ls", 0, body="ls /x\n", extra={"level": "observe"})
        audit.record("payload", "s", "h", "python -c", 0, body="python -c 'print(7)'\n",
                     extra={"level": "improvise"})
        audit.record("payload", "s", "h", "python -", 0, body="python - <<EOF\nold\nEOF\n")
        code, out, _ = _run(_load_bin("hpc-audit"), ["--code"])
        self.assertIn("print(7)", out)
        self.assertIn("    | old", out)              # an old line: body meant improvise
        self.assertNotIn("ls /x", out)

    def test_fixed_tool_line_shows_its_arguments(self):
        with mock.patch.object(audit.sys, "argv", ["/x/bin/hpc-read", "rundir:a.log", "--tail", "5"]):
            audit.record("payload", "s", "h", "set +o pipefail", 0, payload="set +o pipefail\n")
        row = self.last()
        self.assertNotIn("body", row)
        code, out, _ = _run(_load_bin("hpc-audit"), ["--full"])
        self.assertIn("hpc-read", out)
        self.assertIn("rundir:a.log --tail 5", out)

    def test_hpc_audit_code_prints_bodies_only(self):
        audit.record("payload", "s", "h", "ls", 0, payload="ls\n")
        audit.record("payload", "s", "h", "python - <<EOF", 0, body="python - <<EOF\nimport os\nEOF\n")
        code, out, _ = _run(_load_bin("hpc-audit"), ["--code"])
        self.assertEqual(code, 0)
        self.assertIn("    | import os", out)
        self.assertNotIn(" ls\n", out.split("\n", 1)[1])


class TestHpcRunAsksForBody(unittest.TestCase):
    def run_with(self, level, payload, *tokens):
        mod = _load_bin("hpc-run")
        seen = {}

        def fake_run_remote(*args, **kwargs):
            seen.update(kwargs)
            return transport.Result(returncode=0, stdout="", stderr="")

        with Sites(level), mock.patch.object(mod.transport, "run_remote", fake_run_remote):
            code, _, err = _run(mod, ["--site", "testsite", *tokens, "-"], payload)
        self.assertEqual(code, 0, err)
        return seen.get("record_body")

    def test_inline_code_is_kept(self):
        self.assertTrue(self.run_with("improvise", "python -c 'print(1)'\n", "--allow-exec"))

    def test_unknown_command_is_kept(self):
        self.assertTrue(self.run_with("improvise", "frobnicate /x\n", "--allow-exec"))

    def test_reads_writes_and_trusted_scripts_are_kept_too(self):
        self.assertTrue(self.run_with("prepare", "ls $HPC_SCRATCH\n"))
        self.assertTrue(self.run_with("prepare", "mkdir -p $HPC_SCRATCH/x\n", "--allow-write"))
        self.assertTrue(self.run_with("operate", "python /home/tester/my-tools/my-script.py check A\n"))

    def test_level_is_recorded(self):
        mod = _load_bin("hpc-run")
        seen = {}

        def fake_run_remote(*args, **kwargs):
            seen.update(kwargs)
            return transport.Result(returncode=0, stdout="", stderr="")

        with Sites("prepare"), mock.patch.object(mod.transport, "run_remote", fake_run_remote):
            _run(mod, ["--site", "testsite", "-"], "ls $HPC_SCRATCH\n")
        self.assertEqual(seen["audit_extra"], {"level": "observe"})

    def test_ssh_failure_is_explained(self):
        """A master that died mid-payload left a bare 255."""
        mod = _load_bin("hpc-run")
        dead = lambda *a, **k: transport.Result(returncode=255, stdout="", stderr="")
        with Sites("prepare"), mock.patch.object(mod.transport, "run_remote", dead):
            code, _, err = _run(mod, ["--site", "testsite", "-"], "ls $HPC_SCRATCH\n")
        self.assertEqual(code, 255)
        self.assertIn("may have run fully, partly or not at all", err)
        self.assertIn("hpc-check", err)

    def test_python_not_found_names_the_path_line(self):
        """`python my-script.py check` with no python on PATH exits 127."""
        mod = _load_bin("hpc-run")
        gone = lambda *a, **k: transport.Result(returncode=127, stdout="", stderr="")
        sites = Sites("improvise")
        sites.text = sites.text.replace("[paths]\n", '[paths]\npython_bin = "/opt/py/bin"\n')
        for payload, hinted in (("python -V\n", True),
                                ('export PATH="$HPC_PYTHON_BIN:$PATH"\npython -V\n', False),
                                ("frobnicate\n", False)):
            with self.subTest(payload=payload), sites, \
                    mock.patch.object(mod.transport, "run_remote", gone):
                code, _, err = _run(mod, ["--site", "testsite", "--allow-exec", "-"], payload)
            self.assertEqual(code, 127)
            self.assertEqual('export PATH="$HPC_PYTHON_BIN:$PATH"' in err, hinted, err)


class TestTransportKeepsBody(unittest.TestCase):
    """What reaches the log through run_remote, with ssh faked."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "a.jsonl"
        self._prev = os.environ.get("HPC_TETHER_AUDIT")
        os.environ["HPC_TETHER_AUDIT"] = str(self.log)
        self.site = transport.Site(name="t", host="h", scheduler="slurm", user="u")

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("HPC_TETHER_AUDIT", None)
        else:
            os.environ["HPC_TETHER_AUDIT"] = self._prev
        self.tmp.cleanup()

    def send(self, script, **kw):
        done = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(transport.subprocess, "run", return_value=done):
            transport.run_remote(self.site, script, preflight=False, capture=True, **kw)
        return json.loads(self.log.read_text().splitlines()[-1])

    def test_whole_text_after_a_setup_first_line(self):
        script = 'export PATH="$HPC_PYTHON_BIN:$PATH"\ncd $HPC_SCRATCH\nsqueue -u "$HPC_USER"\n'
        row = self.send(script, record_body=True, audit_extra={"level": "observe"})
        self.assertEqual(row["body"], script)                 # as written, no prelude
        self.assertEqual(row["detail"], "cd $HPC_SCRATCH")    # not the PATH line
        self.assertIn("payload_sha256", row)
        self.assertEqual(row["level"], "observe")

    def test_fixed_tool_script_is_not_kept(self):
        row = self.send("set -- /a/b\ntail -n 5 -- \"$1\"\n")
        self.assertNotIn("body", row)

    def test_deletion_refusal_keeps_body(self):
        with self.assertRaises(transport.TransportError):
            self.send("echo a\nrm /x/y\n", record_body=True)
        row = json.loads(self.log.read_text().splitlines()[-1])
        self.assertEqual((row["kind"], row["body"]), ("refused", "echo a\nrm /x/y\n"))


class TestRefusedLineIsLogged(unittest.TestCase):
    """The audit detail of a refusal is the refused line, not the payload's
    first line."""

    def test_detail_is_the_refused_line(self):
        code, _, rows = run_payload("improvise", "df -h /s\nfrobnicate -h\n")
        self.assertEqual(code, transport.EXIT_GUARD)
        self.assertEqual(rows[-1]["detail"], "frobnicate -h")

    def test_detail_skips_the_path_line_and_reads(self):
        payload = 'export PATH="$HPC_PYTHON_BIN:$PATH"\nls /s\nmkdir -p $HPC_SCRATCH/x\n'
        code, _, rows = run_payload("prepare", payload)
        self.assertEqual(code, transport.EXIT_GUARD)
        self.assertEqual(rows[-1]["detail"], "mkdir -p $HPC_SCRATCH/x")


if __name__ == "__main__":
    unittest.main()
