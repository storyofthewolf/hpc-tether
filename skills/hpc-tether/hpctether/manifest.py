"""Pull/push manifest: what hpc-pull fetched, so hpc-push can detect drift.

One JSON file, ``~/.cache/hpc-tether/manifest.json``, keyed by absolute local
path. Each entry records where the file came from and what the remote content
was at pull time (sha256 of the transferred bytes, remote size and mtime).
``hpc-push`` compares the remote's *current* state against this record: if the
remote changed since the pull, the push is refused rather than silently
overwriting someone else's edit.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path


def manifest_path() -> Path:
    """Location of the manifest file (override: HPC_TETHER_MANIFEST)."""
    override = os.environ.get("HPC_TETHER_MANIFEST")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "hpc-tether" / "manifest.json"


def load() -> dict[str, dict]:
    path = manifest_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save(entries: dict[str, dict]) -> None:
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record(
    local: Path,
    site: str,
    remote_path: str,
    remote_size: int,
    remote_mtime: int,
) -> dict:
    """Insert/update the entry for ``local`` and return it.

    The stored sha256 is computed from the *local* copy, which after a
    successful transfer is byte-identical to the remote at pull time — no
    remote checksum pass is needed.
    """
    entries = load()
    entry = {
        "site": site,
        "remote_path": remote_path,
        "remote_size": remote_size,
        "remote_mtime": remote_mtime,
        "sha256": sha256_file(local),
        "pulled_at": int(time.time()),
    }
    entries[str(local.resolve())] = entry
    save(entries)
    return entry


def record_dir(
    local: Path, site: str, remote_path: str, file_count: int, total_bytes: int
) -> dict:
    """Record a directory fetched by hpc-fetch.

    A tree entry carries no checksum -- hashing thousands of files on every
    fetch is not worth it -- so it is provenance, not a drift record: where a
    local tree came from, how big it was, and when. Keyed by the directory's
    resolved path like a file entry, distinguished by ``"kind": "dir"``.
    """
    entries = load()
    entry = {
        "kind": "dir",
        "site": site,
        "remote_path": remote_path,
        "file_count": file_count,
        "total_bytes": total_bytes,
        "fetched_at": int(time.time()),
    }
    entries[str(local.resolve())] = entry
    save(entries)
    return entry


def lookup(local: Path) -> dict | None:
    return load().get(str(local.resolve()))
