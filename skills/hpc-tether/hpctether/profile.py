"""Profiles: how much the agent may do on the cluster, set by the user.

Four levels, lowest first. Each contains everything below it and adds one
kind of trust:

``observe``    fixed tools only: queue, logs, probes, reads, pull and fetch
``prepare``    change files inside the write roots; never start a process
``operate``    run the user's trusted scripts and control jobs
``improvise``  run code the agent writes itself

The profile lives in the site file (``profile = "..."``) and nowhere else.
It is never a command-line flag: a flag is something the agent can pass
itself. The toolkit steers; it is not a security boundary. A site
file with no ``profile`` gets ``observe``.

The profile decides what may be sent at all. The tokens (``--allow-write``,
``--allow-exec``, ``--yes``) still mark each operation that the profile
permits but the user must approve.

At ``observe`` only the fixed tools run (hpc-check, hpc-jobs, hpc-read,
hpc-ls, hpc-nc, hpc-probe, hpc-pull, hpc-fetch, hpc-sync-status). Their
remote scripts are written by the toolkit and the agent's input reaches
them only as quoted arguments, so no agent-written shell is sent and none
needs parsing. ``hpc-run`` is refused. ``needed_for`` still returns
``observe`` for a read-only payload: that is the guard's classification,
used by ``hpc-trust check``; ``hpc-run`` itself needs ``prepare``.
"""

from __future__ import annotations

LEVELS = ("observe", "prepare", "operate", "improvise")
DEFAULT = "observe"

SUMMARY = {
    "observe": "fixed tools only; their remote scripts are written by the toolkit",
    "prepare": "change files inside the write roots; never start a process",
    "operate": "run the user's trusted scripts and control jobs",
    "improvise": "run code the agent writes itself",
}

#: Commands that start, stop or alter scheduler jobs or processes. The guard
#: marks them write tier; the profile puts them at ``operate``, because
#: submitting a job runs a job script the guard never reads.
JOB_COMMANDS = frozenset({"sbatch", "scancel", "scontrol", "kill", "pkill"})


def rank(level: str) -> int:
    return LEVELS.index(level)


def allows(current: str, needed: str) -> bool:
    """True if a site at ``current`` may do what ``needed`` requires."""
    return rank(current) >= rank(needed)


def needed_for(report) -> str:
    """The lowest level at which a guard report's payload may be sent.

    Forbidden and traversal violations are not levels: no profile sends them,
    and the caller refuses those first.
    """
    from hpctether import guard  # local import: guard does not need profiles

    kinds = {v.kind for v in report.violations}
    if guard.INTERPRETER in kinds or guard.UNKNOWN in kinds:
        return "improvise"
    if report.uses:
        return "operate"
    writes = report.of_kind(guard.WRITE)
    if any(v.command in JOB_COMMANDS for v in writes):
        return "operate"
    if writes:
        return "prepare"
    return "observe"


def refusal(site, needed: str, what: str = "this operation") -> str:
    """The message for an operation the site's profile does not allow."""
    where = f" ({site.source})" if site.source else ""
    return (
        f"REFUSED by profile: {what} needs profile {needed!r} "
        f"({SUMMARY[needed]}); site {site.name!r} is at {site.profile!r}{where}.\n"
        "Nothing was transmitted. The profile is the user's setting in the "
        "site file. Never edit it to get past this refusal: tell the user what "
        "was refused and which profile would allow it, or do the part a lower "
        "level allows."
    )
