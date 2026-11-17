#!/usr/bin/env python3
"""Check hpc-fetch's exclude sizing against a real rsync, locally.

Not collected by pytest: it shells out to rsync, which the agent's Bash hook
blocks, so the user runs it by hand:

    python3 tests/exclude_parity.py [path/to/rsync]

For each pattern set it copies a small scratch tree with `rsync -a --exclude
...` (local to local, no cluster) and compares the files that arrived with
what `apply_excludes` counts as kept. A mismatch means the size cap and the
transfer disagree about what a fetch moves. In a real fetch the cluster's
rsync applies the excludes, so a local rsync of a different lineage (macOS
openrsync vs. GNU rsync 3.x) is a proxy; the version line says which ran.

Exit: 0 all agree, 1 a mismatch, 2 rsync not runnable.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_spec = importlib.util.spec_from_loader(
    "hpc_fetch", importlib.machinery.SourceFileLoader("hpc_fetch", str(ROOT / "bin" / "hpc-fetch"))
)
hpc_fetch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hpc_fetch)

TREE = [
    "out_a.dat",
    "out_a_bin0000.dat",
    "out_a_bin0001.dat",
    "out_a_bin0012.dat",
    "info_a2.dat",
    "keep.txt",
    "sub/out_a.dat",
    "sub/keep.txt",
    "sub/deep/keep.txt",
    "rest/deep/x.tmp",
    "rest/deep/y.nc",
    "rest/z.nc",
    "deep/top.nc",
    "a b/space name.txt",
]

CASES = [
    ["out_a.dat"],
    ["*.tmp"],
    ["*.dat"],
    ["/out_a.dat"],
    ["/sub"],
    ["sub/"],
    ["deep"],
    ["deep/"],
    ["/rest/deep/"],
    ["rest/***"],
    ["sub/*.dat"],
    ["sub/keep.txt"],
    ["deep/keep.txt"],
    ["**/deep/*.nc"],
    ["**.nc"],
    ["rest/**"],
    ["out_a_bin000?.dat"],
    ["out_a_bin00[01]*"],
    ["[!O]*.dat"],
    ["*/keep.txt"],
    ["a b/"],
    ["out_a.dat", "*.tmp", "sub/"],
]


def arrived(root: Path) -> list[str]:
    out = []
    for dirpath, _, names in os.walk(root):
        for n in names:
            out.append(os.path.relpath(os.path.join(dirpath, n), root))
    return sorted(out)


def main() -> int:
    exe = sys.argv[1] if len(sys.argv) > 1 else "rsync"
    try:
        ver = subprocess.run([exe, "--version"], capture_output=True, text=True, check=False)
    except OSError as exc:
        print(f"cannot run {exe}: {exc}")
        return 2
    print((ver.stdout or ver.stderr).splitlines()[0] if (ver.stdout or ver.stderr) else exe)

    listing = [(1, rel) for rel in TREE]
    bad = 0
    with tempfile.TemporaryDirectory(prefix="exclude-parity-") as tmp:
        src = Path(tmp) / "src"
        for rel in TREE:
            p = src / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("x")
        for i, patterns in enumerate(CASES):
            dst = Path(tmp) / f"dst{i}"
            argv = [exe, "-a"]
            for pat in patterns:
                argv += ["--exclude", pat]
            argv += [f"{src}/", f"{dst}/"]
            r = subprocess.run(argv, capture_output=True, text=True, check=False)
            if r.returncode != 0:
                print(f"ERR   {patterns}: rsync exit {r.returncode}: {r.stderr.strip()}")
                bad += 1
                continue
            real = arrived(dst) if dst.exists() else []
            kept, _ = hpc_fetch.apply_excludes(listing, patterns)
            model = sorted(rel for _, rel in kept)
            if real == model:
                print(f"ok    {patterns}")
            else:
                bad += 1
                print(f"DIFF  {patterns}")
                for rel in sorted(set(real) - set(model)):
                    print(f"        rsync sent, sizing dropped: {rel}")
                for rel in sorted(set(model) - set(real)):
                    print(f"        sizing kept, rsync skipped: {rel}")
    print(f"\n{len(CASES) - bad}/{len(CASES)} agree")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
