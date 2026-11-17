"""hpc-fetch: the local decisions it makes before and around the transfer.

Nothing here touches a network. The listing parser, the caps, the destination
refusals, and the rsync argv are all pure functions, and the rsync argv test is
the one that matters most: it pins that no spelling of --delete can appear.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import importlib.machinery  # noqa: E402

spec = importlib.util.spec_from_loader(
    "hpc_fetch", importlib.machinery.SourceFileLoader("hpc_fetch", str(ROOT / "bin" / "hpc-fetch"))
)
hpc_fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hpc_fetch)

LISTING = """###FILES###
1024 atm.log
2048 run/history.0001-01.nc
junk line without size
###TOP###
f atm.log
d run
"""


class TestListing(unittest.TestCase):
    def test_parse(self):
        files, top = hpc_fetch.parse_listing(LISTING)
        self.assertEqual(files, [(1024, "atm.log"), (2048, "run/history.0001-01.nc")])
        self.assertEqual(top, [("f", "atm.log"), ("d", "run")])

    def test_empty(self):
        self.assertEqual(hpc_fetch.parse_listing(""), ([], []))


class TestLimits(unittest.TestCase):
    def test_within(self):
        self.assertIsNone(hpc_fetch.check_limits([(10, "a")], 100, 5, False))

    def test_too_many_files(self):
        files = [(1, f"f{i}") for i in range(6)]
        self.assertIn("exceeds the cap of 5", hpc_fetch.check_limits(files, 100, 5, False))

    def test_too_big_needs_force(self):
        files = [(200, "big")]
        self.assertIn("--force-size", hpc_fetch.check_limits(files, 100, 5, False))
        self.assertIsNone(hpc_fetch.check_limits(files, 100, 5, True))

    def test_file_cap_is_not_forceable(self):
        files = [(1, f"f{i}") for i in range(6)]
        self.assertIsNotNone(hpc_fetch.check_limits(files, 100, 5, True))


class TestExcludes(unittest.TestCase):
    """The caps must measure what rsync will send, not the whole tree.

    Once a 46.5 GB run directory was one large
    out_a.dat plus 68 small per-band files. `--exclude out_a.dat` did not
    shrink the sized total, so the only way past the cap was 136 single-file
    hpc-pull calls.
    """

    TREE = [
        (46_000_000_000, "out_a.dat"),
        (500_000, "out_a_bin0000.dat"),
        (500_000, "out_a_bin0001.dat"),
        (4_000, "info_a2.dat"),
        (10, "sub/out_a.dat"),
        (20, "sub/keep.txt"),
        (30, "rest/deep/x.tmp"),
        (40, "rest/deep/y.nc"),
    ]

    def kept(self, *patterns):
        kept, dropped = hpc_fetch.apply_excludes(self.TREE, list(patterns))
        self.assertEqual(len(kept) + len(dropped), len(self.TREE))
        return sorted(rel for _, rel in kept)

    def test_no_excludes_keeps_everything(self):
        kept, dropped = hpc_fetch.apply_excludes(self.TREE, [])
        self.assertEqual(kept, self.TREE)
        self.assertEqual(dropped, [])

    def test_the_incident_fits_under_the_cap(self):
        kept, dropped = hpc_fetch.apply_excludes(self.TREE, ["out_a.dat"])
        self.assertIsNone(hpc_fetch.check_limits(kept, 2 * 1024**3, 2000, False))
        self.assertEqual(sorted(r for _, r in dropped), ["out_a.dat", "sub/out_a.dat"])

    def test_bare_name_matches_at_any_depth(self):
        self.assertNotIn("sub/out_a.dat", self.kept("out_a.dat"))

    def test_leading_slash_anchors_at_root(self):
        kept = self.kept("/out_a.dat")
        self.assertNotIn("out_a.dat", kept)
        self.assertIn("sub/out_a.dat", kept)

    def test_star_does_not_cross_slash(self):
        # `*` is one component; the bins go, the nested file stays.
        kept = self.kept("out_a_*.dat")
        self.assertNotIn("out_a_bin0000.dat", kept)
        self.assertIn("out_a.dat", kept)
        kept = self.kept("/*.dat")
        self.assertIn("sub/out_a.dat", kept)
        self.assertNotIn("info_a2.dat", kept)

    def test_double_star_crosses_slash(self):
        kept = self.kept("rest/**")
        self.assertNotIn("rest/deep/x.tmp", kept)
        self.assertNotIn("rest/deep/y.nc", kept)

    def test_directory_pattern_drops_its_contents(self):
        kept = self.kept("deep/")
        self.assertNotIn("rest/deep/x.tmp", kept)
        self.assertNotIn("rest/deep/y.nc", kept)
        self.assertIn("sub/keep.txt", kept)

    def test_trailing_slash_does_not_match_a_file(self):
        self.assertIn("info_a2.dat", self.kept("info_a2.dat/"))

    def test_slash_pattern_matches_trailing_components(self):
        kept = self.kept("deep/*.tmp")
        self.assertNotIn("rest/deep/x.tmp", kept)
        self.assertIn("rest/deep/y.nc", kept)

    def test_triple_star_is_dir_and_below(self):
        kept = self.kept("/rest/***")
        self.assertFalse(any(r.startswith("rest/") for r in kept))
        self.assertIn("sub/keep.txt", kept)

    def test_classes_and_question_mark(self):
        kept = self.kept("out_a_bin000[0].dat", "info_a?.dat")
        self.assertNotIn("out_a_bin0000.dat", kept)
        self.assertIn("out_a_bin0001.dat", kept)
        self.assertNotIn("info_a2.dat", kept)
        self.assertNotIn("out_a_bin0001.dat", self.kept("out_a_bin000[!0].dat"))


class TestRuleSpellings(unittest.TestCase):
    """rsync reads these as filter rules; the sizing cannot, so they fail closed."""

    def test_rule_prefixes_refused(self):
        for p in ["+ keep.nc", "- *.tmp", "-! *.nc", "+_x", "-/ /abs", "!"]:
            self.assertIsNotNone(hpc_fetch.unsupported_exclude(p), p)

    def test_plain_patterns_allowed(self):
        for p in ["out_a.dat", "*.tmp", "-dashed-name", "+plus", "/root/", "a b"]:
            self.assertIsNone(hpc_fetch.unsupported_exclude(p), p)

    def test_main_refuses_before_any_contact(self):
        rc = hpc_fetch.main(["/remote/dir", "--exclude", "+ keep.nc", "--dry-run"])
        self.assertEqual(rc, hpc_fetch.transport.EXIT_USAGE)


class TestDestination(unittest.TestCase):
    def setUp(self):
        # Not the system temp dir: on macOS that is /var/folders, which the
        # destination check refuses on purpose, so every case would fail on
        # the wrong reason. A scratch dir beside the tests is a real
        # working-directory shape.
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.skill = Path(self.tmp.name) / "skill"
        self.skill.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_skill_tree_refused(self):
        self.assertIn("skill directory",
                      hpc_fetch.refuse_destination(self.skill / "data", self.skill, False))

    def test_temp_refused(self):
        self.assertIn("temp or scratchpad",
                      hpc_fetch.refuse_destination(Path("/tmp/x"), self.skill, False))
        self.assertIn("temp or scratchpad",
                      hpc_fetch.refuse_destination(Path("/a/scratchpad/x"), self.skill, False))

    def test_nonempty_needs_merge(self):
        d = Path(self.tmp.name) / "out"
        d.mkdir()
        (d / "edited.txt").write_text("local edit\n")
        self.assertIn("--merge", hpc_fetch.refuse_destination(d, self.skill, False))
        self.assertIsNone(hpc_fetch.refuse_destination(d, self.skill, True))

    def test_empty_or_absent_fine(self):
        d = Path(self.tmp.name) / "empty"
        d.mkdir()
        self.assertIsNone(hpc_fetch.refuse_destination(d, self.skill, False))
        self.assertIsNone(hpc_fetch.refuse_destination(Path(self.tmp.name) / "new", self.skill, False))

    def test_file_in_the_way(self):
        f = Path(self.tmp.name) / "f"
        f.write_text("x")
        self.assertIn("not a directory", hpc_fetch.refuse_destination(f, self.skill, True))


class TestRsyncArgv(unittest.TestCase):
    def test_read_direction_and_no_delete(self):
        argv = hpc_fetch.rsync_argv("host", "/remote/dir", Path("/local/out"), ["*.tmp"])
        self.assertEqual(argv[0], "rsync")
        self.assertNotIn("--delete", " ".join(argv))
        self.assertFalse(any(a.startswith("--del") for a in argv))
        # Source is the remote, destination is local: bytes flow down only.
        self.assertEqual(argv[-2], "host:/remote/dir/")
        self.assertEqual(argv[-1], "/local/out/")
        self.assertIn("--exclude", argv)
        self.assertIn("*.tmp", argv)
        self.assertIn("BatchMode=yes", " ".join(argv))

    def test_remote_path_quoted(self):
        argv = hpc_fetch.rsync_argv("host", "/remote/dir with space", Path("/o"), [])
        self.assertEqual(argv[-2], "host:'/remote/dir with space'/")


class TestManifestDir(unittest.TestCase):
    def test_record_dir(self):
        from hpctether import manifest

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["HPC_TETHER_MANIFEST"] = str(Path(tmp) / "m.json")
            try:
                d = Path(tmp) / "tree"
                d.mkdir()
                entry = manifest.record_dir(d, "site", "/remote/tree", 12, 3456)
                self.assertEqual(entry["kind"], "dir")
                self.assertEqual(manifest.lookup(d)["file_count"], 12)
            finally:
                del os.environ["HPC_TETHER_MANIFEST"]


if __name__ == "__main__":
    unittest.main()
