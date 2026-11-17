"""Write confinement: every write the guard can see lands in a write root.

Part of the floor (plugin-generalization-plan.md §4.1), so it holds at every
profile level. The write roots are the site's scratch role plus its
``writable_roots``. A write whose target this scan cannot place -- a shell
variable other than a site role, a relative path in an unknown directory, a
``..`` that escapes -- is refused: unknown is reported as unknown.

The scan is textual. It runs locally on payload text and cannot stat the
cluster, so a directory reached by two spellings (``/home/u`` and
``/gpfs/home/u``) is the same directory only if the site's ``[aliases]``
says so. This binds only what the guard can read: a trusted script, a job
script, or inline code writes wherever it writes.
"""

from __future__ import annotations

import dataclasses
import posixpath
import re
from dataclasses import dataclass

from hpctether.commands import (
    CODE_SUFFIXES, NO_CODE_WRITE, NO_FILE_TARGET, UNPACKERS, WRITE_TARGETS, TargetRule,
    split_by_flags,
)

#: A chmod mode that starts with a dash (`chmod -w f`) is the mode, not a flag.
_CHMOD_MODE = re.compile(r"-[rwxXstugo]+")


def _split(cmd: str, tokens: list[str], rule: TargetRule):
    """``(options, operands)`` for a write command. Attached values are read
    like spaced ones: ``cp -t/outside a b`` was taken to write ``b``
   ."""
    if cmd == "chmod":
        modes = [t for t in tokens[1:] if _CHMOD_MODE.fullmatch(t)]
        tokens = [t for t in tokens if not _CHMOD_MODE.fullmatch(t)]
        opts, operands = split_by_flags(tokens, rule.value_flags | rule.target_flags)
        return opts, modes + operands
    return split_by_flags(tokens, rule.value_flags | rule.target_flags)

_GLOB = "*?["


@dataclass(frozen=True)
class Context:
    """Everything needed to place a path, for one segment of a payload.

    ``roots`` must already be canonical (see :func:`canonical`). ``roots`` of
    ``None`` means no site context: nothing is confined, so every write is
    refused rather than waved through.
    """

    roots: tuple[str, ...] | None
    paths: dict[str, str]
    home: str | None
    aliases: tuple[tuple[str, str], ...] = ()
    cwd: str | None = None
    #: The site's script roots, canonical. Active or not at this profile,
    #: no code file may be written into one (see :func:`code_refusal`).
    script_roots: tuple[str, ...] = ()
    #: The site's ``[repos]``, as ``(canonical remote, Repo)`` pairs.
    repos: tuple = ()
    #: ``[policy] protected``, canonical: never written, even inside a root.
    protected: tuple[str, ...] = ()


def canonical(path: str, aliases) -> str:
    """Rewrite ``path`` through the longest matching alias prefix."""
    for alias, real in aliases:  # sorted longest first by the caller
        if path == alias or path.startswith(alias + "/"):
            return real + path[len(alias):]
    return path


def sort_aliases(aliases: dict[str, str]) -> tuple[tuple[str, str], ...]:
    pairs = [(posixpath.normpath(a), posixpath.normpath(r)) for a, r in aliases.items()]
    return tuple(sorted(pairs, key=lambda p: -len(p[0])))


def resolve(arg: str, ctx: Context) -> str | None:
    """The absolute, normalized, canonical path ``arg`` names, or None.

    None means this scan cannot say where the write lands.
    """
    arg = arg.strip("\"'")
    if not arg:
        return None
    for role, root in (ctx.paths or {}).items():
        var = f"HPC_{role.upper()}"
        for prefix in (f"${{{var}}}", f"${var}"):
            if arg == prefix or arg.startswith(prefix + "/"):
                arg = root.rstrip("/") + arg[len(prefix):]
                break
        else:
            continue
        break
    if ctx.home and (arg == "~" or arg.startswith("~/")):
        arg = ctx.home.rstrip("/") + arg[1:]
    if "$" in arg or "`" in arg or arg.startswith("~"):
        return None
    if not arg.startswith("/"):
        if ctx.cwd is None:
            return None
        arg = ctx.cwd.rstrip("/") + "/" + arg
    # A glob stays inside the directory its fixed part names, provided no
    # `..` follows it: check that directory instead of the pattern.
    cut = min((arg.index(c) for c in _GLOB if c in arg), default=None)
    if cut is not None:
        if ".." in arg[cut:].split("/"):
            return None
        arg = posixpath.dirname(arg[:cut]) or "/"
    resolved = posixpath.normpath(arg)
    if ".." in resolved.split("/"):
        return None
    return canonical(resolved, ctx.aliases)


def confined(resolved: str, roots) -> bool:
    return any(resolved == r or resolved.startswith(r.rstrip("/") + "/")
               for r in roots or ())


def display_roots(roots) -> list[str]:
    """``roots`` for a refusal message: a root inside another listed root is
    dropped (the outer one already covers it). Display only; the guard's own
    test is :func:`confined` over the full list."""
    roots = list(roots or ())
    return [r for r in roots
            if not any(o != r and confined(r, [o]) for o in roots)]


def targets(cmd: str, tokens: list[str]) -> tuple[list[str], bool]:
    """``(target arguments, writes into the working directory)`` for a write."""
    if cmd in NO_FILE_TARGET:
        return [], False
    rule = WRITE_TARGETS.get(cmd, TargetRule())
    opts, operands = _split(cmd, tokens, rule)
    if any(name in rule.stdout_flags for name, _ in opts):
        return [], False
    flagged = [value for name, value in opts
               if name in rule.target_flags and value is not None]
    if flagged:
        return flagged, False
    pick = {
        "all": operands,
        "last": [] if rule.single_is_cwd and len(operands) < 2 else operands[-1:],
        "first": operands[:1],
        "all_but_first": operands[1:],
        "none": [],
    }[rule.operands]
    cwd = rule.cwd or (rule.single_is_cwd and len(operands) == 1)
    return pick, cwd


def writes_nothing(cmd: str, tokens: list[str]) -> bool:
    """True if a stdout flag sends all output to stdout (``gzip -c``)."""
    rule = WRITE_TARGETS.get(cmd)
    if not (rule and rule.stdout_flags):
        return False
    opts, _ = _split(cmd, tokens, rule)
    return any(name in rule.stdout_flags for name, _ in opts)


def _link_refusal(tokens: list[str], ctx: Context) -> str | None:
    """Why this ``ln`` is never sent, or None.

    A link is a second name for its source, so a later write through the link
    lands on the source: `ln -s /outside link && touch link/x` wrote outside
    every root with each command confined. The
    source must be in a write root too. A link that already exists is not
    seen from here; that limit is stated in reference/design.md.
    """
    rule = WRITE_TARGETS["ln"]
    opts, operands = _split("ln", tokens, rule)
    flagged = any(name in rule.target_flags for name, _ in opts)
    sources = operands if flagged or len(operands) < 2 else operands[:-1]
    symbolic = any(name in ("-s", "--symbolic") for name, _ in opts)
    for src in sources:
        if symbolic and resolve(src, dataclasses.replace(ctx, cwd=None)) is None \
                and not src.startswith(("$", "~")):
            return (f"ln -s {src!r}: a relative link target is read from the "
                    "link's own directory, which this scan doesn't follow. "
                    "Give the target as an absolute or $HPC_ROLE path")
        why = check_path(src, ctx, f"ln links to {src!r}")
        if why:
            return (why + ". (A link to a file outside the write roots would "
                    "let a later write through it land there.)")
    return None


def refusal(cmd: str, tokens: list[str], ctx: Context) -> str | None:
    """Why this write is never sent, or None if it may be.

    Every target must be confined, and none may put code in a script root.
    """
    why = _location_refusal(cmd, tokens, ctx)
    return why or code_refusal(cmd, tokens, ctx)


def _location_refusal(cmd: str, tokens: list[str], ctx: Context) -> str | None:
    args, cwd = targets(cmd, tokens)
    for arg in args:
        why = check_path(arg, ctx, f"{cmd} writes {arg!r}")
        if why:
            return why
    if cmd == "ln":
        why = _link_refusal(tokens, ctx)
        if why:
            return why
    if cwd:
        if ctx.cwd is None:
            return (f"{cmd} writes into the working directory, which this scan "
                    "can't place. Put `cd <absolute or $HPC_ROLE path> &&` "
                    "directly before it on the same line, or give it an "
                    "explicit absolute output path")
        why = check_path(ctx.cwd, ctx, f"{cmd} writes into {ctx.cwd!r}")
        if why:
            return why
    return None


def check_path(arg: str, ctx: Context, what: str) -> str | None:
    """None if ``arg`` resolves inside a write root, else the refusal text."""
    root_list = ", ".join(display_roots(ctx.roots)) or "(no site context: none)"
    resolved = resolve(arg, ctx)
    if resolved is not None and confined(resolved, ctx.roots):
        guarded = next((p for p in ctx.protected
                        if resolved == p or resolved.startswith(p.rstrip("/") + "/")), None)
        if guarded is None:
            return None
        return (f"{what}: {resolved} is inside {guarded}, which the site file "
                "protects: never written, at any profile. Print the command "
                "and let the user run it by hand if it is really meant")
    if resolved is None:
        bare = arg.strip("\"'")
        if "$" in bare or "`" in bare or bare.startswith("~"):
            cause = ("uses a shell variable or ~ this scan can't expand (only "
                     "site roles like $HPC_SCRATCH are expanded)")
        elif ".." in bare.split("/"):
            cause = "uses '..' in a way this scan can't confirm stays inside a root"
        else:
            cause = ("is relative, and the directory it's relative to isn't "
                     "known (only `cd <dir> &&` directly before it on the same "
                     "line, or a payload with no cd at all, is tracked)")
        return (f"{what}: the path {cause}, so the guard can't confirm it is "
                f"inside a write root ({root_list}). It may well be: rewrite it "
                "as a literal absolute path or a $HPC_ROLE path and retry. Do "
                "not ask the user to change writable_roots for this")
    return (f"{what}: {resolved} is outside the write roots ({root_list}). This "
            "toolkit never writes outside them, at any profile. Print the "
            "command and let the user run it by hand")


#: git options that take a value and precede the subcommand.
_GIT_VALUE_OPTS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})


def _git_location(tokens: list[str], sub: str | None, ctx: Context):
    """Where a git command operates: ``(context with cwd set to the
    repository, --git-dir/--work-tree paths, index of the subcommand, refusal)``.
    The repository is ``-C DIR`` or the working directory."""
    repo = None
    extra: list[str] = []
    i = 1
    while i < len(tokens) and tokens[i] != sub:
        tok = tokens[i]
        name, eq, value = tok.partition("=")
        if name in _GIT_VALUE_OPTS:
            val = value if eq else (tokens[i + 1] if i + 1 < len(tokens) else "")
            if name == "-C":
                repo = val if repo is None else posixpath.join(repo, val)
            elif name in ("--git-dir", "--work-tree"):
                extra.append(val)
            i += 1 if eq else 2
            continue
        i += 1
    base = ctx
    if repo is not None:
        resolved = resolve(repo, ctx)
        if resolved is None:
            return ctx, extra, i, check_path(repo, ctx, f"git -C {repo!r}")
        base = dataclasses.replace(ctx, cwd=resolved)
    return base, extra, i, None


def listed_repo(tokens: list[str], sub: str | None, ctx: Context):
    """The site ``[repos]`` entry this git command operates in, or None.

    Only a repository the scan can place for certain counts: ``-C`` or a
    known working directory, no ``--git-dir``/``--work-tree``. Anything else
    falls back to the ordinary write-root rules.
    """
    if not ctx.repos:
        return None
    base, extra, _, why = _git_location(tokens, sub, ctx)
    if why or extra or base.cwd is None:
        return None
    where = canonical(posixpath.normpath(base.cwd), ctx.aliases)
    for remote, repo in ctx.repos:
        if where == remote or where.startswith(remote + "/"):
            return repo
    return None


def git_refusal(tokens: list[str], sub: str | None, ctx: Context) -> str | None:
    """Why a git write is never sent, or None if its repository is confined.

    The repository is ``-C DIR`` or the working directory; ``--git-dir`` and
    ``--work-tree`` must be confined too. ``git clone URL DIR`` writes DIR
    (or a new directory in the working directory).
    """
    base, extra, i, why = _git_location(tokens, sub, ctx)
    if why:
        return why
    for path in extra:
        why = check_path(path, base, f"git writes {path!r}")
        if why:
            return why
    if sub == "clone":
        rest = [t for t in tokens[i + 1:] if not t.startswith("-")]
        if len(rest) >= 2:
            return check_path(rest[-1], base, f"git clone into {rest[-1]!r}")
    if base.cwd is None:
        return ("git writes to the repository in the working directory, which "
                "this scan can't place. Use `git -C <absolute or $HPC_ROLE "
                "path> ...` or `cd <dir> && git ...` on one line")
    return check_path(base.cwd, base, f"git writes the repository at {base.cwd!r}")


def _is_code(name: str) -> bool:
    """True for a code extension, or no extension at all: an extensionless
    name may be a directory or an executable, and is not trusted to be data."""
    base = posixpath.basename(name.rstrip("/").strip("\"'"))
    suffix = posixpath.splitext(base)[1].lower()
    return suffix == "" or suffix in CODE_SUFFIXES


def _has_code_suffix(name: str) -> bool:
    base = posixpath.basename(name.rstrip("/").strip("\"'"))
    return posixpath.splitext(base)[1].lower() in CODE_SUFFIXES


def _in_script_root(arg: str, ctx: Context) -> bool:
    resolved = resolve(arg, ctx)
    return resolved is not None and confined(resolved, ctx.script_roots)


def code_refusal(cmd: str, tokens: list[str], ctx: Context) -> str | None:
    """Why this write would put code into a script root, or None.

    Floor, every profile. Checks the written file and, for a copy, move or
    link, every source too: ``cp a.py $ROOT/v2.0`` writes ``v2.0/a.py`` if
    ``v2.0`` is a directory, which the text can't tell. Data files with a
    non-code extension pass (experiment matrices are pushed, not committed).
    Git is not checked here: git is how code is meant to arrive.
    """
    if not ctx.script_roots or cmd in NO_FILE_TARGET or cmd in NO_CODE_WRITE:
        return None
    args, cwd = targets(cmd, tokens)
    root_targets = [a for a in args if _in_script_root(a, ctx)]
    cwd_in_root = cwd and ctx.cwd is not None and confined(
        canonical(posixpath.normpath(ctx.cwd), ctx.aliases), ctx.script_roots)
    if not root_targets and not cwd_in_root:
        return None
    where = root_targets[0] if root_targets else ctx.cwd
    if cmd in UNPACKERS or cwd_in_root:
        return _code_message(f"{cmd} unpacks files into {where!r}, inside a script root")
    rule = WRITE_TARGETS.get(cmd)
    if rule is not None and rule.operands in ("last", "first"):
        # A copy, move or link: the content and often the name come from the
        # sources. Sources must be data (a code or extensionless source may be
        # a script or a directory of them); the target must not be named as
        # code, and may be a directory.
        sources = [o for o in _operands(tokens, rule) if o not in root_targets]
        bad = next((n for n in sources if _is_code(n)), None)
        if bad is None:
            bad = next((n for n in root_targets if _has_code_suffix(n)), None)
    else:
        # The command creates the content: the written name must be data.
        bad = next((n for n in root_targets if _is_code(n)), None)
    if bad is None:
        return None
    return _code_message(f"{cmd} would put {bad!r} into a script root")


def redirect_code_refusal(target: str, ctx: Context) -> str | None:
    if ctx.script_roots and _in_script_root(target, ctx) and _is_code(target):
        return _code_message(f"redirect would write {target!r} into a script root")
    return None


def _operands(tokens: list[str], rule: TargetRule) -> list[str]:
    return split_by_flags(tokens, rule.value_flags | rule.target_flags)[1]


def _code_message(what: str) -> str:
    return (f"{what}. A file there runs as trusted code, so code reaches a script "
            "root only through git: commit it, push, and `git pull` the "
            "checkout. Data files (.csv, .toml, .json, .nc, ...) can be written "
            "there; a file with no extension is not trusted to be data. Never "
            "sent, at any profile")


def site_context(site) -> Context:
    """The confinement context for a loaded site, for a write named by an
    absolute path (no working directory). ``hpc-push`` uses this."""
    aliases = sort_aliases(site.aliases)
    canon = lambda r: canonical(posixpath.normpath(r), aliases)  # noqa: E731
    return Context(
        roots=tuple(canon(r) for r in site.guard_roots()),
        paths=dict(site.paths),
        home=site.paths.get("home"),
        aliases=aliases,
        script_roots=tuple(canon(r) for r in site.script_roots),
        repos=repo_rules(site.repos, aliases),
        protected=tuple(canon(r) for r in getattr(site, "protected", ())),
    )


def repo_rules(repos, aliases) -> tuple:
    """``(canonical remote, Repo)`` pairs for :class:`Context`. Only a repo
    with a ``local`` side is a mirror whose sync operations are allowed; one
    with no local checkout follows the ordinary write rules."""
    return tuple((canonical(posixpath.normpath(r.remote), aliases), r)
                 for r in (repos or {}).values() if r.local)


def push_refusal(remote_path: str, ctx: Context) -> str | None:
    """Why a push to ``remote_path`` is never sent, or None.

    The same floor as hpc-run: the destination must be in a write root, and
    a code file may not land in a script root.
    """
    why = check_path(remote_path, ctx, f"push to {remote_path!r}")
    if why:
        return why
    if ctx.script_roots and _in_script_root(remote_path, ctx) and _is_code(remote_path):
        return _code_message(f"push would put {remote_path!r} into a script root")
    return None
