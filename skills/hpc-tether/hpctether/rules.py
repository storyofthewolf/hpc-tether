"""The guard's rules: what one parsed command may do. The gotcha list.

hpctether/shell.py turns a payload into commands; this module decides about
each one. Most rules exist because something went wrong in a real session or
a review: the comment or docstring on each says what. The rules are tried in
order (:data:`RULES`); the first that claims a command decides it. A command
no rule claims is a read if hpctether/commands.py lists it, and unknown
otherwise (fail closed).

A rule is a function ``(Command) -> list[Violation] | None``. ``None`` means
"not mine, ask the next rule"; a list (possibly empty) is the decision. So a
new gotcha is one function added at the right place in :data:`RULES`, or
one line in a table in commands.py, and the parser does not change.

Scope: these rules catch what an agent writes by habit or when steered by
something it read. They read text; they are not a sandbox.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from hpctether import confine
from hpctether.commands import (
    FORBIDDEN_ALWAYS,
    DELETION_CMDS,
    WRITE_CMDS,
    NO_FILE_TARGET,
    INTERPRETERS,
    SHELLS,
    EDITORS,
    EVAL_LIKE,
    EXEC_LAUNCHERS,
    BUILD_DRIVERS,
    NETWORK_CMDS,
    GIT_READ,
    GIT_DUAL_MODE,
    GIT_READ_ONLY_FLAGS,
    READ_COMMANDS,
    READ_OPTIONS,
    FIND_RUNS,
    unknown_option,
    sed_unknown,
    find_unknown,
    write_form_refusal,
    ARG_RULES,
    writes_by_args,
)
from hpctether.shell import (
    SUBST_PLACEHOLDER,
    path_args,
    short_flags,
    strip_wrappers,
    tokenize,
)

#: Violation kinds, in decreasing severity.
FORBIDDEN = "forbidden"
TRAVERSAL = "traversal"
INTERPRETER = "interpreter"
#: A command no rule claims and READ_COMMANDS does not list. The guard can't
#: tell whether it writes or runs code, so it is treated like an interpreter:
#: profile ``improvise`` and ``--allow-exec``.
UNKNOWN = "unknown"
WRITE = "write"


@dataclass(frozen=True)
class Violation:
    """One refused or gated construct, tied to the line that contains it."""

    kind: str  # forbidden | traversal | interpreter | unknown | write
    lineno: int
    line: str
    message: str
    #: The command name, where the level depends on it (job control is
    #: ``operate`` in hpctether/profile.py, other writes ``prepare``).
    command: str = ""
    #: The site ``[repos]`` entry a git write operates in, if any. The
    #: transport checks that checkout's origin before the payload runs.
    repo: str = ""
    #: False for a write that needs no ``--allow-write``: it is still write
    #: tier for the profile, still confined to the write roots, and still
    #: audited, but the user is not asked (see design.md, "Git writes that
    #: can be undone", and "The sandbox").
    token: bool = True
    #: False where the sandbox never waives the token: a git push, job
    #: control other than sbatch, an always_confirm match.
    sandboxable: bool = True
    #: For a construct whose target is not a plain file write -- an sbatch
    #: script, a trusted script's scope directory, an interpreter's working
    #: directory -- whether that target lies inside the roots this scan was
    #: given. None: decided by the kind (a write that was not refused).
    in_roots: "bool | None" = None


@dataclass
class Command:
    """One parsed command and what a rule needs to judge it."""

    tokens: list          # the command and its arguments, prefixes stripped
    raw_cmd: str          # the command word as written (`/bin/rm`, `./x.sh`)
    cmd: str              # its basename (`rm`)
    lineno: int
    line: str             # the source line, for the report
    ctx: "confine.Context | None"
    trust_index: object = None
    uses: "list | None" = None
    cwd: str | None = None  # directory for a relative trusted-script path

    def v(self, kind: str, message: str, **kw) -> Violation:
        return Violation(kind, self.lineno, self.line, message, **kw)


# ---------------------------------------------------------------------------
# Helpers shared by several rules

_CONFIRM = "needs --allow-exec and per-use user confirmation"

#: In-place editing is floor (plan §4.1): never sent, at any profile. The
#: edit loop is pull, edit locally, push -- which gives a diff, a backup and a
#: drift check that an in-place edit has none of. The author's audit found no
#: use of it in seven weeks.
IN_PLACE = (
    "{cmd} edits a remote file in place, which this toolkit never does at any "
    "profile. Pull the file (hpc-pull), edit it locally, and push it back "
    "(hpc-push): that keeps a backup and checks the remote didn't change "
    "meanwhile. For the same edit across many files, use a trusted script"
)


def confined_write(
    why: str | None, lineno: int, line: str, message: str, command: str = "",
    token: bool = True, sandboxable: bool = True,
) -> Violation:
    """A write-tier violation, or a forbidden one if it lands outside the
    write roots. Confinement is floor (hpctether/confine.py): no profile and
    no token sends an unconfined write."""
    if why:
        return Violation(FORBIDDEN, lineno, line, why, command=command)
    return Violation(WRITE, lineno, line, message, command=command, token=token,
                     sandboxable=sandboxable)


def unknown_form(lineno: int, line: str, why: str, command: str = "") -> Violation:
    """A known command in a form the guard can't call a read: an option it
    doesn't list, a program text it can't parse. Gated like an unknown
    command."""
    return Violation(
        UNKNOWN, lineno, line,
        f"{why}, so the guard can't tell whether it writes or runs code — "
        "needs profile improvise and --allow-exec. Use a form the guard knows, "
        "or report it if this form only ever reads",
        command=command)


# ---------------------------------------------------------------------------
# Site roles

#: An assignment to a site path role (`HPC_REPO=...`), with or without
#: `export`. These names are the prelude's; a body that rewrites one changes
#: the meaning of every role-spelled path after the guard has resolved it.
ROLE_ASSIGN_RE = re.compile(r"^(?:export\s+)?(HPC_[A-Z0-9_]+)=")
_ROLE_NAME_RE = re.compile(r"^(HPC_[A-Z0-9_]+)(?:\+?=|\[|$)")
#: Builtins that set a variable named as an argument.
_ASSIGNERS = frozenset({"export", "declare", "typeset", "local", "readonly",
                        "read", "unset", "let", "printf", "mapfile",
                        "readarray", "getopts", "for", "select"})
_ARITH_ROLE_RE = re.compile(r"\(\(.*(?<![$\w{])HPC_[A-Z0-9_]+")


def role_assigned(segment: str) -> str | None:
    """The site role a segment sets, or None.

    The line-start check alone missed `true; HPC_SCRATCH=/elsewhere; touch
    $HPC_SCRATCH/x`: confinement resolved the role as the site defines it
    while the shell used the new value. This
    looks at every segment: an assignment prefix, a name handed to a builtin
    that sets it (`read`, `export`, `printf -v`, `for`), and arithmetic. The
    payload prelude also marks the roles readonly, so a spelling missed here
    fails on the cluster.
    """
    tokens = tokenize(segment)
    for k, tok in enumerate(tokens[:-1]):
        if tok in ("for", "select"):
            m = _ROLE_NAME_RE.match(tokens[k + 1])
            if m:
                return m.group(1)
    command = strip_wrappers(tokens)
    # Everything before the command: assignment prefixes and `env NAME=...`.
    # (`NAME+=value` is not skipped as an assignment, so it shows up as the
    # command itself.)
    for tok in tokens[:len(tokens) - len(command)] + command[:1]:
        m = _ROLE_NAME_RE.match(tok)
        if m and "=" in tok:
            return m.group(1)
    if command and command[0] in _ASSIGNERS:
        for tok in command[1:]:
            m = _ROLE_NAME_RE.match(tok)
            if m:
                return m.group(1)
    m = _ARITH_ROLE_RE.search(segment)
    if m:
        return re.search(r"HPC_[A-Z0-9_]+", m.group(0)).group(0)
    return None


def role_message(role: str, verb: str) -> str:
    """The refusal for a payload that sets a site role (``verb``: how)."""
    if verb == "reassigning":
        return (f"{role} is a site path role set by the "
                "payload prelude; reassigning it in the body changes what "
                "every $-spelled path means, including a pre-built "
                "script's, after this scan has already resolved it. Use the "
                "role as the site defines it, or a literal path")
    return (f"{role} is a site path role set by the payload prelude; "
            "setting it in the body changes what every $-spelled path "
            "means after this scan has resolved it. Use the role as "
            "the site defines it, or a literal path")


# ---------------------------------------------------------------------------
# Redirection

#: Redirection targets that do not write a file.
_SAFE_REDIRECT = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})
_REDIRECT_RE = re.compile(r"\d*>>?\s*([^\s;|&)]+)")


def redirects(segment: str, lineno: int, line: str, ctx) -> list[Violation]:
    """Every redirect to a file in ``segment``, checked on the raw text so
    ``>file`` (no space) is seen."""
    out = []
    for match in _REDIRECT_RE.finditer(segment):
        target = match.group(1)
        if target.startswith("&") or target in _SAFE_REDIRECT:
            continue
        out.append(confined_write(
            confine.check_path(target, ctx, f"redirect writes {target!r}")
            or confine.redirect_code_refusal(target, ctx),
            lineno, line, f"redirects output to {target!r} on the remote"))
    return out


# ---------------------------------------------------------------------------
# The rules, in order. Each returns None to pass the command on.

def computed_name(c: Command):
    """A command name produced by a substitution (`$(echo rm) -rf /x`,
    `` `echo rm` -rf /x ``) is chosen at run time; no name rule below can
    classify it."""
    if SUBST_PLACEHOLDER in c.raw_cmd:
        return [c.v(INTERPRETER,
                    "the command name is computed by a substitution at run time, "
                    "so this scan cannot classify it — needs --allow-exec and "
                    "per-use user confirmation")]
    return None


def forbidden_always(c: Command):
    """Deletion and the other never-sent commands (commands.FORBIDDEN_ALWAYS)."""
    if c.cmd in FORBIDDEN_ALWAYS or c.cmd.startswith("mkfs"):
        why = (
            "deletes; this toolkit never deletes anything on the cluster, "
            "under any flag or path"
            if c.cmd in DELETION_CMDS
            else "is never sent through this tool"
        )
        return [c.v(FORBIDDEN,
                    f"{c.cmd!r} {why}; if it is truly needed, it is the user's to "
                    "run by hand")]
    return None


def trusted_script(c: Command):
    """A pre-built script is checked before the interpreter rules, and only
    those: it is precisely the `python /root/script.py args` shape that
    would otherwise land in interpreter tier, and the shape of a file invoked
    by path. It is checked AFTER the forbidden rules, which no root softens.

    The ``use`` is recorded because it is the record of what this scan
    allowed, and the audit must log exactly that rather than a second parse
    of the same text. What this does NOT do is look
    inside the script. A script under a root that deletes will delete; the
    root is the user's statement that they wrote and maintain that code.
    """
    if not c.trust_index:
        return None
    from . import trust as trust_mod

    use = trust_mod.match_invocation(c.tokens, c.trust_index, cwd=c.cwd)
    if use is None:
        return None
    if c.uses is not None:
        c.uses.append(use)
    if use.tier == "free":
        return []
    scoped = bool(use.scope_dir) and not use.always and c.ctx is not None
    in_roots = scoped and confine.check_path(use.scope_dir, c.ctx, "scope") is None
    return [c.v(WRITE,
                f"pre-built script {use.path} with arguments {use.args!r} matches "
                "a script_confirm pattern in the site file — needs --allow-write "
                "and per-use user confirmation",
                sandboxable=scoped, in_roots=in_roots)]


#: Interpreter flags that take a glued argument, so the letters after them
#: are not more flags: `perl -Mstrict` holds an `i` that isn't -i.
_GLUED_ARG_FLAGS = frozenset("MmIlx0eECdDVr")


def _interpreter_in_place(cmd: str, tokens: list[str]) -> bool:
    """``perl -pi -e``, ``ruby -i``, ``gawk -i inplace``: an in-place edit."""
    if cmd in ("awk", "gawk"):
        for k, t in enumerate(tokens):
            if t in ("-iinplace", "--include=inplace"):
                return True
            if t in ("-i", "--include") and tokens[k + 1:k + 2] == ["inplace"]:
                return True
        return False
    if cmd not in ("perl", "ruby"):
        return False
    for t in tokens[1:]:
        if t == "--" or not t.startswith("-"):
            break
        if t.startswith("--"):
            continue
        for ch in t[1:]:
            if ch == "i":
                return True
            if ch in _GLUED_ARG_FLAGS:
                break
    return False


def interpreter(c: Command):
    """Interpreters are checked before every name-based rule below: a command
    that runs a string can spell any of those operations in a language this
    scan cannot read, so it must never fall through to read tier. An
    interpreter's own in-place edit (`perl -pi`) is refused like `sed -i`."""
    if c.cmd not in INTERPRETERS:
        return None
    if _interpreter_in_place(c.cmd, c.tokens):
        return [c.v(FORBIDDEN, IN_PLACE.format(cmd=f"{c.cmd} -i"))]
    return [c.v(INTERPRETER,
                f"{c.cmd} executes arbitrary source the guard cannot inspect — "
                + _CONFIRM)]


def nested_shell(c: Command):
    if c.cmd not in SHELLS:
        return None
    detail = "runs a command string" if "-c" in c.tokens[1:] else "runs an arbitrary script"
    return [c.v(INTERPRETER,
                f"nested {c.cmd} {detail}, bypassing this scan — needs "
                "--allow-exec and per-use user confirmation")]


def eval_like(c: Command):
    if c.cmd not in EVAL_LIKE:
        return None
    return [c.v(INTERPRETER,
                f"{c.cmd!r} executes text assembled at run time — " + _CONFIRM)]


#: A command invoked by path rather than by name: `./run.sh`, `../x/tool`,
#: `bin/helper.py`, `$HOME/s.sh`. The file's contents are opaque to this scan,
#: so it is interpreter tier.
#:
#: Absolute paths under the standard system prefixes are deliberately excluded:
#: `/bin/rm` and `/usr/bin/squeue` are the same known commands spelled long,
#: and `cmd` (basename) already classifies them correctly. Anything else
#: absolute -- `/home/u/tool`, an install under /opt -- is a real unknown.
_PATH_INVOCATION_RE = re.compile(
    r"^(?!/(?:usr/)?s?bin/)"           # not a system binary spelled long
    r"(?:\./|\.\./|~/|\$|[^/\s]*/)"    # ./  ../  ~/  $VAR  dir/
    r".*"
)


def file_by_path(c: Command):
    """Invoking a file by path -- `./run.sh`, `bin/helper.py`, `/path/to/tool`.
    Its contents are as opaque to a lexical scan as a `-c` string, so it
    belongs in the same tier as python/bash/srun rather than defaulting to
    read. A command found on $PATH by bare name (`squeue`, `ls`) is not this:
    those are classified by name."""
    if not _PATH_INVOCATION_RE.match(c.raw_cmd):
        return None
    return [c.v(INTERPRETER,
                f"{c.raw_cmd} runs a file whose contents this scan cannot read — "
                + _CONFIRM)]


def launcher(c: Command):
    if c.cmd not in EXEC_LAUNCHERS:
        return None
    return [c.v(INTERPRETER,
                f"{c.cmd} launches an arbitrary process on a compute node — " + _CONFIRM)]


def build_driver(c: Command):
    if c.cmd not in BUILD_DRIVERS:
        return None
    return [c.v(INTERPRETER,
                f"{c.cmd} runs recipe commands this scan cannot see — " + _CONFIRM)]


def network(c: Command):
    if c.cmd not in NETWORK_CMDS:
        return None
    return [c.v(INTERPRETER,
                f"{c.cmd} moves data across the network (fetching code in or "
                "sending results out) — needs --allow-exec and per-use user "
                "confirmation")]


def mv(c: Command):
    """Every mv path, source included, must be in a write root: a move
    changes both ends, and a wrong one is undone by moving back only if
    both ends are the user's. A rename inside the user's own directories is
    routine work; refusing it outright once stranded a /remote-control
    session until the user reached a physical terminal."""
    if c.cmd != "mv":
        return None
    paths = path_args(c.tokens)
    if not paths:
        why = "mv with no path arguments is never sent through this tool"
    else:
        why = next((w for w in (confine.check_path(a, c.ctx, f"mv {a!r}")
                                for a in paths) if w), None)
        why = why or confine.code_refusal("mv", c.tokens, c.ctx)
    return [confined_write(
        why, c.lineno, c.line,
        "mv confined to a write root — needs --allow-write and per-use "
        "user confirmation")]


def chmod(c: Command):
    if c.cmd != "chmod":
        return None
    if "R" in short_flags(c.tokens) or "--recursive" in c.tokens:
        return [c.v(FORBIDDEN, "recursive chmod is never sent")]
    return [confined_write(
        confine.refusal("chmod", c.tokens, c.ctx), c.lineno, c.line,
        "chmod changes remote state")]


# --- git ---------------------------------------------------------------------

#: `git -c KEY=VALUE` keys that only change how output looks. Any other key
#: can name a program git then runs (core.pager, core.fsmonitor, alias.*).
_GIT_SAFE_CONFIG = re.compile(r"^(color\.[A-Za-z.]+|core\.quotepath|advice\.[A-Za-z]+)=", re.I)


def _git_unknown(tokens: list[str], sub: str | None) -> str | None:
    """Why this git command can run a program or write a file whatever its
    subcommand, or None."""
    i = 1
    while i < len(tokens) and tokens[i] != sub:
        tok = tokens[i]
        if tok == "-c":
            value = tokens[i + 1] if i + 1 < len(tokens) else ""
            if not _GIT_SAFE_CONFIG.match(value):
                return (f"git -c {value.split('=', 1)[0]} sets configuration "
                        "that can name a program for git to run")
            i += 2
            continue
        if tok.startswith(("--config-env", "--exec-path=")):
            return f"git {tok.split('=', 1)[0]} changes what git runs"
        i += 1
    if any(t == "--output" or t.startswith("--output=") for t in tokens[i:]):
        return f"git {sub} --output writes a file"
    # `-O` is an orderfile for diff and log; only `git grep -O` runs a pager.
    if any(t == "--ext-diff"
           or (sub == "grep" and (t.startswith("-O") or t.startswith("--open-files-in-pager")))
           for t in tokens[i:]):
        return f"git {sub} in this form runs an external program"
    return None


def _git_reads(sub: str | None, args: list[str]) -> bool:
    """True if ``git <sub> <args>`` only reads: a read subcommand, or a
    dual-mode one in a listing form."""
    if sub is None or sub in GIT_READ:
        return True
    if sub not in GIT_DUAL_MODE:
        return False
    if sub == "stash":
        # Bare `git stash` stashes the working tree: a write. Its reads are
        # the `list` and `show` subcommands.
        return bool(args) and args[0] in ("list", "show")
    query = [t for t in args if t in GIT_READ_ONLY_FLAGS]
    operands = [t for t in args if not t.startswith("-")]
    flags = [t for t in args if t.startswith("-")]
    return not args or (all(f in GIT_READ_ONLY_FLAGS for f in flags)
                        and (not operands or (query and len(operands) == 1)))


#: git writes that can be undone (from the reflog, or by another commit) and
#: so need no ``--allow-write``. Everything else that writes keeps the token:
#: ``push`` (it publishes), ``config`` and ``remote`` (they can make git run a
#: program, or fetch from somewhere else), and anything that can discard
#: uncommitted work or the reflog (``checkout``, ``restore``, ``reset``,
#: ``rebase``, ``gc``, ``reflog``, ``stash drop``, ``branch -D``, ...).
_GIT_UNDOABLE = frozenset({"fetch", "pull", "clone", "add", "commit", "merge"})
#: ``git switch`` flags that throw away local changes or reset a branch.
_SWITCH_DISCARD = frozenset({"-f", "--force", "--discard-changes", "-C", "--force-create"})
#: ``git stash`` subcommands that keep every stash.
_STASH_UNDOABLE = frozenset({"push", "save", "apply", "pop", "list", "show"})


def git_needs_token(sub: str | None, args: list[str]) -> bool:
    """True unless ``git <sub> <args>`` is a write that can be undone."""
    flags = [a for a in args if a.startswith("-")]
    operands = [a for a in args if not a.startswith("-")]
    if sub in _GIT_UNDOABLE:
        return False
    if sub == "switch":
        return any(f in _SWITCH_DISCARD for f in flags)
    if sub == "stash":
        return bool(operands) and operands[0] not in _STASH_UNDOABLE
    if sub in ("branch", "tag"):
        # `git branch NAME` and `git tag NAME` add a ref; any flag may delete,
        # move or force one.
        return bool(flags)
    return True


#: ``git switch`` flags that only steer output or tracking.
_SWITCH_SAFE_FLAGS = frozenset({"-q", "--quiet", "--progress", "--no-progress",
                                "-t", "--track", "--no-track", "--guess", "--no-guess",
                                "-d", "--detach"})
_SWITCH_CREATE_FLAGS = frozenset({"-c", "--create"})


def _repo_sync_refusal(repo, sub: str, args: list[str]) -> str | None:
    """Why a git write in a listed checkout is refused, or None to allow it.

    The one-way rule in code: a ``pull-only`` mirror changes only by
    fetching, fast-forwarding and switching branch; ``two-way`` adds commit,
    add, push and merge. A pinned ``branch`` refuses switching away. History
    rewrites (rebase, amend, refspecs that force) are refused in both.
    Force push, ``reset --hard`` and ``clean`` are refused before this runs.
    """
    two_way = repo.mode == "two-way"
    flags = [a for a in args if a.startswith("-")]
    operands = [a for a in args if not a.startswith("-")]
    where = f"{repo.name} is a {repo.mode} mirror (site [repos])"
    elsewhere = (f"{where}: on the cluster it changes only by git fetch, "
                 "pull --ff-only and switch. Make the change locally, push it, "
                 "and pull it here. Allowing more is the user's setting "
                 "(mode = \"two-way\"), never the agent's")
    if two_way:
        elsewhere = (f"{where}: git {sub} is not a sync operation. Run it in your "
                     "own terminal if it is needed")
    if any(":" in o or o.startswith("+") for o in operands) and sub in ("fetch", "pull", "push"):
        return f"{where}: a refspec (src:dst or +ref) can overwrite a branch; name the remote and branch only"
    if sub == "fetch":
        return None
    if sub == "pull":
        if any(f == "-r" or f.startswith("--rebase") for f in flags):
            return f"{where}: pull --rebase rewrites the checkout's commits; use pull --ff-only"
        if two_way or "--ff-only" in flags:
            return None
        return (f"{where}: pull only with --ff-only, so the checkout never gains a "
                "merge commit of its own. If it is refused as not a fast-forward, "
                "the checkout has diverged: stop and show the user")
    if sub == "checkout":
        return (f"{where}: use `git switch <branch>`. `git checkout` also restores "
                "files, overwriting uncommitted work, and the scan can't tell the two apart")
    if sub == "switch":
        create = [f for f in flags if f in _SWITCH_CREATE_FLAGS]
        bad = [f for f in flags if f not in _SWITCH_SAFE_FLAGS and f not in _SWITCH_CREATE_FLAGS]
        if bad:
            return f"{where}: git switch {bad[0]} can discard work or reset a branch; never sent"
        if create and not two_way:
            return (f"{where}: creating a branch on the cluster is two-way work. "
                    "Create and push it locally, then `git switch <branch>` here")
        if len(operands) != 1 and not (create and len(operands) == 2):
            return f"{where}: git switch takes one branch name"
        if repo.branch and (create or operands[0] != repo.branch):
            return (f"{where} pinned to branch {repo.branch!r}: switching away is "
                    "refused. Pinning is the user's setting")
        return None
    if two_way:
        if sub in ("add", "merge", "push"):
            if sub == "merge" and any(f in ("--squash",) for f in flags):
                return elsewhere
            return None
        if sub == "commit":
            if "--amend" in flags:
                return f"{where}: commit --amend rewrites history; make a new commit"
            return None
    return elsewhere


#: git's own options, which precede the subcommand. Those in the first set take
#: a separate value argument.
_GIT_GLOBAL_VALUE_OPTS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path",
     "--config-env"}
)


def git_subcommand(tokens: list[str]) -> str | None:
    """The real git subcommand, skipping git's global options and their values.

    ``next(t for t in tokens[1:] if not t.startswith("-"))`` returns the VALUE
    of a global option when one is present: ``git -C /repo push --force`` gave
    ``/repo`` and ``git -c x=y reset --hard`` gave ``x=y``. Neither matched
    ``GIT_READ``, so both fell through to the generic write branch and the
    force-push / reset--hard refusals never ran -- a force push, which SKILL.md
    promises is never sent, was reachable with --allow-write.
    """
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("-"):
            return tok
        if tok == "--":
            i += 1
            continue
        if "=" not in tok and tok in _GIT_GLOBAL_VALUE_OPTS:
            i += 2  # the option and its value
        else:
            i += 1
    return None


def git(c: Command):
    """Force pushes, ``reset --hard`` and ``clean`` are never sent; a git
    write lands in a write root, or follows its ``[repos]`` entry."""
    if c.cmd != "git":
        return None
    tokens, ctx = c.tokens, c.ctx
    sub = git_subcommand(tokens)
    rest = tokens[1:]
    args_after_sub = rest[rest.index(sub) + 1:] if sub in rest else []
    if sub == "push" and (
        any(t in ("--force", "-f", "--force-with-lease") for t in rest)
        # `git push origin +main:main` force-updates via the refspec's
        # leading '+', with no --force flag anywhere on the line.
        or any(
            t.startswith("+") and ":" in t and not t.startswith("-")
            for t in args_after_sub
        )
    ):
        return [c.v(FORBIDDEN, "force push is never sent")]
    if sub == "reset" and "--hard" in rest:
        return [c.v(FORBIDDEN, "git reset --hard is never sent")]
    if sub == "clean":
        return [c.v(FORBIDDEN, "git clean deletes files and is never sent")]
    git_why = _git_unknown(tokens, sub)
    if git_why is not None:
        return [unknown_form(c.lineno, c.line, git_why, "git")]
    repo = confine.listed_repo(tokens, sub, ctx)
    if repo is not None and not _git_reads(sub, args_after_sub):
        why = _repo_sync_refusal(repo, sub, args_after_sub)
        if why:
            return [c.v(FORBIDDEN, why)]
        token = git_needs_token(sub, args_after_sub)
        return [Violation(WRITE, c.lineno, c.line,
                          f"git {sub} in {repo.name} ({repo.mode} mirror, site [repos]) — "
                          + _git_token_note(token),
                          repo=repo.name, token=token, sandboxable=False)]
    if sub in GIT_DUAL_MODE:
        # Bare (`git branch`) or an explicit listing flag reads; anything
        # else -- a value to set, a branch to delete -- writes. A read flag
        # may carry one operand (`git config --get user.name` names the key
        # to read, and reads it), so a query flag admits a single
        # non-flag argument; two would be `config key value`, a write.
        if not _git_reads(sub, args_after_sub):
            token = git_needs_token(sub, args_after_sub)
            return [confined_write(
                confine.git_refusal(tokens, sub, ctx), c.lineno, c.line,
                f"git {sub} in this form changes the remote checkout — "
                + _git_token_note(token), token=token, sandboxable=not token)]
        return []
    if sub is not None and sub not in GIT_READ:
        token = git_needs_token(sub, args_after_sub)
        return [confined_write(
            confine.git_refusal(tokens, sub, ctx), c.lineno, c.line,
            f"git {sub} writes to the remote checkout — "
            + _git_token_note(token), token=token, sandboxable=not token)]
    return []


def _git_token_note(token: bool) -> str:
    if token:
        return "needs --allow-write and per-use user confirmation"
    return "no token: git can undo it"


def scontrol(c: Command):
    if c.cmd != "scontrol":
        return None
    sub = next((t for t in c.tokens[1:] if not t.startswith("-")), None)
    if sub not in ("show", None):
        return [c.v(WRITE, f"scontrol {sub} changes scheduler state", command="scontrol", sandboxable=False)]
    return []


def sed(c: Command):
    """`sed -i` edits in place. Unrecognized before, so it reached read tier
    -- and it is precisely the in-place remote edit the skill forbids in
    favour of pull / edit locally / push. A sed program that is not plainly
    a read (`w`, `e`, ...) is unknown."""
    if c.cmd != "sed":
        return None
    in_place = any(
        t == "--in-place" or t.startswith("--in-place=")
        or (t.startswith("-") and not t.startswith("--") and "i" in t.lstrip("-"))
        for t in c.tokens[1:]
    )
    if in_place:
        return [c.v(FORBIDDEN, IN_PLACE.format(cmd="sed -i"))]
    sed_why = sed_unknown(c.tokens)
    if sed_why:
        return [unknown_form(c.lineno, c.line, sed_why, "sed")]
    return []


def find(c: Command):
    """`find -delete/-exec` are never sent; an unbounded find walks the whole
    shared filesystem."""
    if c.cmd != "find":
        return None
    if FIND_RUNS & set(c.tokens):
        return [c.v(FORBIDDEN,
                    "find with -delete/-exec/-execdir/-ok can destroy trees and "
                    "is never sent")]
    predicate = find_unknown(c.tokens)
    if predicate:
        return [unknown_form(
            c.lineno, c.line,
            f"the find predicate {predicate} is not one this guard knows "
            "(-fprint and its kin write a file)", "find")]
    if "-maxdepth" not in c.tokens:
        return [c.v(TRAVERSAL,
                    "find without -maxdepth is an unbounded walk on a shared "
                    "filesystem — add -maxdepth N")]
    return []


def du(c: Command):
    if c.cmd != "du":
        return None
    flags = short_flags(c.tokens)
    bounded = (
        "s" in flags or "d" in flags
        or "--summarize" in c.tokens
        or any(t.startswith("--max-depth") for t in c.tokens)
    )
    if not bounded:
        return [c.v(TRAVERSAL, "du without -s/--max-depth walks the whole tree — bound it")]
    return []


def ls(c: Command):
    if c.cmd != "ls":
        return None
    if "R" in short_flags(c.tokens):
        return [c.v(TRAVERSAL,
                    "ls -R is a recursive walk — list one directory, or use "
                    "find with -maxdepth")]
    return []


def rg_preprocessor(c: Command):
    if c.cmd == "rg" and any(
            t.split("=", 1)[0] in ("--pre", "--pre-glob", "--hostname-bin")
            for t in c.tokens):
        return [unknown_form(c.lineno, c.line, "rg --pre runs a program on each file", "rg")]
    return None


def grep(c: Command):
    if c.cmd not in ("grep", "egrep", "fgrep", "rg", "zgrep"):
        return None
    recursive = "--recursive" in c.tokens or any(c_ in "rR" for c_ in short_flags(c.tokens))
    if recursive:
        return [c.v(TRAVERSAL, "recursive grep walks the tree — name the files explicitly")]
    return []


def editor(c: Command):
    if c.cmd in EDITORS or c.cmd == "patch":
        return [c.v(FORBIDDEN, IN_PLACE.format(cmd=c.cmd))]
    return None


def sbatch_wrap(c: Command):
    if c.cmd == "sbatch" and any(t == "--wrap" or t.startswith("--wrap=") for t in c.tokens):
        return [c.v(INTERPRETER,
                    "sbatch --wrap runs a command string on a compute node that "
                    "this scan cannot read — needs --allow-exec and per-use user "
                    "confirmation")]
    return None


def scancel_all(c: Command):
    """`scancel -u USER` with no job id cancels every job the user has.
    One wrong keystroke ends every running simulation; that is the user's
    to type in their own terminal. Any other scancel falls through to the
    write rule."""
    if c.cmd != "scancel":
        return None
    user_wide = any(t in ("-u", "--user") or t.startswith("--user=") for t in c.tokens)
    has_jobid = any(t[:1].isdigit() for t in c.tokens[1:])
    if user_wide and not has_jobid:
        return [c.v(FORBIDDEN, "scancel of every job at once is never sent; name a job id")]
    return None


def write_command(c: Command):
    """A command that writes (commands.WRITE_CMDS): its targets must be in a
    write root, and forms that delete or run a program are refused."""
    if c.cmd not in WRITE_CMDS:
        return None
    form = write_form_refusal(c.cmd, c.tokens)
    if form:
        kind, why = form
        return [c.v(FORBIDDEN, why) if kind == "forbidden"
                else unknown_form(c.lineno, c.line, why, c.cmd)]
    if confine.writes_nothing(c.cmd, c.tokens):
        return []  # `gzip -c f`: output to stdout; a redirect is checked above
    v = confined_write(
        confine.refusal(c.cmd, c.tokens, c.ctx), c.lineno, c.line,
        f"{c.cmd} changes remote state — needs --allow-write and "
        "per-use user confirmation", command=c.cmd)
    if v.kind == WRITE and c.cmd == "sbatch":
        v = replace(v, in_roots=_sbatch_script_in_roots(c))
    elif v.kind == WRITE and c.cmd in NO_FILE_TARGET:
        # scancel/kill name a job, not a path: the sandbox can't place it.
        v = replace(v, sandboxable=False)
    return [v]


#: File names an sbatch job script is commonly given.
_JOB_SCRIPT_SUFFIXES = (".sh", ".bash", ".csh", ".run", ".slurm", ".job", ".sbatch")


def _sbatch_script_in_roots(c: Command) -> bool:
    """True if every path-like operand of an sbatch (the job script, and any
    other path) lies inside the roots this scan was given. Option values
    (``-t 00:30:00``) are not path-like and are skipped."""
    if c.ctx is None:
        return False
    operands = [t for t in c.tokens[1:] if not t.startswith("-")]
    paths = [t for t in operands if "/" in t or t.endswith(_JOB_SCRIPT_SUFFIXES)]
    return bool(paths) and all(
        confine.check_path(t, c.ctx, "sbatch script") is None for t in paths)


def read_options(c: Command):
    """A known read carrying an option its entry doesn't list is unknown
   . A listed form falls through."""
    if c.cmd in READ_OPTIONS:
        option_why = unknown_option(c.cmd, c.tokens)
        if option_why:
            return [unknown_form(c.lineno, c.line, option_why, c.cmd)]
    return None


def output_by_args(c: Command):
    """Commands that read in one form and write in another (commands.ARG_RULES):
    `ncks -v T in.nc` prints, `ncks -v T in.nc out.nc` writes."""
    if c.cmd not in ARG_RULES:
        return None
    if writes_by_args(c.cmd, c.tokens):
        return [confined_write(
            confine.refusal(c.cmd, c.tokens, c.ctx), c.lineno, c.line,
            f"{c.cmd} in this form writes an output file — needs "
            "--allow-write and per-use user confirmation", command=c.cmd)]
    return []


def fail_closed(c: Command):
    """Every command a rule above claims has been decided; what is left is
    either a known read or a command this scan knows nothing about. The
    second used to pass as a read, which is how `ncks -O`, `cdo` and
    `nccopy` could write with no token. Teaching the guard a command is a
    line in hpctether/commands.py, not a rule here."""
    if c.cmd in READ_COMMANDS:
        return []
    return [c.v(UNKNOWN,
                f"{c.cmd!r} is not a command this guard knows, so it can't tell "
                "whether it writes or runs code — needs profile improvise and "
                "--allow-exec. If it only ever reads, this is a false refusal: "
                "show the user the `hpc-trust check -` output for the payload",
                command=c.cmd)]


#: The rules, in the order they are tried. Order matters: forbidden before a
#: trusted script (no root softens a forbidden command), a trusted script before
#: the interpreter rules (it is the one `python FILE` that is not one), and
#: interpreters before every name-based rule.
RULES = [
    computed_name,
    forbidden_always,
    trusted_script,
    interpreter,
    nested_shell,
    eval_like,
    file_by_path,
    launcher,
    build_driver,
    network,
    mv,
    chmod,
    git,
    scontrol,
    sed,
    find,
    du,
    ls,
    rg_preprocessor,
    grep,
    editor,
    sbatch_wrap,
    scancel_all,
    write_command,
    read_options,
    output_by_args,
    fail_closed,
]


def classify_command(
    segment: str, lineno: int, line: str, ctx,
    trust_index=None, uses: "list | None" = None, cwd: str | None = None,
) -> list[Violation]:
    """Classify one segment: its redirects, then the first rule that claims
    its command. Appends any trusted-script use it allows to ``uses``."""
    out = redirects(segment, lineno, line, ctx)
    tokens = strip_wrappers(tokenize(segment))
    if not tokens:
        return out
    raw_cmd = tokens[0].rstrip(")") or tokens[0]  # `(cd x && pwd)` -> `pwd`
    c = Command(tokens, raw_cmd, raw_cmd.rsplit("/", 1)[-1], lineno, line, ctx,
                trust_index, uses, cwd)
    for rule in RULES:
        decided = rule(c)
        if decided is not None:
            return out + decided
    return out


#: `env` options that change where or what the command runs: `-C DIR`
#: changes directory, `-S STRING` splits a string into the command.
_ENV_REDIRECTS = ("-C", "--chdir", "-S", "--split-string")
#: Wrappers that take the command's arguments from their input at run time.
_LATE_ARGS = frozenset({"xargs", "parallel"})
#: The commands allowed behind one: none has an option that writes a file or
#: runs a program. `echo -o/out f | xargs sort` ran `sort -o/out f`, and the
#: guard saw a bare `sort`. Anything else is unknown.
_LATE_OK = frozenset({
    "grep", "egrep", "fgrep", "wc", "cat", "head", "tail", "ls", "stat",
    "md5sum", "sha256sum", "basename", "dirname", "echo",
})

def check_segment(
    segment: str, lineno: int, line: str, ctx,
    trust_index=None, uses: "list | None" = None, cwd: str | None = None,
) -> list[Violation]:
    """Classify one segment, wrappers included.

    A wrapper is skipped to find the command, but two kinds change what the
    command does, and the rules for the command can't see that:

    * `env -C DIR cmd` runs it in DIR, so a relative write that was placed
      against the tracked `cd` lands somewhere else. `env -S` hides the command in a string. Both are unknown.
    * `xargs cmd` and `parallel cmd` supply arguments at run time, so the
      text may not describe the command that runs (a write's targets, or an
      option such as `sort -o`). Only the commands in `_LATE_OK` are allowed
      behind one; anything else is unknown.
    """
    raw = tokenize(segment)
    prefix = raw[:len(raw) - len(strip_wrappers(raw))]
    if "env" in prefix and any(
            t.startswith(_ENV_REDIRECTS) for t in prefix[prefix.index("env") + 1:]):
        return [unknown_form(
            lineno, line,
            "env -C / -S changes the directory or the command it runs, which "
            "this scan doesn't follow", "env")]
    out = classify_command(segment, lineno, line, ctx, trust_index, uses, cwd)
    late = _LATE_ARGS & set(prefix)
    if late:
        rest = strip_wrappers(raw)
        verb = rest[0].rsplit("/", 1)[-1] if rest else ""
        if verb and verb not in _LATE_OK:
            wrapper = sorted(late)[0]
            out = [v for v in out if v.kind != WRITE]
            if any(v.kind in (FORBIDDEN, TRAVERSAL, INTERPRETER, UNKNOWN) for v in out):
                return out  # already refused for a stronger reason
            out = out + [unknown_form(
                lineno, line,
                f"{wrapper} supplies {verb}'s arguments at run time, so the "
                "text may not show what it does or writes; behind "
                f"{wrapper} only {', '.join(sorted(_LATE_OK))} are known", wrapper)]
    return out
