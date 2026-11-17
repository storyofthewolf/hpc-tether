"""Audit log: one JSON line for everything the toolkit sends to a cluster.

On the cluster every command arrives as the account holder, and the account
holder answers for all of it. Nothing on either end distinguishes a command an
agent issued from one typed by hand -- except this file. ``transport.run_remote``
records every payload (sent, refused, or timed out), and the scp/rsync paths in
hpc-pull, hpc-push and hpc-fetch record every transfer, so the question "what
did the agent do on the cluster, and when, and under which confirmation" has an
answer that does not depend on anyone's memory.

Location: ``~/.local/state/hpc-tether/audit-YYYY-MM.jsonl`` -- one file per
UTC month, chosen from the entry's own timestamp, so a long-lived log stays
readable without anything ever being rotated away. ``HPC_TETHER_AUDIT``
overrides it and means either the directory to write the monthly files into,
or, when it ends in ``.jsonl``, one exact file to append everything to.
Nothing here ever deletes or truncates a log file: old months accumulate and
are the user's to prune by hand. Append-only from this module's point of
view; mode 0600. Each line:

    ts            ISO-8601 UTC
    session       Claude Code session id when run under one, else ""
    agent         true when run under Claude Code (CLAUDECODE is set)
    verb          the bin/ tool that ran (basename of argv[0])
    argv          its arguments, joined
    tokens        which confirmation flags were present on that command line
                  (--allow-write, --allow-exec, --yes, --force, --force-size,
                  --merge, --overwrite, --budget-add)
    site, host    from the site file
    kind          "payload" | "scp" | "rsync" | "refused"
    detail        first line of the payload, or the transfer endpoints
    payload_sha256  for a payload: sha256 of the script text sent
    trusted       present only when the payload ran a pre-built script (a .py
                  under the site's script_roots): path, the root, the
                  argument string, and its tier (free, or write via
                  script_confirm)
    rc            exit code (77 for a refusal that sent nothing)
    seconds       wall time of the remote call
    direction, files, bytes, remote, local
                  on a completed transfer only: "in" (cluster -> local) or
                  "out", and what actually landed -- measured on the local
                  side after the copy, not the size planned beforehand.
                  hpc-audit --transfers and the session budget
                  (hpctether/budget.py) add these up.
    budget_add    on a "budget" line: bytes the user added to this session's
                  transfer budget with --budget-add
    body          the script text of an hpc-run payload as the agent wrote
                  it, before the toolkit's prelude; sent, refused or timed out.
                  Cut at BODY_LIMIT characters, and then body_truncated is true
                  and body_length is the full length. Entries from older
                  versions carry a body only for profile improvise.
    level         on an hpc-run line: the profile level the payload needed
                  (observe, prepare, operate, improvise). ``hpc-audit --code``
                  selects improvise.

The fixed tools (hpc-read, hpc-jobs, hpc-ls, ...) send scripts the toolkit
wrote, so their lines carry no body: ``verb`` and ``argv`` say what was asked,
and the hash identifies the script. ``hpc-audit --full`` prints bodies.

The log holds whole payloads, which can carry data, so it stays on this
machine: the file is created, and kept, at mode 0600.

A logging failure never fails the command: the log is evidence, not a gate,
and a full disk must not stop a read of a job log. It is reported on stderr.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

#: Every confirmation token; hooks/approve_hpc_writes.py asks the user for each.
_TOKENS = ("--allow-write", "--allow-exec", "--yes", "--force", "--force-size", "--merge",
           "--overwrite", "--budget-add")


def audit_dir() -> Path:
    """Directory holding the monthly log files.

    ``HPC_TETHER_AUDIT`` overrides it. When that override names an exact file
    (``*.jsonl``) the directory is that file's parent.
    """
    override = os.environ.get("HPC_TETHER_AUDIT")
    if override:
        path = Path(override).expanduser()
        return path.parent if path.suffix == ".jsonl" else path
    return Path.home() / ".local" / "state" / "hpc-tether"


def month_of(ts: str) -> str:
    """``YYYY-MM`` from an entry timestamp; today's UTC month if unparseable."""
    if len(ts) >= 7 and ts[4] == "-":
        return ts[:7]
    return datetime.now(timezone.utc).strftime("%Y-%m")


def audit_path(month: str | None = None) -> Path:
    """The log file for ``month`` (``YYYY-MM``), or for the current UTC month.

    An ``HPC_TETHER_AUDIT`` override ending in ``.jsonl`` is an exact file and
    is returned whatever the month: the caller asked for one specific log.
    """
    override = os.environ.get("HPC_TETHER_AUDIT")
    if override:
        path = Path(override).expanduser()
        if path.suffix == ".jsonl":
            return path
    if month is None:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
    return audit_dir() / f"audit-{month}.jsonl"


def existing_months() -> list[str]:
    """Every month that has a log file, oldest first.

    Empty when the override names one exact file -- there are no months then.
    """
    override = os.environ.get("HPC_TETHER_AUDIT")
    if override and Path(override).expanduser().suffix == ".jsonl":
        return []
    directory = audit_dir()
    if not directory.is_dir():
        return []
    return sorted(p.name[len("audit-"):-len(".jsonl")]
                  for p in directory.glob("audit-*.jsonl"))


def _context() -> dict:
    argv = sys.argv
    return {
        "session": os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
        "agent": bool(os.environ.get("CLAUDECODE")),
        "verb": Path(argv[0]).name if argv else "",
        "argv": " ".join(argv[1:]),
        "tokens": [t for t in _TOKENS
                   if any(a == t or a.startswith(t + "=") for a in argv[1:])],
        "cwd": os.getcwd(),
    }


#: Longest payload body kept in one audit line, in characters. Past it the
#: body is cut and the line says so (body_truncated, body_length).
BODY_LIMIT = 64 * 1024


def record(
    kind: str,
    site: str,
    host: str,
    detail: str,
    rc: int | None,
    seconds: float | None = None,
    payload: str | None = None,
    trusted: list | None = None,
    extra: dict | None = None,
    body: str | None = None,
) -> None:
    """Append one line. Never raises.

    ``trusted`` names the pre-built scripts a payload ran -- path, the script
    root that covered it, the argument string, and whether it ran at ``free``
    or ``write`` tier. Without it the log would show a read-tier payload with
    no confirmation token and no way to tell which root made that legitimate.

    ``extra`` carries the transfer fields (see ``transfer_fields``) or a
    budget grant; its keys are merged into the line.
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **_context(),
        "site": site,
        "host": host,
        "kind": kind,
        "detail": detail[:200],
        "rc": rc,
    }
    if seconds is not None:
        entry["seconds"] = round(seconds, 2)
    if payload is not None:
        entry["payload_sha256"] = hashlib.sha256(payload.encode()).hexdigest()
    if trusted:
        entry["trusted"] = trusted
    if body is not None:
        entry["body"] = body[:BODY_LIMIT]
        if len(body) > BODY_LIMIT:
            entry["body_truncated"] = True
            entry["body_length"] = len(body)
    if extra:
        entry.update(extra)
    try:
        path = audit_path(month_of(entry["ts"]))
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        # A file made before bodies were kept, or by hand, may be wider.
        if os.fstat(fd).st_mode & 0o077:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"hpc-tether: audit log not written ({exc})", file=sys.stderr)


def transfer_fields(direction: str, files: int, nbytes: int, remote: str, local) -> dict:
    """The ``extra`` for a completed transfer: what landed, in which direction."""
    return {
        "direction": direction,
        "files": files,
        "bytes": nbytes,
        "remote": remote,
        "local": str(Path(local).resolve()),
    }


def trusted_records(uses) -> list | None:
    """``trusted`` field from ``hpctether.trust.Use`` objects, or None.

    Kept here rather than in the transport so the log's shape is defined in one
    place. None when there are no uses, so an ordinary payload's line is
    byte-identical to what it was before this field existed.
    """
    if not uses:
        return None
    return [
        {
            "path": use.path,
            "root": use.root,
            "args": use.args,
            "tier": use.tier,
        }
        for use in uses
    ]


def is_path_prelude(line: str) -> bool:
    """True for the `export PATH="$HPC_PYTHON_BIN:$PATH"` line SKILL.md has
    python payloads open with. It says nothing about what the payload does."""
    s = line.strip()
    return s.startswith("export PATH=") and "HPC_PYTHON_BIN" in s


def first_line(script: str) -> str:
    """The first substantive line of a payload, for `detail`: not blank, a
    comment, a `set --` argument line or the PATH prelude."""
    for line in script.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and not s.startswith("set -- ") \
                and not is_path_prelude(s):
            return s
    return ""


class Timer:
    """`with Timer() as t: ...; t.seconds`."""

    def __enter__(self) -> "Timer":
        self._start = time.monotonic()
        self.seconds = 0.0
        return self

    def __exit__(self, *exc) -> None:
        self.seconds = time.monotonic() - self._start
