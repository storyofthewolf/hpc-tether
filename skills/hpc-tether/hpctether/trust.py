"""Pre-built scripts: Python files under the site's declared script roots.

The guard's strictness belongs where it can read: hand-rolled shell. A Python
file that already lives on the cluster is opaque to it either way, and
per-file hash pins plus per-argument patterns did not make running one any
safer -- they only made the outcome depend on how a command happened to be
spelled (``check`` with no case, ``verify``, ``status``, a
``cd ~/...`` and every repo but one all fell back to ``--allow-exec``).

So the grant is now a directory: a repo the site file marks trusted::

    [repos.my-tools]
    remote  = "/home/you/code/my-tools"
    trusted = true
    confirm = ["* --execute*"]   # optional: these still need --allow-write

Any existing ``.py`` file under a trusted repo (a script root), run as
``python [flags] <file>.py ARGS`` or directly as ``<file>.py ARGS``, is read
tier with no token -- whatever the arguments. That repo's ``confirm``
patterns (and the older site-wide ``script_confirm``) are matched against
``<path relative to its root> <args>`` (``*my-script.py *--execute*``) and put a
match back at write tier.

What stays strict, because it is exactly what the guard can read:

* inline code -- ``python -c``, ``python -m``, ``python - <<EOF``, a nested
  ``bash -c`` -- is interpreter tier, as is a ``.py`` outside every root;
* a path the scan cannot name with certainty -- ``~user``, ``~`` with no
  ``home`` role or in a payload that assigns ``HOME``, a shell variable other
  than a site role, a glob, any ``..``, a relative path not directly after
  ``cd <dir> &&`` -- is not resolved, so it stays interpreter tier;
* writing a file into a root (redirection, ``cp``, ``git pull``, ``hpc-push``)
  is write tier, so a freshly written script cannot become "pre-built"
  without the user approving the write;
* nothing here softens a forbidden-tier construct anywhere in the payload.

What a root does NOT mean: the guard still cannot see inside the script. A
script under a root that deletes will delete. The roots are the user's
statement that they wrote and maintain that code.
"""

from __future__ import annotations

import fnmatch
import posixpath
import re
from dataclasses import dataclass

#: Interpreter verbs that run a pre-built script: ``python``, ``python3``,
#: ``python3.13``. The site's modules decide which name resolves to which
#: binary; the file is what was declared, not the spelling of the verb.
_PYTHON_RE = re.compile(r"^python(3(\.\d+)?)?$")

#: Interpreter flags that take the program from somewhere other than the next
#: path argument. After one of these, a script path is data, not the program.
_DISPLACING_SHORT = frozenset({"c", "m"})
_DISPLACING_LONG = frozenset({"--command", "--module", "--eval", "--exec"})


@dataclass(frozen=True)
class Roots:
    """The site's script roots, its confirm patterns, and its path roles --
    everything needed to decide whether one invocation is a pre-built script."""

    roots: tuple[str, ...] = ()
    confirm: tuple[str, ...] = ()
    #: Patterns that apply to one root only: ``((root, patterns), ...)``,
    #: from each trusted ``[repos.<name>]`` entry's ``confirm``.
    scoped: tuple[tuple[str, tuple[str, ...]], ...] = ()
    paths: tuple[tuple[str, str], ...] = ()
    #: False when the payload assigns HOME: tilde expansion reads $HOME, so
    #: the scan can no longer say what `~` means (see guard.classify).
    tilde: bool = True

    @property
    def home(self) -> str | None:
        """What `~` expands to: the site's ``home`` path role, if any."""
        return dict(self.paths).get("home") if self.tilde else None

    def __bool__(self) -> bool:
        return bool(self.roots)

    @classmethod
    def for_site(cls, site) -> "Roots":
        return cls(
            roots=tuple(site.script_roots),
            confirm=tuple(site.script_confirm),
            scoped=tuple(sorted((getattr(site, "root_confirm", None) or {}).items())),
            paths=tuple(sorted((site.paths or {}).items())),
        )

    def root_of(self, path: str) -> str | None:
        """The declared root containing absolute ``path``, or None."""
        for root in self.roots:
            if path.startswith(root.rstrip("/") + "/"):
                return root
        return None


@dataclass(frozen=True)
class Use:
    """One pre-built script invocation the guard allowed."""

    path: str
    root: str
    args: str
    tier: str  # "free" | "write"


def expand_tilde(token: str, home: str | None) -> str:
    """``~`` or ``~/x`` -> ``<home>/x`` when the site declares a ``home``
    role. ``~user`` and every other token are returned unchanged."""
    if home and (token == "~" or token.startswith("~/")):
        return home.rstrip("/") + token[1:]
    return token


def _expand_role(token: str, paths) -> str:
    """``$HPC_REPO/x.py`` -> ``/abs/repo/x.py`` for a known site role; any
    other token is returned unchanged (and then refused if it still has a
    ``$``)."""
    for role, root in paths:
        var = f"HPC_{role.upper()}"
        for prefix in (f"${{{var}}}", f"${var}"):
            if token == prefix or token.startswith(prefix + "/"):
                return root.rstrip("/") + token[len(prefix):]
    return token


def resolve(token: str, roots: Roots, cwd: str | None) -> tuple[str, str] | None:
    """``(absolute_path, root)`` if ``token`` names a ``.py`` file under a
    declared root, else None.

    The path must be one this scan can name for certain: absolute, a site
    role, ``~/`` when the site declares a ``home`` role, or relative directly
    after ``cd <dir> &&`` (``cwd``). ``~user``, any other variable, a glob, or
    a ``..`` segment is never resolved, because the guard runs locally on
    unexpanded text and must not guess.
    """
    if not token or ".." in token.split("/"):
        return None
    path = _expand_role(expand_tilde(token, roots.home), roots.paths)
    if "$" in path or "~" in path or "\\" in path or any(c in path for c in "*?["):
        return None
    if not path.startswith("/"):
        if cwd is None:
            return None
        path = posixpath.join(cwd, path)
    path = posixpath.normpath(path)
    if not path.endswith(".py"):
        return None
    root = roots.root_of(path)
    return (path, root) if root else None


def _tier(path: str, root: str, args: str, roots: "Roots") -> str:
    rel = path[len(root.rstrip("/")) + 1:]
    subject = f"{rel} {args}".rstrip()
    patterns = tuple(roots.confirm) + dict(roots.scoped).get(root, ())
    if any(fnmatch.fnmatchcase(subject, p) for p in patterns):
        return "write"
    return "free"


def _use(path: str, root: str, rest: list[str], roots: "Roots") -> Use:
    args = " ".join(rest)
    return Use(path, root, args, _tier(path, root, args, roots))


def match_invocation(tokens: list[str], roots: Roots, cwd: str | None = None) -> Use | None:
    """The :class:`Use` if ``tokens`` run a pre-built script, else None.

    Two shapes, and only these two:

    * ``python[3[.N]] [flags] <script>.py ARGS...``
    * ``<script>.py ARGS...`` (direct execution; a bare name with no ``/`` is
      found on ``PATH``, not in a root, so it never matches)

    ``ARGS`` is the remaining tokens joined with single spaces.
    """
    if not tokens or not roots:
        return None
    raw_cmd = tokens[0]

    if "/" in raw_cmd:
        found = resolve(raw_cmd, roots, cwd)
        if found:
            path, root = found
            return _use(path, root, tokens[1:], roots)

    if not _PYTHON_RE.match(raw_cmd.rsplit("/", 1)[-1]):
        return None
    for i, token in enumerate(tokens[1:], start=1):
        if token.startswith("-"):
            # A flag that carries code or names a module means the path after
            # it is an argument, not the program: `python3 -cCODE x.py` runs
            # CODE. Only harmless flags are stepped over.
            if _displaces_script(token):
                return None
            continue
        found = resolve(token, roots, cwd)
        if not found:
            return None
        path, root = found
        return _use(path, root, tokens[i + 1:], roots)
    return None


def _displaces_script(token: str) -> bool:
    """True if this interpreter flag means the script path is not what runs.

    ``-c``/``-m`` take the program from the command line or a module name;
    both the separated and attached spellings count, and so does a bundle such
    as ``-uc``. ``--`` ends flag parsing, so the next token really is the
    script -- that one is safe. A bare ``-`` reads the program from stdin.
    """
    if token == "--":
        return False
    if token == "-":
        return True
    if token.startswith("--"):
        return token.split("=", 1)[0] in _DISPLACING_LONG
    return any(ch in _DISPLACING_SHORT for ch in token[1:])
