"""SSH transport: ControlMaster checks, remote execution, exit-code mapping.

Every remote invocation in this toolkit goes through this module. Two
structural properties are what make it safe, and neither is a convention that
has to be remembered:

1. ``subprocess.run`` is always called with an argv *list* and never with
   ``shell=True``. argv goes straight to ``execve``, so no local shell -- not
   bash, not the user's tcsh -- ever parses any of it. The entire class of
   quoting bugs where a local shell mangles ``$``, ``!``, or nested quotes
   before ``ssh`` even starts simply cannot occur.

2. Remote payloads are piped to ``bash -s`` on stdin. They are never passed as
   a quoted remote command string, so the *remote* login shell never parses
   them either -- which also makes a tcsh login shell a non-issue.

A third structural property: **no payload that deletes is ever sent.**
``run_remote`` calls ``guard.contains_deletion`` on every script, whichever
verb assembled it, and raises with exit 77 if it finds ``rm``, ``rmdir``,
``unlink``, ``shred``, ``find -delete`` or ``git clean`` in any spelling the
guard recognises. The full guard policy runs only in ``hpc-run``; this one rule
runs here so that "the toolkit never deletes anything on the cluster" is a
property of the transport rather than a convention each tool remembers.

Every payload -- sent, refused, or timed out -- is also written to the audit
log (``hpctether/audit.py``) with the session id, the confirmation tokens on
the command line, the payload's first line and hash, and the exit code; an
``hpc-run`` payload's whole text is kept too. On
the cluster everything arrives as the account holder; the log is the only
record of which of it an agent issued.

A fourth: every payload runs under a **remote-side watchdog** (``timeout`` on the cluster) whose budget is shorter than the local
timeout. A hung command is therefore killed *on the cluster*, process group and
all, before the local side gives up, rather than being left orphaned on a login
node. When the watchdog fires, the payload prints a distinctive notice and
exits 124; a *local* 124 now only means the ssh transport itself stalled.

The kill reaches the payload's process *group*. A child that deliberately
leaves it -- ``setsid``, a daemonizing program, a ``disown``ed background job
-- can outlive the watchdog, so "the watchdog fired" means the group was
killed, not that the login node is provably clear.

Exit-code convention shared by every ``bin/`` script:

    0        success
    1-63     passed through unchanged from the remote script
    64       usage error or unknown site
    69       no ControlMaster socket (emitted only during preflight, before any
             remote execution, so it is never ambiguous with a remote code)
    77       guard refusal: the payload contains a construct hpc-run does not
             send (see hpctether/guard.py); nothing was transmitted
    124      timeout — remote watchdog if the notice is present (nothing left
             running), otherwise a local transport stall
    255      ssh transport failure (ssh's own convention)
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass

from . import audit, gitsync, guard
from .commands import CODE_SUFFIXES
from .site import Site

EXIT_OK = 0
EXIT_USAGE = 64
EXIT_NO_MASTER = 69
EXIT_GUARD = 77
#: A script root the payload uses holds code git doesn't know about, or is not
#: a git checkout: nothing in the payload ran. Set on the cluster.
EXIT_UNTRUSTED = 78
#: A job of the name a payload submits is already queued or running, or the
#: queue couldn't be read: nothing in the payload ran. Set on the cluster.
EXIT_DUPLICATE = 79
#: A ``[repos]`` checkout the payload writes with git has a different origin
#: from its local repository, or none: nothing in the payload ran. Set on the
#: cluster (or locally, when the local side has no origin).
EXIT_ORIGIN = 80

#: SLURM states that mean a job occupies, or is about to occupy, its output
#: directory. hpc-jobs --name and the duplicate check share this one set.
ACTIVE_JOB_STATES = ("PENDING", "RUNNING", "CONFIGURING", "COMPLETING",
                     "SUSPENDED", "RESIZING")
EXIT_TIMEOUT = 124
EXIT_SSH_FAILURE = 255

#: A SLURM job id: digits, with an optional array (`123_4`, `123_[5-9]`) or
#: heterogeneous-job (`123+1`) suffix. hpc-jobs and hpc-probe refuse anything
#: else before it reaches a remote command line.
JOBID_RE = re.compile(r"^[0-9]+([_+][0-9A-Za-z\[\]\-,]+)?$")

#: A user name for ``hpc-jobs --user``: never one that starts with a dash.
USER_RE = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]*$")

#: Applied to every ssh invocation.
#:
#: BatchMode=yes: without it a dead master falls back to an interactive
#: prompt that no agent can answer, and the call hangs until timeout. With it,
#: failure is immediate and legible.
SSH_BASE_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")

DEFAULT_TIMEOUT = 120

#: Seconds subtracted from the local timeout to size the remote watchdog, so
#: the watchdog always fires first and kills the process group on the cluster.
WATCHDOG_MARGIN = 15

#: Floor for the remote watchdog budget.
WATCHDOG_MIN = 15

#: Grace between the watchdog's TERM and its follow-up KILL.
WATCHDOG_KILL_AFTER = 10


#: Prelude names every site has, besides its [paths] roles.
FIXED_ROLES = ("HPC_SITE", "HPC_HOST", "HPC_USER", "HPC_SCHEDULER")


def watchdog_budget(local_timeout: int) -> int:
    """Remote watchdog seconds for a given local timeout."""
    return max(local_timeout - WATCHDOG_MARGIN, WATCHDOG_MIN)


class TransportError(Exception):
    """A transport-level failure with an associated exit code."""

    def __init__(self, message: str, code: int = EXIT_SSH_FAILURE) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Result:
    """Outcome of a remote invocation."""

    returncode: int
    stdout: str
    stderr: str


def reopen_hint(site: Site) -> str:
    """The exact command the user must run to reopen an expired master.

    Reopening requires interactive 2FA, which only the user can supply. Naming
    the literal command removes the guesswork from the one thing that is
    genuinely blocked.
    """
    return f"ssh -fN {site.host}"


def check_master(site: Site) -> tuple[bool, str]:
    """Return ``(is_live, message)`` for the site's ControlMaster socket.

    Runs ``ssh -O check``, which touches the local socket only -- it does not
    open a connection and costs nothing on the remote.
    """
    argv = ["ssh", *SSH_BASE_OPTS, "-O", "check", site.host]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=15, check=False
        )
    except FileNotFoundError:
        raise TransportError("ssh not found on PATH", EXIT_SSH_FAILURE) from None
    except subprocess.TimeoutExpired:
        return False, f"ssh -O check {site.host} timed out after 15s"

    output = (proc.stdout + proc.stderr).strip() or "(no output)"
    return proc.returncode == 0, output


def require_master(site: Site) -> str:
    """Preflight gate: raise unless the master is live.

    Raises:
        TransportError: with code 69 and a message naming ``ssh -fN <host>``.

    Code 69 is emitted only here, before any remote execution, so it can never
    be confused with an exit code produced by a remote script.
    """
    live, message = check_master(site)
    if live:
        return message
    raise TransportError(
        f"no live ControlMaster for {site.host}: {message}\n"
        f"An agent cannot reopen it -- that needs interactive 2FA.\n"
        f"Ask the user to run:  {reopen_hint(site)}",
        EXIT_NO_MASTER,
    )


#: Strict mode for the caller's script, inside the watchdog heredoc.
#:
#: ``set -Eeuo pipefail``, with an ERR trap that does two things.
#:
#: It names the command that ended the payload: under ``set -e`` a ``grep``
#: that matches nothing (exit 1) used to stop the payload with no output at
#: all, which read like a transport fault. LINENO counts from
#: the heredoc's first line, so the payload's own line is LINENO minus the
#: generated lines before it.
#:
#: And it lets a cut-off pipeline through. When ``head``, ``grep -m`` or
#: ``sed q`` exits early, the command feeding it dies of SIGPIPE (status 141),
#: and ``pipefail`` made that the pipeline's status: 25 payloads in the audit
#: log failed this way. The trap reads PIPESTATUS. If the status is 141 and
#: every command in the pipeline exited 0 or 141, nothing failed: the trap
#: turns ``-e`` off so bash does not exit (bash checks ``-e`` after running
#: the trap), and from then on the trap itself ends the payload on a failure,
#: as ``-e`` would have. Any other status anywhere in the pipeline stops the
#: payload as before; the message gives the statuses. A lone 141 counts as a
#: cut-off too: a function, subshell or ``$(...)`` whose last pipeline was cut
#: returns it.
#:
#: Limits: after the first cut-off pipeline a later ``set +e`` no longer turns
#: strict mode off (use ``|| true``), and a cut-off pipeline is still false in
#: ``if`` or before ``&&``. The trap is silent when ``-e`` is off by the
#: payload's own ``set +e``. ``set -E`` carries the trap into functions and
#: subshells. bash 3.2 does not run the ERR trap for a failed ``( ... )`` or a
#: pipeline ending in a loop, so there those still stop on a cut-off; the
#: cluster runs bash 4.4 or newer.
def strict_prelude(extra: int = 0) -> tuple[str, ...]:
    """The strict-mode lines, reporting payload line numbers net of these
    lines and the ``extra`` generated lines that follow them in the heredoc."""
    lines = [
        "set -Eeuo pipefail",
        "hpc_on_err() {",
        '  case $- in *e*) ;; *) [ -n "${hpc_errexit:-}" ] || return 0 ;; esac',
        '  hpc_rc=$1 hpc_line=$2 hpc_cmd=$3 hpc_bad= hpc_ps=; shift 3',
        '  for hpc_s in "$@"; do hpc_ps="$hpc_ps $hpc_s"; '
        'case $hpc_s in 0|141) ;; *) [ -n "$hpc_bad" ] || hpc_bad=$hpc_s ;; esac; done',
        '  if [ "$hpc_rc" -eq 141 ] && [ -z "$hpc_bad" ]; then '
        "set +e; hpc_errexit=1; return 0; fi",
        '  [ "$hpc_rc" -eq 141 ] || hpc_bad=$hpc_rc',
        '  if [ "$#" -gt 1 ]; then hpc_ps=" (pipeline statuses:$hpc_ps)"; else hpc_ps=; fi',
        "  printf 'hpc-tether: payload stopped at line %s (exit %s): %s%s -- strict mode "
        "ends the payload on the first failing command; add || true to one that may "
        "fail, e.g. a grep with no match\\n' "
        '"$((hpc_line - @OFFSET@))" "$hpc_bad" "$hpc_cmd" "$hpc_ps" >&2',
        '  exit "$hpc_bad"',
        "}",
        "trap 'hpc_on_err \"$?\" \"$LINENO\" \"$BASH_COMMAND\" \"${PIPESTATUS[@]}\"' ERR",
    ]
    offset = str(len(lines) + extra)
    return tuple(line.replace("@OFFSET@", offset) for line in lines)


#: As every payload carries it: one generated line (the readonly roles)
#: always follows.
STRICT_PRELUDE = strict_prelude(1)

#: After the payload's last line, inside the heredoc. A payload whose last
#: command was a cut-off pipeline ends with status 141 although nothing
#: failed; this reports 0 instead. It runs only when the payload falls off
#: its end: an ``exit`` or a stop by strict mode never reaches it. The blank
#: line ends a trailing ``\`` continuation before it. Not an EXIT trap: bash
#: 3.2 reports an unset-variable stop as 0 to one under ``set -e``.
STRICT_EPILOGUE = (
    "",
    'hpc_rc=$?; if [ "$hpc_rc" -eq 141 ]; then exit 0; fi; exit "$hpc_rc"',
)

#: A payload has no terminal, so git asking for a password or a host-key
#: confirmation waits for an answer that never comes, until the watchdog kills
#: it, and the message says nothing about credentials. These make a missing
#: credential fail at once with git's own error. ``GIT_TERMINAL_PROMPT=0``
#: covers HTTPS; ``BatchMode=yes`` covers git over ssh. A user's own
#: ``GIT_SSH_COMMAND`` is kept: the default applies only when none is set.
GIT_NO_PROMPT = (
    "export GIT_TERMINAL_PROMPT=0",
    'export GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes}"',
)


def trusted_check(roots, files=()) -> list[str]:
    """Shell lines that refuse to run anything if a script root is not clean,
    or if a script the payload runs (``files``, as ``(root, path)``) is not a
    file git tracks there.

    Trusted means committed. Before a payload that runs a trusted script, each
    root it uses must be a git checkout with no modified and no untracked code
    file (CODE_SUFFIXES, bytecode aside). Code that reached the root by any
    route other than a commit -- a trusted script writing one, an edit made
    outside this toolkit -- stops the payload with exit 78 before its first
    line. Ignored files are not counted: a .gitignore entry is the user's
    statement that the path holds generated artifacts (build_scripts/*.sh,
    __pycache__), and an agent can no longer write code into a root itself.
    The script being run is held to more: it must be tracked. An ignored
    ``.py`` under a root, which a trusted script could have generated, passed
    the status check and ran as trusted.
    Fails closed: no git, or a git error, also stops it.
    """
    specs = " ".join(shlex.quote(f":(icase)*{s}")
                     for s in sorted(CODE_SUFFIXES - {".pyc", ".pyo"}))
    lines = ["# --- hpc-tether: trusted scripts run only from a clean checkout ---"]
    for root in sorted(set(roots)):
        q = shlex.quote(root)
        lines += [
            f"if ! hpc_dirty=$(git -C {q} status --porcelain --untracked-files=all -- {specs}); then",
            f"  printf 'hpc-tether: cannot check script root %s with git (not a checkout, or git failed); nothing was run\\n' {q} >&2",
            f"  exit {EXIT_UNTRUSTED}",
            "fi",
            'if [ -n "$hpc_dirty" ]; then',
            f"  printf 'hpc-tether: script root %s holds code git does not know about; nothing was run:\\n' {q} >&2",
            "  printf '%s\\n' \"$hpc_dirty\" >&2",
            "  printf 'Commit it, or add generated files to .gitignore: for the user to decide.\\n' >&2",
            f"  exit {EXIT_UNTRUSTED}",
            "fi",
        ]
    for root, path in sorted(set(files)):
        # Relative to the root when the spelling allows, so a root reached
        # through a symlink is not compared as text against its real path.
        rel = path[len(root) + 1:] if path.startswith(root.rstrip("/") + "/") else path
        q, f = shlex.quote(root), shlex.quote(rel)
        full = shlex.quote(rel if rel.startswith("/") else root.rstrip("/") + "/" + rel)
        lines += [
            f"if ! git -C {q} ls-files --error-unmatch -- {f} >/dev/null 2>&1; then",
            f"  printf 'hpc-tether: %s is not a file git tracks in script root %s, so it is not trusted; nothing was run\\n' {f} {q} >&2",
            "  printf 'A trusted script is one you committed. Commit it, or run it by hand.\\n' >&2",
            f"  exit {EXIT_UNTRUSTED}",
            "fi",
            # A committed symlink is tracked, but it can point at a file git
            # does not track (an untracked file in scratch).
            f"if [ -L {full} ]; then",
            f"  printf 'hpc-tether: %s is a symlink, and it may point outside the commit, so it is not trusted; nothing was run\\n' {f} >&2",
            "  printf 'A trusted script is a regular file you committed. Commit the file itself, or run it by hand.\\n' >&2",
            f"  exit {EXIT_UNTRUSTED}",
            "fi",
        ]
    return lines


def duplicate_check(names) -> list[str]:
    """Shell lines that refuse to run anything if a job of one of ``names`` is
    already queued or running for this user (exit 79).

    Two jobs on one output directory race on files, double-spend the
    allocation, and produce corrupt results that still look plausible. This
    makes the queue check part of the submission instead of an instruction
    to remember. Fails closed: if squeue is missing or errors, the queue
    state is unknown and nothing runs -- an empty answer from a failed query
    is how a duplicate was once green-lit.
    """
    states = ",".join(ACTIVE_JOB_STATES)
    lines = ["# --- hpc-tether: no second job of a name already in the queue ---"]
    for name in sorted(set(names)):
        q = shlex.quote(name)
        lines += [
            f'if ! hpc_queue=$(squeue -u "$HPC_USER" -h -n {q} -t {states} -o "%i %T"); then',
            f"  printf 'hpc-tether: squeue failed, so whether a job named %s is already queued is unknown; nothing was run\\n' {q} >&2",
            f"  exit {EXIT_DUPLICATE}",
            "fi",
            'if [ -n "$hpc_queue" ]; then',
            f"  printf 'hpc-tether: a job named %s is already queued or running; nothing was run:\\n' {q} >&2",
            "  printf '%s\\n' \"$hpc_queue\" >&2",
            "  printf 'Two jobs on one output directory corrupt each other. Cancel or wait for it first, in a separate payload.\\n' >&2",
            f"  exit {EXIT_DUPLICATE}",
            "fi",
        ]
    return lines


def origin_check(checks) -> list[str]:
    """Shell lines that refuse to run anything if a ``[repos]`` checkout's
    origin is not the repository its entry pairs it with (exit 80).

    ``checks`` holds ``(name, remote path, expected normalized URL)``. Fails
    closed: no checkout, no origin, or a git error also stops the payload.
    """
    lines = ["# --- hpc-tether: [repos] checkouts must have the local origin ---",
             *gitsync.SHELL_NORMALIZE.splitlines()]
    for name, remote, expected in sorted(set(checks)):
        n, q, e = shlex.quote(name), shlex.quote(remote), shlex.quote(expected)
        lines += [
            f"if ! hpc_url=$(git -C {q} config --get remote.origin.url); then",
            f"  printf 'hpc-tether: [repos] %s: %s is not a git checkout with an origin remote; nothing was run\\n' {n} {q} >&2",
            f"  exit {EXIT_ORIGIN}",
            "fi",
            f'if [ "$(hpc_norm "$hpc_url")" != {e} ]; then',
            f"  printf 'hpc-tether: [repos] %s: the cluster checkout %s has origin %s, but the local repository is %s; nothing was run.\\n' {n} {q} \"$hpc_url\" {e} >&2",
            "  printf 'The entry pairs two different repositories. Fixing it is the user'\\''s to do, in the site file.\\n' >&2",
            f"  exit {EXIT_ORIGIN}",
            "fi",
        ]
    return lines


def build_payload(
    site: Site,
    script: str,
    local_timeout: int = DEFAULT_TIMEOUT,
    trust_uses=None,
    job_names=None,
    origin_checks=None,
) -> str:
    """Assemble the bash payload: prelude + watchdog + the caller's script.

    The prelude exports every ``[paths]`` role as ``HPC_<KEY_UPPER>`` so remote
    scripts reference site paths by role instead of hardcoding a cluster path.

    The caller's script runs inside ``timeout --kill-after=N <budget> bash``
    fed by a quoted heredoc on fd 3 (stdin is ``/dev/null``, so a command that
    reads stdin gets end-of-file instead of the rest of the script), with the
    budget sized by :func:`watchdog_budget`
    to fire *before* the local timeout. GNU timeout signals the whole process
    group, so a hung command dies on the cluster along with its children
    instead of surviving as an orphan on the login node -- excepting a child
    that has left the group (``setsid`` and friends). Strict mode lives
    inside the heredoc; the outer wrapper stays lax so it can report the
    watchdog verdict after ``timeout`` returns.

    Values are quoted with ``shlex.quote``. That is quoting *inside a generated
    file that no local shell reads*, not quoting of a command line -- the file
    is delivered on stdin.
    """
    budget = watchdog_budget(local_timeout)
    delim = "__HPC_BODY__"
    while delim in script:
        delim += "_"

    body = script if script.endswith("\n") else script + "\n"
    lines = [
        "# --- hpc-tether prelude (generated) ---",
        f"export HPC_SITE={shlex.quote(site.name)}",
        f"export HPC_HOST={shlex.quote(site.host)}",
        f"export HPC_USER={shlex.quote(site.user)}",
        f"export HPC_SCHEDULER={shlex.quote(site.scheduler)}",
    ]
    for key, value in sorted(site.env().items()):
        lines.append(f"export {key}={shlex.quote(value)}")
    lines.extend(GIT_NO_PROMPT)
    lines.append("# --- end prelude ---")
    # The clean-checkout check runs inside the watchdog with the payload, so
    # a git status that hangs on a slow filesystem is killed like anything else.
    # The roles are fixed for the payload: a body that reassigns one would
    # change what a $-spelled path means after the guard resolved it. The
    # guard refuses the spellings it can read; this stops the rest.
    roles = [*FIXED_ROLES, *sorted(site.env())]
    check = ["readonly " + " ".join(roles)]
    if trust_uses:
        check += trusted_check(
            (u.root for u in trust_uses),
            [(u.root, u.path) for u in trust_uses if getattr(u, "path", None)])
    if job_names:
        check += duplicate_check(job_names)
    if origin_checks:
        check += origin_check(origin_checks)
    lines.extend(
        [
            # The script arrives on fd 3 and stdin is /dev/null. With the
            # script on stdin, a command that reads stdin (srun, a nested
            # ssh, `cat`, `read`, a script asking "Proceed? [y/N]") consumed
            # the payload's remaining lines, which then never ran, and the
            # payload still exited 0.
            f"timeout --kill-after={WATCHDOG_KILL_AFTER} {budget} bash /dev/fd/3 "
            f"3<<'{delim}' </dev/null",
            *strict_prelude(len(check)),
            *check,
            body.rstrip("\n"),
            *STRICT_EPILOGUE,
            delim,
            "rc=$?",
            'if [ "$rc" -eq 124 ]; then',
            f'  echo "hpc-tether: remote watchdog killed the payload after '
            f'{budget}s on $HPC_HOST; its process group was killed" >&2',
            '  echo "hpc-tether: if the work was legitimate, rerun it as a background '
            'Bash call (run_in_background) with a larger --timeout; never size '
            '--timeout to the 10-minute foreground Bash limit" >&2',
            "fi",
            'exit "$rc"',
        ]
    )
    return "\n".join(lines) + "\n"


def run_remote(
    site: Site,
    script: str,
    timeout: int = DEFAULT_TIMEOUT,
    capture: bool = False,
    preflight: bool = True,
    trust_uses=None,
    record_body: bool = False,
    job_names=None,
    origin_checks=None,
    audit_extra: dict | None = None,
) -> Result:
    """Execute ``script`` remotely by piping the assembled payload to ``bash -s``.

    Args:
        site: resolved site definition.
        script: bash source. Assembled with the prelude by ``build_payload``.
        timeout: local wall-clock limit in seconds.
        capture: capture stdout/stderr instead of letting them stream through.
        preflight: run ``require_master`` first (default). Only bypass this
            when the caller has already checked.
        record_body: keep the script text in the audit line. hpc-run sets
            it for every payload; the fixed tools leave it off, since their
            arguments (in the line's argv) say what they sent.
        audit_extra: fields merged into every audit line this call writes
            (hpc-run's ``level``).
        trust_uses: pre-built scripts this payload runs, taken from the
            ``uses`` of the ``guard.classify`` report that allowed this
            payload -- never recomputed here. Each is named in the audit
            record so the log says which script root made it read tier.

    Returns:
        Result: with the remote script's exit code passed through unchanged.

    Raises:
        TransportError: 77 (payload deletes; nothing sent), 69 (no master),
            124 (local timeout), 255 (ssh failure).
    """
    detail = audit.first_line(script)
    trusted = audit.trusted_records(trust_uses)
    deletion = guard.contains_deletion(script)
    if deletion is not None:
        audit.record("refused", site.name, site.host, deletion.line, EXIT_GUARD,
                     payload=script, trusted=trusted, extra=audit_extra,
                     body=script if record_body else None)
        raise TransportError(
            "refusing to transmit a payload that deletes on the cluster "
            f"(line {deletion.lineno}: {deletion.line}). This toolkit never "
            "deletes; the command is the user's to run by hand.",
            EXIT_GUARD,
        )

    if preflight:
        require_master(site)

    payload = build_payload(site, script, local_timeout=timeout,
                            trust_uses=trust_uses, job_names=job_names,
                            origin_checks=origin_checks)
    argv = ["ssh", *SSH_BASE_OPTS, site.host, "bash", "-s"]

    with audit.Timer() as clock:
        try:
            proc = subprocess.run(
                argv,
                input=payload,
                text=True,
                # Binary output (a tail of a compressed file) must not crash
                # the verb before the audit entry below is written.
                errors="replace",
                timeout=timeout,
                capture_output=capture,
                check=False,
            )
        except FileNotFoundError:
            raise TransportError("ssh not found on PATH", EXIT_SSH_FAILURE) from None
        except subprocess.TimeoutExpired:
            audit.record("payload", site.name, site.host, detail, EXIT_TIMEOUT,
                         seconds=timeout, payload=script, trusted=trusted,
                         extra=audit_extra, body=script if record_body else None)
            raise TransportError(
                f"remote execution on {site.host} exceeded the local timeout of "
                f"{timeout}s; the remote process may still be running",
                EXIT_TIMEOUT,
            ) from None

    audit.record("payload", site.name, site.host, detail, proc.returncode,
                 seconds=clock.seconds, payload=script, trusted=trusted,
                 extra=audit_extra, body=script if record_body else None)
    return Result(
        returncode=proc.returncode,
        stdout=proc.stdout or "" if capture else "",
        stderr=proc.stderr or "" if capture else "",
    )


def run_capture(
    site: Site, script: str, timeout: int = DEFAULT_TIMEOUT, trust_uses=None,
    audit_extra: dict | None = None,
) -> Result:
    """``run_remote`` with output captured. Convenience for parsing callers."""
    return run_remote(site, script, timeout=timeout, capture=True,
                      trust_uses=trust_uses, audit_extra=audit_extra)
