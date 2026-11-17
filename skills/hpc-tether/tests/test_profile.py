"""Profiles: what each level lets hpc-run, hpc-push and the site loader do.

Every hpc-run case is a --dry-run, so nothing connects. The profile check
comes before the token checks, so a payload the profile forbids is refused
even with every token passed.
"""

import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from hpctether import guard, profile, site as site_mod, transport

from test_trust import REPO, SITE_TOML, _load_bin, _run

ALL_TOKENS = ["--allow-write", "--allow-exec"]


class Sites:
    """A temp sites dir with one site at a given profile (None: key absent)."""

    def __init__(self, level):
        text = SITE_TOML.replace('profile = "improvise"\n', "")
        if level is not None:
            text = text.replace('user = "tester"\n', f'user = "tester"\nprofile = "{level}"\n')
        self.text = text

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        Path(self._tmp.name, "testsite.toml").write_text(self.text)
        self._prev = os.environ.get("HPC_TETHER_SITES")
        os.environ["HPC_TETHER_SITES"] = self._tmp.name
        return self

    def __exit__(self, *exc):
        if self._prev is None:
            os.environ.pop("HPC_TETHER_SITES", None)
        else:
            os.environ["HPC_TETHER_SITES"] = self._prev
        self._tmp.cleanup()


READ = "ls $HPC_SCRATCH\n"
WRITE = "mkdir -p $HPC_SCRATCH/x\n"
JOB = "sbatch $HPC_SCRATCH/job.sh\n"
CANCEL = "scancel 12345\n"
TRUSTED = f"python {REPO}/my-script.py check A\n"
INLINE = "python -c 'print(1)'\n"

# payload -> lowest level that sends it
CASES = {READ: "observe", WRITE: "prepare", JOB: "operate", CANCEL: "operate",
         TRUSTED: "operate", INLINE: "improvise"}


class TestSiteProfile(unittest.TestCase):
    def test_missing_profile_is_observe(self):
        with Sites(None):
            self.assertEqual(site_mod.load("testsite").profile, "observe")

    def test_unknown_profile_is_an_error(self):
        with Sites("everything"):
            with self.assertRaises(site_mod.SiteError) as info:
                site_mod.load("testsite")
            self.assertIn("observe, prepare, operate, improvise", str(info.exception))

    def test_levels_are_ordered(self):
        self.assertTrue(profile.allows("operate", "prepare"))
        self.assertFalse(profile.allows("prepare", "operate"))


class TestHpcRunLevels(unittest.TestCase):
    def setUp(self):
        self.mod = _load_bin("hpc-run")

    def test_each_payload_at_each_level(self):
        for level in profile.LEVELS:
            for payload, needed in CASES.items():
                with self.subTest(level=level, payload=payload.strip()):
                    with Sites(level):
                        code, _, err = _run(
                            self.mod, ["--site", "testsite", "--dry-run", *ALL_TOKENS, "-"],
                            payload)
                    if level == "observe":
                        # observe sends no free-form payload at all
                        self.assertEqual(code, transport.EXIT_GUARD)
                        self.assertIn("allows only the fixed tools", err)
                    elif profile.allows(level, needed):
                        self.assertEqual(code, 0, err)
                    else:
                        self.assertEqual(code, transport.EXIT_GUARD)
                        self.assertIn(f"needs profile {needed!r}", err)

    def test_trusted_script_below_operate_names_operate_not_improvise(self):
        with Sites("prepare"):
            _, _, err = _run(self.mod, ["--site", "testsite", "--dry-run", "-"], TRUSTED)
        self.assertIn("needs profile 'operate'", err)

    def test_refusal_says_never_edit_the_site_file(self):
        with Sites(None):
            _, _, err = _run(self.mod, ["--site", "testsite", "--dry-run", "-"], WRITE)
        self.assertIn("never edit it", err)
        self.assertIn("at 'observe'", err)

    def test_observe_refusal_names_the_fixed_tools_and_prepare(self):
        with Sites("observe"):
            code, _, err = _run(self.mod, ["--site", "testsite", "-"], READ)
        self.assertEqual(code, transport.EXIT_GUARD)
        for tool in ("hpc-check", "hpc-jobs", "hpc-read", "hpc-ls", "hpc-nc", "hpc-probe",
                     "hpc-pull", "hpc-fetch", "hpc-sync-status", "hpc-audit", "hpc-trust"):
            self.assertIn(tool, err)
        self.assertIn("starts at 'prepare'", err)

    def test_observe_refusal_is_audited_and_sends_nothing(self):
        with tempfile.TemporaryDirectory() as d, Sites("observe"):
            log = os.path.join(d, "a.jsonl")
            prev = os.environ.get("HPC_TETHER_AUDIT")
            os.environ["HPC_TETHER_AUDIT"] = log
            try:
                with mock.patch.object(self.mod.transport, "run_remote") as run:
                    code, _, _ = _run(self.mod, ["--site", "testsite", "-"], READ)
                rows = [json.loads(l) for l in open(log)]
            finally:
                if prev is None:
                    os.environ.pop("HPC_TETHER_AUDIT", None)
                else:
                    os.environ["HPC_TETHER_AUDIT"] = prev
        run.assert_not_called()
        self.assertEqual(code, transport.EXIT_GUARD)
        self.assertEqual(rows[-1]["kind"], "refused")
        self.assertEqual(rows[-1]["body"], READ)

    def test_trust_check_is_local_and_works_at_observe(self):
        with Sites("observe"):
            code, out, err = _run(_load_bin("hpc-trust"),
                                  ["--site", "testsite", "check", "-"], READ)
        self.assertEqual(code, 0, err)
        # A read still goes through hpc-run, which observe refuses (live A12).
        self.assertIn("needs profile: prepare", out)
        self.assertNotIn("needs profile: observe", out)

    def test_fixed_tools_do_not_go_through_the_hpc_run_gate(self):
        # They send toolkit-written scripts through transport; only hpc-run
        # classifies agent-written shell, and only hpc-push checks a level.
        bindir = os.path.join(os.path.dirname(__file__), "..", "bin")
        for name in ("hpc-check", "hpc-jobs", "hpc-read", "hpc-ls", "hpc-nc", "hpc-probe",
                     "hpc-pull", "hpc-fetch", "hpc-sync-status"):
            src = open(os.path.join(bindir, name)).read()
            with self.subTest(tool=name):
                self.assertNotIn("guard.classify", src)
                self.assertNotIn("_apply_guard", src)
                self.assertNotIn('allows("prepare")', src)
                self.assertNotIn('allows("operate")', src)

    def test_forbidden_refusal_is_logged_without_a_level(self):
        """The receipt said 'needs profile observe' for a write
        outside the roots, which no profile allows."""
        with tempfile.TemporaryDirectory() as d, Sites("prepare"):
            log = os.path.join(d, "a.jsonl")
            with mock.patch.dict(os.environ, {"HPC_TETHER_AUDIT": log}), \
                    mock.patch.object(self.mod.transport, "run_remote") as run:
                code, _, _ = _run(self.mod, ["--site", "testsite", "--allow-write", "-"],
                                  "touch /elsewhere/x\n")
            row = [json.loads(l) for l in open(log)][-1]
        run.assert_not_called()
        self.assertEqual(code, transport.EXIT_GUARD)
        self.assertNotIn("level", row)
        self.assertEqual(row["refusal"], "forbidden at every profile")

    def test_heredoc_file_write_points_at_hpc_push(self):
        """`cat > f <<'EOF'` needs improvise; the refusal says a push is the
        way to write the file."""
        for payload in ("cat > $HPC_SCRATCH/j.sh <<'EOF'\nsleep 1\nEOF\n",
                        "cat <<EOF > $HPC_SCRATCH/j.sh\nsleep 1\nEOF\n"):
            with self.subTest(payload=payload), Sites("prepare"):
                code, _, err = _run(self.mod, ["--site", "testsite", "--dry-run",
                                               "--allow-write", "-"], payload)
                self.assertEqual(code, transport.EXIT_GUARD)
                self.assertIn("hpc-push LOCAL --to", err)
                self.assertIn("Do not ask for a higher profile", err)

    def test_inline_python_heredoc_gets_no_push_hint(self):
        with Sites("prepare"):
            _, _, err = _run(self.mod, ["--site", "testsite", "--dry-run", "-"],
                             "python - <<EOF\nprint(1)\nEOF\n")
        self.assertNotIn("hpc-push LOCAL", err)

    def test_duplicate_job_name_logs_its_reason_and_describe_refuses(self):
        """The receipt said 'needs profile operate' at operate,
        and the approval hook asked for a payload refused locally."""
        twice = "sbatch -J two $HPC_SCRATCH/j.sh\nsbatch -J two $HPC_SCRATCH/j.sh\n"
        with tempfile.TemporaryDirectory() as d, Sites("operate"):
            log = os.path.join(d, "a.jsonl")
            with mock.patch.dict(os.environ, {"HPC_TETHER_AUDIT": log}), \
                    mock.patch.object(self.mod.transport, "run_remote") as run:
                code, _, _ = _run(self.mod, ["--site", "testsite", "--allow-write", "-"], twice)
            row = [json.loads(l) for l in open(log)][-1]
            _, out, _ = _run(_load_bin("hpc-trust"), ["--site", "testsite", "describe", "-"], twice)
        run.assert_not_called()
        self.assertEqual(code, transport.EXIT_DUPLICATE)
        self.assertNotIn("level", row)
        self.assertEqual(row["refusal"], "job name submitted more than once")
        self.assertTrue(out.startswith("REFUSED, never sent:"), out)

    def test_missing_token_is_logged_as_the_token(self):
        """Never 'needs profile prepare' at prepare."""
        for level, payload, token in (("prepare", WRITE, "--allow-write"),
                                      ("improvise", INLINE, "--allow-exec")):
            with self.subTest(level=level), tempfile.TemporaryDirectory() as d, Sites(level):
                log = os.path.join(d, "a.jsonl")
                with mock.patch.dict(os.environ, {"HPC_TETHER_AUDIT": log}), \
                        mock.patch.object(self.mod.transport, "run_remote") as run:
                    code, _, _ = _run(self.mod, ["--site", "testsite", "-"], payload)
                row = [json.loads(l) for l in open(log)][-1]
                run.assert_not_called()
                self.assertEqual(code, transport.EXIT_GUARD)
                self.assertEqual(row["refusal"], "needs " + token)

    def test_profile_refusal_precedes_token_refusal(self):
        # At observe a write is refused for the profile, not for a missing
        # --allow-write: asking the user to approve it would be pointless.
        with Sites("observe"):
            _, _, err = _run(self.mod, ["--site", "testsite", "--dry-run", "-"], WRITE)
        self.assertIn("REFUSED by profile", err)
        self.assertNotIn("--allow-write was not given", err)

    def test_forbidden_is_refused_at_every_level(self):
        for level in profile.LEVELS:
            with self.subTest(level=level), Sites(level):
                code, _, err = _run(
                    self.mod, ["--site", "testsite", "--dry-run", *ALL_TOKENS, "-"],
                    "rm -rf $HPC_SCRATCH/x\n")
                self.assertEqual(code, transport.EXIT_GUARD)
                if level != "observe":  # observe refuses every payload
                    self.assertNotIn("REFUSED by profile", err)


class TestNeededFor(unittest.TestCase):
    def test_scontrol_change_is_job_control(self):
        report = guard.classify("scontrol hold 123\n")
        self.assertEqual(profile.needed_for(report), "operate")

    def test_scontrol_show_is_a_read(self):
        self.assertEqual(profile.needed_for(guard.classify("scontrol show job 1\n")), "observe")


class TestHpcPushLevels(unittest.TestCase):
    def setUp(self):
        self.mod = _load_bin("hpc-push")

    def test_push_refused_at_observe(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt") as f, Sites("observe"):
            code, _, err = _run(self.mod, [f.name, "--site", "testsite", "--to",
                                           "/scratch/tester/x.txt", "--yes"])
        self.assertEqual(code, transport.EXIT_GUARD)
        self.assertIn("REFUSED by profile", err)
        self.assertIn("'prepare'", err)


if __name__ == "__main__":
    unittest.main()
