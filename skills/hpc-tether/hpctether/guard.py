"""Payload guard: classify a bash script before it leaves the local machine.

Every ``hpc-run`` payload passes through :func:`classify` first. The scan is
lexical and deliberately conservative — it guards against habit under context
pressure, not against an adversary. A false refusal costs one ``--dry-run`` to
read the reason; a false pass costs shared, often unbacked-up data.

Five violation kinds, from the caller's point of view:

``forbidden``
    Never sent, no override flag exists. **Every deletion**: ``rm``, ``rmdir``,
    ``unlink``, ``shred``, ``find -delete``, ``git clean``, and their wrapped
    spellings -- there is no path, flag, or site setting under which this
    toolkit deletes anything on the cluster. Also **any write outside the
    write roots** (hpctether/confine.py: every target of ``cp``, ``mv``,
    ``mkdir``, ``tee``, a redirect, a git write, an NCO output, ...; one the
    scan can't place counts as outside), **in-place editing** (``sed -i``,
    ``perl -i``, ``vi``, ``ex``, ``ed``, ``patch``: pull, edit locally, push
    instead), **a code file written into a script root** (code arrives there
    only through git), ``git reset --hard``, force pushes, ``chown``, recursive
    ``chmod``, ``dd``, ``truncate``, ``mkfs``, ``crontab``/``at`` (persistence
    outside the watchdog), and ``scancel`` of every job at once. These are the
    user's to run by hand in their own terminal.

``traversal``
    Never sent either, but the fix is a bound, not a different operator:
    ``find`` without ``-maxdepth``, ``du`` without ``-s``/``--max-depth``,
    ``ls -R``, recursive ``grep``. On a shared parallel filesystem the cost of
    these is the tree walked, and an unbounded walk on a login node is exactly
    what triggers cluster resource warnings.

``interpreter``
    Sent only with ``--allow-exec``. Commands that execute a *string* rather
    than a fixed operation: ``python -c``, ``perl -e``, a nested ``bash -c``,
    ``eval``, ``srun``, ``make``, and network fetchers (``curl``, ``wget``,
    ``scp``) that can pull code in or push data out. This tier exists because
    the rest of this module is lexical: it classifies command *names*, so any
    command that interprets arbitrary text walks straight through every other
    check. ``python -c "import shutil; shutil.rmtree(...)"`` is exactly the
    ``rm`` this guard refuses, spelled in a language the guard cannot read.
    Enumerating dangerous spellings can never catch a general-purpose
    interpreter — so interpreters are classified by capability instead, and
    never reach read tier.

``write``
    Sent only when the caller passes ``--allow-write``, which the skill ties to
    explicit per-use user confirmation: ``sbatch``, ``scancel``, git writes,
    ``mkdir``, ``touch``, ``cp``, output redirection to a real file, and
    ``mv`` -- each only when
    every file it writes is inside a write root: the scratch role plus any
    ``writable_roots``, through the site's ``[aliases]``. A rename inside a directory
    the user owns is routine work, and a wrong one is undone by moving the
    file back; refusing it outright stops real work at a physical terminal for
    no safety gain. Deletion has no such undo, so it is never write tier.

One deliberate exception cuts across the interpreter tier: a **pre-built
script** (``hpctether/trust.py``) -- a ``.py`` file under one of the site's
``script_roots``, repos the user wrote and maintains. ``python
$HPC_REPO/my-script.py check CASE`` is opaque to this module either way; the
strictness belongs on hand-rolled text, which it can read. Such an invocation
is read tier whatever its arguments, unless it matches a ``script_confirm``
pattern (write tier). Inline code (``python -c``, ``python - <<EOF``) and a
``.py`` outside every root stay interpreter tier. No root softens a
forbidden-tier construct, and none reaches inside the file: a script under a
root that deletes will delete.

``unknown``
    A command no rule claims and ``READ_COMMANDS`` does not list. Gated like
    an interpreter: profile ``improvise`` and ``--allow-exec``. Once
    an unrecognized command defaulted to read tier, so every
    dangerous spelling needed its own branch here, found after the fact; and
    ``ncks -O``, ``cdo`` and ``nccopy`` wrote with no token. Failing closed
    ends that: a new command is a line in hpctether/commands.py, which holds
    every command-name table this module classifies by.

Scope, stated plainly: this scan guards against habit under context pressure,
and against payload text steered by something read along the way. It is not
proof against a determined adversary with full control of the payload —
``eval`` reached through variable indirection, or a base64 blob decoded at run
time, will still get through. Defence in depth is the remote account's own
permissions, not this file.

How it is built: hpctether/shell.py parses the payload into commands and
decides nothing; hpctether/rules.py holds the rules, tried in order, each
small and each with the incident that put it there; hpctether/commands.py
holds the command tables the rules look up. This module joins them: parse,
apply the payload-wide checks, run the rules on every command, report.
"""

from __future__ import annotations

import dataclasses
import posixpath
import re

from hpctether import confine, shell
from hpctether.commands import (
    DELETION_CMDS as _DELETION_CMDS,
    LOADER_ENV as _LOADER_ENV,
)
from hpctether.rules import (  # noqa: F401  (re-exported: the guard's API)
    FORBIDDEN,
    INTERPRETER,
    TRAVERSAL,
    UNKNOWN,
    WRITE,
    ROLE_ASSIGN_RE as _ROLE_ASSIGN_RE,
    Violation,
    check_segment as _check_segment,
    role_assigned as _role_assigned,
    role_message as _role_message,
)
from hpctether.shell import (  # noqa: F401  (kept for callers and tests)
    SUBST_PLACEHOLDER as _SUBST_PLACEHOLDER,
    mask_substitutions as _mask_substitutions,
    unmask as _unmask,
)


@dataclasses.dataclass
class Report:
    """Outcome of classifying one script.

    ``uses`` is the pre-built script invocations this scan actually allowed,
    in source order. It is produced by the same walk that classified them, and
    it is what the audit records: two separate parses of the same text is how
    a payload once came to be authorized against one file and to run another
   . One parse, one answer.
    """

    violations: list[Violation] = dataclasses.field(default_factory=list)
    uses: list = dataclasses.field(default_factory=list)
    #: Why the scan treated the payload more strictly than its commands
    #: alone would suggest (e.g. script roots ignored). For the caller to
    #: print alongside a refusal.
    notes: list[str] = dataclasses.field(default_factory=list)

    def worst(self) -> str | None:
        for kind in (FORBIDDEN, TRAVERSAL, INTERPRETER, UNKNOWN, WRITE):
            if any(v.kind == kind for v in self.violations):
                return kind
        return None

    def of_kind(self, kind: str) -> list[Violation]:
        return [v for v in self.violations if v.kind == kind]

    def repo_writes(self) -> list[str]:
        """Names of the ``[repos]`` checkouts this payload's git writes touch."""
        return sorted({v.repo for v in self.violations if v.repo and v.kind == WRITE})


#: The site's own prelude form: prepend the declared Python environment. It
#: points PATH at a site role the user set, so it does not taint trusted calls.
_PYTHON_BIN_PATH_RE = re.compile(
    r'(?:export\s+)?PATH=(["\']?)\$\{?HPC_PYTHON_BIN\}?:\$\{?PATH\}?\1(?=[\s;&|)]|$)')

#: A loader variable named other than as an expansion (`$X`, `${X}`):
#: `X=`, `export X`, `env X=`, `read X`, `declare -x X`, ... all set it.
_LOADER_ENV_RE = re.compile(
    r"(?<![\w${])(" + "|".join(sorted(_LOADER_ENV, key=len, reverse=True)) + r")(?![\w])")


def _loader_env_set(script: str) -> str | None:
    """The first loader variable the payload sets, or None (see LOADER_ENV).

    Deliberately broad: any non-expansion mention counts, so a false hit only
    costs a trusted script its token-free status, never the reverse.
    """
    for line in script.splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        text = _PYTHON_BIN_PATH_RE.sub(" ", text)
        match = _LOADER_ENV_RE.search(text)
        if match:
            return match.group(1)
    return None


def classify(
    script: str,
    roots: list[str] | None = None,
    scratch: str | None = None,
    trust=None,
    paths: "dict[str, str] | None" = None,
    aliases: "dict[str, str] | None" = None,
    script_roots: "list[str] | None" = None,
    repos=None,
    protected: "list[str] | None" = None,
) -> Report:
    """Classify every command in ``script``.

    Args:
        script: the payload's bash source.
        roots: the site's writable roots -- its scratch role plus any
            ``writable_roots`` it declares. ``mv`` confined to one of these is
            write-tier; anywhere else it is forbidden.
        scratch: the site's scratch role. Kept for callers; deletion is no
            longer confined to it or to anything else -- ``rm`` is forbidden
            everywhere. Passing None for both leaves only the ``$HPC_SCRATCH``
            spelling recognizable for ``mv``.
        trust: the site's script roots (:class:`hpctether.trust.Roots`), or
            None. A ``.py`` file under one, run by an interpreter or directly,
            is read tier, or write tier if it matches a ``script_confirm``
            pattern; anything else falls through to the ordinary rules, which
            put it in interpreter tier. Never softens a forbidden-tier
            construct, and never reaches inside the script.
        paths: the site's ``[paths]`` roles, so ``cd $HPC_REPO && ...`` is
            recognized as the directory it expands to. The guard runs locally
            on unexpanded text, so this textual expansion is how a
            role-spelled ``cd`` is resolved at all.
        protected: ``[policy] protected``: directories never written, even
            inside a write root.
        repos: the site's ``[repos]`` (name -> :class:`hpctether.site.Repo`).
            A git write in a listed checkout follows that entry's mode
            instead of the write roots (see ``rules._repo_sync_refusal``).

    Returns:
        Report: every violation found, in source order.
    """
    trust_index = trust if trust else None
    loader_var = _loader_env_set(script) if trust_index else None
    if loader_var:
        trust_index = None
    if trust_index and shell.HOME_TOUCHED_RE.search(script):
        trust_index = dataclasses.replace(trust_index, tilde=False)
    home = trust_index.home if trust_index else None
    cd_redefined = bool(shell.CD_REDEFINED_RE.search(script))

    # Write confinement (hpctether/confine.py). With no roots there is no
    # site context, and every write is refused rather than waved through.
    # `scratch` is the scratch role, passed on its own by older callers.
    if scratch and "scratch" not in (paths or {}):
        paths = {**(paths or {}), "scratch": scratch}
    login_dir = (paths or {}).get("home")
    tilde = None if shell.HOME_TOUCHED_RE.search(script) else login_dir
    alias_pairs = confine.sort_aliases(aliases or {})
    canon_roots = (None if roots is None else tuple(
        confine.canonical(posixpath.normpath(r), alias_pairs) for r in roots))
    # The prelude never changes directory, so a payload with no cd at all
    # runs where sshd put it: the login directory.
    start_dir = login_dir if not shell.ANY_CD_RE.search(script) else None
    base_ctx = confine.Context(
        roots=canon_roots, paths=paths or {}, home=tilde, aliases=alias_pairs,
        cwd=start_dir,
        script_roots=tuple(confine.canonical(posixpath.normpath(r), alias_pairs)
                           for r in (script_roots or ())),
        repos=confine.repo_rules(repos, alias_pairs),
        protected=tuple(confine.canonical(posixpath.normpath(r), alias_pairs)
                        for r in (protected or ())))
    report = Report()
    if loader_var:
        report.notes.append(
            f"script roots are ignored for this payload: it sets {loader_var}, "
            "which changes what code a trusted script loads or runs (a "
            "PYTHONPATH pointing at scratch makes it import code from there). "
            "Drop the assignment; the only PATH change allowed alongside a "
            'trusted script is export PATH="$HPC_PYTHON_BIN:$PATH"')
    collected: list = []
    for lineno, raw, line in shell.command_lines(script):
        # A payload that reassigns a site role rewrites what every role-spelled
        # script path means, AFTER this scan has resolved it: `HPC_REPO=/tmp/x`
        # leaves `$HPC_REPO/my-script.py` resolving under a script root here while
        # the remote runs a file from anywhere.
        # The roles are the prelude's to set. Refuse the reassignment rather
        # than try to track it: this scan does not evaluate shell state, so
        # anything short of refusal is a guess.
        role_assign = _ROLE_ASSIGN_RE.match(line)
        if role_assign:
            report.violations.append(Violation(
                FORBIDDEN, lineno, raw.rstrip(),
                _role_message(role_assign.group(1), "reassigning")))
            continue

        parsed = shell.parse_line(
            lineno, raw, line, paths or {}, trusted_home=home, write_home=tilde,
            track_trusted=bool(trust_index), cd_redefined=cd_redefined)
        n_before = len(collected)
        for seg in parsed.segments:
            role = _role_assigned(seg.text)
            if role:
                report.violations.append(Violation(
                    FORBIDDEN, lineno, raw.rstrip(), _role_message(role, "setting")))
                continue
            ctx = dataclasses.replace(base_ctx, cwd=seg.write_dir or start_dir)
            report.violations.extend(
                _check_segment(seg.text.strip(), lineno, raw.rstrip(), ctx,
                               trust_index, collected, seg.cd_dir))
        # A trusted use found on the masked line records its real arguments.
        if parsed.originals:
            for k in range(n_before, len(collected)):
                use = collected[k]
                if _SUBST_PLACEHOLDER in use.args:
                    collected[k] = dataclasses.replace(
                        use, args=_unmask(use.args, parsed.originals))

    # A payload that will not be sent authorized nothing. Reporting uses for a
    # refused script is the same authorized-here/verified-there split this walk
    # exists to close: `out=$(python3 $HPC_REPO/my-script.py check X)` is refused
    # for its assignment wrapper while the inner invocation matches a `free`
    # pattern, and the caller must not be handed a grant for a payload it is
    # about to reject.
    if any(v.kind in (FORBIDDEN, TRAVERSAL) for v in report.violations):
        return report

    # Deduplicate on (path, args), preserving source order: the audit names
    # each distinct invocation once.
    seen: set[tuple[str, str]] = set()
    for use in collected:
        key = (use.path, use.args)
        if key in seen:
            continue
        seen.add(key)
        report.uses.append(use)
    return report


def contains_deletion(script: str) -> Violation | None:
    """The first deletion in ``script``, or None.

    This is the transport-layer invariant: :mod:`transport` refuses to send
    any payload for which this returns a violation, whichever verb assembled
    it. ``classify`` is the full policy and runs only in ``hpc-run``; this is
    the one rule that holds for every verb, so that "the toolkit never deletes
    anything on the cluster" is a property of the transport, not a convention
    each tool has to remember.
    """
    for v in classify(script).violations:
        if v.kind != FORBIDDEN:
            continue
        if (
            v.message.startswith(tuple(f"{c!r}" for c in _DELETION_CMDS))
            or "find with -delete" in v.message
            or "git clean" in v.message
        ):
            return v
    return None


#: A job name the duplicate check can quote safely: no shell syntax at all.
_LITERAL_JOB_NAME = re.compile(r"^[A-Za-z0-9_.+:@%-]+$")


def _subst_body(text: str) -> str:
    """The command text inside a held ``$(...)``, ``<(...)`` or backtick."""
    if text.startswith("`"):
        return text.strip("`")
    return text[2:-1] if text.endswith(")") else text[2:]


def _sbatch_commands(text: str, box: list):
    """Every ``sbatch`` command in ``text``, including those inside command
    substitutions, as ``(tokens, loop_depth)``.

    ``box`` is the loop depth, ``[n]``, updated as ``do``/``done`` are seen.
    A substitution body is walked where it sits in the line, at the depth
    there, so ``for x in a b; do id=$(sbatch -J n j.sh); done`` counts as a
    loop. ``a=$(sbatch --parsable -J x j.sh)`` is how a job id is captured,
    and masking the body discarded it.
    """
    masked, originals = shell.mask_substitutions(text)
    for segment in shell.split_segments(masked):
        words = shell.tokenize(segment.strip())
        if words[:1] == ["do"]:
            box[0] += 1
        elif words[:1] == ["done"]:
            box[0] = max(0, box[0] - 1)
        tokens = shell.strip_wrappers(words)
        if tokens and tokens[0].rsplit("/", 1)[-1] == "sbatch":
            yield tokens, box[0]
        for n in re.findall(re.escape(_SUBST_PLACEHOLDER) + r"(\d+)__", segment):
            if int(n) < len(originals):  # the payload may spell a placeholder
                yield from _sbatch_commands(_subst_body(originals[int(n)]), [box[0]])


def _sbatch_walk(script: str):
    """``(line, tokens, loop_depth)`` for each ``sbatch`` in ``script``."""
    box = [0]
    for _, raw in shell.logical_lines(script):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        for tokens, depth in _sbatch_commands(line, box):
            yield line, tokens, depth


def _job_name(tokens: list[str]) -> str | None:
    name = None
    for k, tok in enumerate(tokens[1:], start=1):
        if tok in ("-J", "--job-name"):
            name = tokens[k + 1] if k + 1 < len(tokens) else None
        elif tok.startswith("--job-name="):
            name = tok.split("=", 1)[1]
        elif tok.startswith("-J") and len(tok) > 2:
            name = tok[2:]
    # A substitution is masked to a placeholder before this parse; the
    # placeholder looks literal, and checking the queue for it would report a
    # false "clear".
    if name and _LITERAL_JOB_NAME.match(name) and _SUBST_PLACEHOLDER not in name:
        return name
    return None


def sbatch_repeats(script: str) -> list[str]:
    """Literal job names this payload would submit more than once: named on
    two ``sbatch`` commands, or on one inside a loop. Commands inside
    ``$(...)`` count.

    The queue is checked once, before the payload's first line, so the second
    submission of a name in the same payload was not seen.
    """
    seen: list[str] = []
    repeats: list[str] = []
    for _, tokens, depth in _sbatch_walk(script):
        name = _job_name(tokens)
        if name:
            if (name in seen or depth) and name not in repeats:
                repeats.append(name)
            seen.append(name)
    return repeats


def sbatch_job_names(script: str) -> tuple[list[str], list[str]]:
    """``(literal names, unchecked submissions)`` for every ``sbatch`` in
    ``script``, those inside ``$(...)`` included.

    A name given as ``-J NAME``, ``-JNAME``, ``--job-name NAME`` or
    ``--job-name=NAME`` with no shell syntax in it can be checked against
    the queue before the payload runs (hpctether/transport.duplicate_check).
    A submission whose name is a variable, or comes only from the job
    script's ``#SBATCH`` lines, can't be; its line is returned in the second
    list so the caller can say so.
    """
    names: list[str] = []
    unchecked: list[str] = []
    for line, tokens, _ in _sbatch_walk(script):
        name = _job_name(tokens)
        if name:
            if name not in names:
                names.append(name)
        else:
            unchecked.append(line)
    return names, unchecked
