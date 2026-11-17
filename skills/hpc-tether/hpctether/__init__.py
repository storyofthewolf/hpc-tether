"""Shared library for the hpc-tether skill tools.

Standard library only. Python 3.11+ (requires ``tomllib``).

Modules:
    site       -- TOML site definitions, path-role resolution
    transport  -- ControlMaster checks, remote execution, exit-code mapping
    guard      -- payload classification: forbidden / traversal / write tiers
    manifest   -- pull/push manifest for drift detection

Interpreter check. The ``bin/`` tools start with ``#!/usr/bin/env python3``,
which takes the FIRST ``python3`` on PATH. On macOS that is often Apple's
``/usr/bin/python3`` (3.9), and the tools died on ``import tomllib`` with a
traceback that named neither the cause nor the fix. Importing this
package on an older interpreter now re-runs the tool under the newest
``python3.N`` / ``python3`` on PATH that is 3.11+, or exits with a message that
says what to do. Nothing is hard-coded: the candidates come from PATH.
"""

import os
import sys

__all__ = ["site", "transport", "guard", "manifest"]

MIN_PYTHON = (3, 11)
_REEXEC_ENV = "HPCTETHER_REEXEC"


def _candidates():
    """Interpreters on PATH that might be new enough, best first."""
    import shutil
    seen = set()
    for minor in range(20, MIN_PYTHON[1] - 1, -1):
        p = shutil.which(f"python3.{minor}")
        if p:
            yield p
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, "python3")
        if os.path.isfile(p) and os.access(p, os.X_OK):
            real = os.path.realpath(p)
            if real not in seen:
                seen.add(real)
                yield p


def _version_of(python):
    import subprocess
    try:
        out = subprocess.run(
            [python, "-c", "import sys; print(*sys.version_info[:2])"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            universal_newlines=True, timeout=10)
        major, minor = out.stdout.split()
        return int(major), int(minor)
    except Exception:
        return None


def _ensure_python():
    if sys.version_info[:2] >= MIN_PYTHON:
        return
    here = ".".join(map(str, sys.version_info[:3]))
    need = ".".join(map(str, MIN_PYTHON))
    script = sys.argv[0] if sys.argv and sys.argv[0] else ""
    if os.environ.get(_REEXEC_ENV) != "1" and script and os.path.isfile(script):
        for py in _candidates():
            v = _version_of(py)
            if v and v >= MIN_PYTHON:
                os.environ[_REEXEC_ENV] = "1"
                os.execv(py, [py, script] + sys.argv[1:])
    sys.stderr.write(
        f"hpc-tether needs Python {need}+ (for tomllib); this is {here} at "
        f"{sys.executable}, and no python3 {need}+ was found on PATH.\n"
        f"Fix: put a newer python3 first on PATH (e.g. a conda or Homebrew "
        f"bin/), or run the tool as `python3.{MIN_PYTHON[1]} {script or 'bin/<tool>'} ...`.\n")
    raise SystemExit(70)


_ensure_python()
