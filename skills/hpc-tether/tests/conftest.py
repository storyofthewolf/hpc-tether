"""Keep the suite out of the user's real state.

Every test runs with the audit log and the manifest pointed at a temporary
directory. The first run of the audit-logging transport wrote three test
refusals into the real ~/.local/state/hpc-tether/audit.jsonl; a record that
says "agent refused rm -f /x/tmp" with a real timestamp is exactly the kind of
misleading line the log exists to prevent.
"""

import os
import sys
import tempfile

import pytest

sys.dont_write_bytecode = True  # no __pycache__ in the installed skill


@pytest.fixture(autouse=True, scope="session")
def _isolated_state():
    with tempfile.TemporaryDirectory() as tmp:
        previous = {k: os.environ.get(k) for k in ("HPC_TETHER_AUDIT", "HPC_TETHER_MANIFEST")}
        os.environ["HPC_TETHER_AUDIT"] = os.path.join(tmp, "audit.jsonl")
        os.environ["HPC_TETHER_MANIFEST"] = os.path.join(tmp, "manifest.json")
        try:
            yield
        finally:
            for k, v in previous.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
