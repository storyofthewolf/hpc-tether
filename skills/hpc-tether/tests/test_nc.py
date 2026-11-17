"""hpc-nc end to end: the real cluster program, run locally through bash.

transport.run_capture is replaced by a local bash run of the same payload,
with HPC_PYTHON_BIN pointing at this interpreter, so everything but ssh is
exercised. Needs numpy and netCDF4 here; skipped without them.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

np = None
try:
    import numpy as np
    import netCDF4
except ImportError:  # pragma: no cover
    netCDF4 = None

from hpctether import guard, nc_remote, transport

from test_trust import _load_bin, _run

SITE = """\
name = "testsite"
host = "test.example.edu"
scheduler = "slurm"
user = "tester"
nc_max_read_mb = {cap}

[paths]
scratch = "/scratch/tester"
data = "{data}"
"""


def local_capture(chunk=None):
    """A run_capture that runs the payload here, optionally with small chunks."""
    def fake(site, script, timeout=None, trust_uses=None):
        if chunk:
            script = script.replace('"op":', f'"chunk": {chunk}, "op":', 1) \
                if '\\"op\\"' not in script else script.replace(
                    '\\"op\\":', f'\\"chunk\\": {chunk}, \\"op\\":', 1)
        env = dict(os.environ, HPC_PYTHON_BIN=str(Path(sys.executable).parent))
        p = subprocess.run(["bash", "-s"], input=script, capture_output=True, text=True, env=env)
        return transport.Result(returncode=p.returncode, stdout=p.stdout, stderr=p.stderr)
    return fake


@unittest.skipIf(netCDF4 is None, "numpy/netCDF4 not installed")
class NcCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        f = cls.dir / "a.nc"
        with netCDF4.Dataset(f, "w") as ds:
            ds.title = "test file"
            ds.createDimension("time", None)
            ds.createDimension("lat", 4)
            ds.createDimension("lon", 3)
            t = ds.createVariable("time", "f8", ("time",))
            t.units = "days since 2000-01-01"
            t.calendar = "noleap"
            lat = ds.createVariable("lat", "f8", ("lat",))
            lat.units = "degrees_north"
            gw = ds.createVariable("gw", "f8", ("lat",))
            wt = ds.createVariable("wt", "f8", ("time", "lat"))
            ts = ds.createVariable("TS", "f4", ("time", "lat", "lon"), fill_value=-999.0)
            ts.units = "K"
            ts.long_name = "surface temperature"
            name = ds.createVariable("label", str, ("lat",))
            t[:] = [0, 31, 59, 90, 120]
            lat[:] = [-60, -20, 20, 60]
            gw[:] = [1.0, 2.0, 2.0, 1.0]
            wt[:] = np.arange(1, 21, dtype="f8").reshape(5, 4)
            data = np.arange(5 * 4 * 3, dtype="f4").reshape(5, 4, 3)
            ts[:] = data
            ts[0, 0, 0] = -999.0          # fill
            ts[1, 0, 0] = np.nan          # NaN
            for i, s in enumerate(["a", "b", "c", "d"]):
                name[i] = s
        cls.a = f
        cls.data = np.ma.masked_equal(np.arange(60, dtype="f8").reshape(5, 4, 3), 0.0)
        cls.data[1, 0, 0] = np.nan
        # b.nc: same, one value changed, one attribute changed, one extra var
        g = cls.dir / "b.nc"
        subprocess.run(["cp", str(f), str(g)], check=True)
        with netCDF4.Dataset(g, "a") as ds:
            ds.variables["TS"][4, 3, 2] = 1000.0
            ds.variables["TS"].units = "degC"
            ds.createVariable("extra", "i4", ("lat",))
        cls.b = g

    def nc(self, *argv, cap=100, chunk=None):
        mod = _load_bin("hpc-nc")
        tmp = tempfile.TemporaryDirectory()
        Path(tmp.name, "testsite.toml").write_text(SITE.format(cap=cap, data=self.dir))
        prev = os.environ.get("HPC_TETHER_SITES")
        os.environ["HPC_TETHER_SITES"] = tmp.name
        try:
            with mock.patch.object(mod.transport, "run_capture", local_capture(chunk)):
                return _run(mod, ["--site", "testsite", *argv])
        finally:
            if prev is None:
                os.environ.pop("HPC_TETHER_SITES", None)
            else:
                os.environ["HPC_TETHER_SITES"] = prev
            tmp.cleanup()

    def js(self, *argv, **kw):
        code, out, err = self.nc("--json", *argv, **kw)
        self.assertEqual(code, 0, err)
        return json.loads(out)


class TestHeader(NcCase):
    def test_header(self):
        r = self.js("header", str(self.a))
        self.assertEqual({d["name"]: d["size"] for d in r["dims"]}, {"time": 5, "lat": 4, "lon": 3})
        ts = next(v for v in r["vars"] if v["name"] == "TS")
        self.assertEqual(ts["dims"], ["time", "lat", "lon"])
        self.assertEqual(ts["attrs"]["units"], "K")
        self.assertEqual(r["global_attrs"]["title"], "test file")

    def test_role_path_and_text(self):
        code, out, _ = self.nc("header", "data:a.nc", "--var", "TS")
        self.assertEqual(code, 0)
        self.assertIn("TS(time, lat, lon)", out)
        self.assertIn("units=K", out)


class TestStats(NcCase):
    def expected(self, sel=(slice(None),) * 3):
        x = self.data[sel]
        good = x.compressed()
        good = good[np.isfinite(good)]
        return good

    def test_unweighted_with_nan_and_fill(self):
        r = self.js("stats", str(self.a), "TS")
        good = self.expected()
        self.assertEqual(r["count"], good.size)
        self.assertAlmostEqual(r["mean"], float(good.mean()), places=5)
        self.assertEqual((r["min"], r["max"]), (float(good.min()), float(good.max())))
        self.assertEqual((r["nan"], r["fill"], r["inf"]), (1, 1, 0))
        self.assertEqual(r["mean_kind"], "unweighted")

    def test_selection_last_time(self):
        r = self.js("stats", str(self.a), "TS", "-d", "time,-1")
        self.assertEqual(r["selection"][0], [4, 4])
        self.assertAlmostEqual(r["mean"], float(np.arange(48, 60).mean()), places=5)

    def test_weighted_mean_matches_numpy_across_chunks(self):
        w = np.array([1.0, 2.0, 2.0, 1.0])[None, :, None] * np.ones((5, 4, 3))
        x = np.ma.filled(self.data.astype("f8"), np.nan)
        ok = np.isfinite(x)
        want = float((w[ok] * x[ok]).sum() / w[ok].sum())
        for chunk in (None, 48):   # 48 bytes = 4 values: splits every time row
            with self.subTest(chunk=chunk):
                r = self.js("stats", str(self.a), "TS", "--weights", "gw", chunk=chunk)
                self.assertAlmostEqual(r["weighted_mean"], want, places=4)
                self.assertEqual(r["count"], int(ok.sum()))

    def test_weights_along_the_chunked_dimension(self):
        # wt varies along time, the dimension reads are chunked on, so each
        # chunk must take its own rows of the weights, not broadcast them.
        w = np.broadcast_to(np.arange(1, 21, dtype="f8").reshape(5, 4)[:, :, None], (5, 4, 3))
        x = np.ma.filled(self.data.astype("f8"), np.nan)
        ok = np.isfinite(x)
        want = float((w[ok] * x[ok]).sum() / w[ok].sum())
        for chunk in (None, 48, 96):
            with self.subTest(chunk=chunk):
                r = self.js("stats", str(self.a), "TS", "--weights", "wt", chunk=chunk)
                self.assertAlmostEqual(r["weighted_mean"], want, places=4)
        r = self.js("stats", str(self.a), "TS", "--weights", "wt", "-d", "time,2,4", chunk=48)
        sel = (slice(2, 5),)
        ok2 = np.isfinite(x[sel])
        self.assertAlmostEqual(r["weighted_mean"],
                               float((w[sel][ok2] * x[sel][ok2]).sum() / w[sel][ok2].sum()), places=4)

    def test_weights_with_a_foreign_dimension_refused(self):
        code, _, err = self.nc("stats", str(self.a), "lat", "--weights", "wt")
        self.assertEqual(code, 66)
        self.assertIn("lacks", err)

    def test_text_says_unweighted(self):
        code, out, _ = self.nc("stats", str(self.a), "TS")
        self.assertEqual(code, 0)
        self.assertIn("(unweighted)", out)


class TestSlice(NcCase):
    def test_values_coords_and_decoded_time(self):
        r = self.js("slice", str(self.a), "TS", "-d", "time,0,1", "-d", "lat,0", "-d", "lon,0")
        self.assertEqual(r["shape"], [2, 1, 1])
        self.assertEqual(r["values"][0], None)       # fill
        self.assertEqual(r["values"][1], "nan")
        time = next(c for c in r["coords"] if c["name"] == "time")
        self.assertEqual(time["values"], [0.0, 31.0])
        try:
            import cftime  # noqa: F401
            self.assertTrue(time["decoded"][1].startswith("2000-02-01"))
        except ImportError:
            pass

    def test_string_variable(self):
        r = self.js("slice", str(self.a), "label")
        self.assertEqual(r["values"], ["a", "b", "c", "d"])

    def test_too_many_values(self):
        code, _, err = self.nc("slice", str(self.a), "TS", "--limit", "5")
        self.assertEqual(code, 65)
        self.assertIn("limit is 5", err)


class TestDiff(NcCase):
    def test_identical(self):
        r = self.js("diff", str(self.a), str(self.a))
        self.assertEqual(r["data"], {})

    def test_changes_found(self):
        r = self.js("diff", str(self.a), str(self.b))
        self.assertEqual(r["vars_only_b"], ["extra"])
        self.assertIn("TS", r["vars_changed_meta"])
        self.assertEqual(r["data"]["TS"]["differing"], 1)
        self.assertEqual(r["data"]["TS"]["at"], [4, 3, 2])
        self.assertAlmostEqual(r["data"]["TS"]["max_abs_diff"], 1000.0 - 59.0, places=3)


class TestRefusalsAndErrors(NcCase):
    def test_over_the_cap_is_refused_before_reading(self):
        big = self.dir / "big.nc"
        with netCDF4.Dataset(big, "w") as ds:
            ds.createDimension("n", 300_000)
            ds.createVariable("v", "f8", ("n",))[:] = 1.0   # 2.4 MB
        code, _, err = self.nc("stats", str(big), "v", cap=1)
        self.assertEqual(code, 65)
        self.assertIn("trusted script", err)
        self.assertEqual(self.js("stats", str(big), "v", "-d", "n,0,999", cap=1)["count"], 1000)

    def test_weights_count_toward_the_cap(self):
        """V alone fits; v plus w does not."""
        f = self.dir / "weighted.nc"
        with netCDF4.Dataset(f, "w") as ds:
            ds.createDimension("n", 100_000)
            ds.createVariable("v", "f8", ("n",))[:] = 1.0   # 0.8 MB
            ds.createVariable("w", "f8", ("n",))[:] = 1.0   # 0.8 MB
        self.assertEqual(self.js("stats", str(f), "v", cap=1)["count"], 100_000)
        code, _, err = self.nc("stats", str(f), "v", "--weights", "w", cap=1)
        self.assertEqual(code, 65)

    def test_missing_things(self):
        for argv, text in ((("stats", str(self.a), "NOPE"), "no variable"),
                           (("stats", str(self.a), "TS", "-d", "depth,0"), "no dimension"),
                           (("stats", str(self.a), "TS", "-d", "time,9"), "outside"),
                           (("header", str(self.dir / "none.nc")), "no such file")):
            with self.subTest(argv=argv):
                code, _, err = self.nc(*argv)
                self.assertEqual(code, 66)
                self.assertIn(text, err)

    def test_bad_dim_syntax_is_usage(self):
        code, _, _ = self.nc("stats", str(self.a), "TS", "-d", "time:0")
        self.assertEqual(code, 64)


class TestPayload(unittest.TestCase):
    def test_payload_passes_the_transport_deletion_check(self):
        p = nc_remote.payload({"op": "stats", "path": "/x.nc", "var": "T", "dims": [], "cap": 1})
        self.assertIsNone(guard.contains_deletion(p))

    def test_payload_names_the_request_first(self):
        p = nc_remote.payload({"op": "header", "path": "/x.nc", "cap": 1})
        self.assertTrue(p.startswith(": hpc-nc "))


if __name__ == "__main__":
    unittest.main()
