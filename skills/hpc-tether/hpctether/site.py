"""Site definitions: load ``~/.config/hpc-tether/sites/*.toml`` and resolve path roles.

A site file describes *which* machine to talk to and *what paths mean* on it.
It deliberately does NOT describe how to connect: user, port, ProxyCommand,
and identity all stay in ``~/.ssh/config``, which remains authoritative. The
``host`` key here is only a key into that config.

Every key under ``[paths]`` is arbitrary and becomes ``HPC_<KEY_UPPER>`` in the
remote environment assembled by ``transport.build_payload``.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from hpctether import profile as profile_mod

#: Schedulers this toolkit can actually parse. SLURM only, by design --
#: see ``dispatch_unsupported`` for the extension point.
SUPPORTED_SCHEDULERS = frozenset({"slurm"})

#: Path-role keys must be safe to turn into shell environment variable names.
_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


#: How a mirrored checkout may change on the cluster (see :class:`Repo`).
REPO_MODES = ("pull-only", "two-way")
_REPO_KEYS = frozenset({"local", "remote", "branch", "mode", "trusted", "role", "confirm",
                        "preview"})
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class SiteError(Exception):
    """Unknown site, malformed site file, or unsupported scheduler.

    Callers map this to exit code 64 (usage error).
    """


@dataclass(frozen=True)
class Repo:
    """One repository mirrored local -> GitHub -> cluster (``[repos]``).

    The user writes these; each one grants git writes in a cluster checkout,
    so the agent never adds one. On a listed checkout the guard allows the
    sync operations of its ``mode`` and nothing else, whether or not the
    checkout is in a write root -- and listing it does not make it one.

    ``mode``: ``pull-only`` (fetch, ``pull --ff-only``, ``switch``) or
    ``two-way`` (also commit, push, merge). ``branch``, when set, pins the
    checkout: switching to any other branch is refused.
    """

    name: str
    local: str | None
    remote: str
    branch: str | None = None
    mode: str = "pull-only"
    #: Its ``.py`` files run with no token (it is one of the script roots).
    trusted: bool = False
    #: A path role naming this checkout (``repo`` -> ``$HPC_REPO``), or None.
    role: str | None = None
    #: fnmatch patterns over ``<path in repo> <args>`` that still need
    #: --allow-write; they apply to this repo's scripts only.
    confirm: tuple[str, ...] = ()
    #: Whether the agent previews a ``confirm`` match (runs it without the
    #: action flag and shows the result) before sending it. Advisory: the
    #: skill reads it from ``hpc-trust list``; the guard does not enforce it.
    preview: bool = True


@dataclass(frozen=True)
class Site:
    """A resolved site definition."""

    name: str
    host: str
    scheduler: str
    user: str
    paths: dict[str, str] = field(default_factory=dict)
    writable_roots: list[str] = field(default_factory=list)
    script_roots: list[str] = field(default_factory=list)
    script_confirm: list[str] = field(default_factory=list)
    source: Path | None = None
    profile: str = profile_mod.DEFAULT
    #: Alternate spellings of the same directory: ``{"/home/u": "/gpfs/home/u"}``.
    #: Write confinement compares text, so without this a write spelled one
    #: way is refused when the root is spelled the other.
    aliases: dict[str, str] = field(default_factory=dict)
    #: Most variable data one hpc-nc call may read, in MB. A site setting, not
    #: a flag, so the agent can't raise it: past it, the work belongs in a
    #: trusted script.
    nc_max_read_mb: int = 100
    #: Repositories mirrored local -> GitHub -> cluster, by name.
    repos: dict[str, Repo] = field(default_factory=dict)
    #: Directories never written, even inside a write root (``[policy]
    #: protected``): a shared model tree that must stay static.
    protected: list[str] = field(default_factory=list)
    #: ``script_confirm`` patterns scoped to one script root: root -> patterns.
    #: The top-level ``script_confirm`` list applies to every root.
    root_confirm: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: Notes for the user about the file itself (old key names); printed by
    #: hpc-check. Never affects what is allowed.
    warnings: tuple[str, ...] = ()
    #: ``[policy] sandbox_roots``: directories, inside the write roots, where
    #: what the profile allows runs with no token (design.md, "The sandbox").
    #: A single one is also the path role ``sandbox`` ($HPC_SANDBOX).
    sandbox_roots: list[str] = field(default_factory=list)

    def allows(self, level: str) -> bool:
        """True if this site's profile permits what ``level`` requires."""
        return profile_mod.allows(self.profile, level)

    def env(self) -> dict[str, str]:
        """Return the ``HPC_*`` environment mapping for this site's paths."""
        return {f"HPC_{key.upper()}": value for key, value in self.paths.items()}

    def guard_roots(self) -> list[str]:
        """Roots where ``mv`` is write-tier rather than forbidden.

        The scratch role is always writable; ``writable_roots`` adds
        directories the user owns outright, where a rename is ordinary work.
        Everything else on the cluster stays forbidden. Nothing here widens
        ``rm``: deletion is forbidden everywhere, by the transport.
        """
        roots = list(self.writable_roots)
        scratch = self.paths.get("scratch")
        if scratch and scratch not in roots:
            roots.append(scratch)
        return roots

    def remote_path(self, raw: str) -> str:
        """A tool's PATH argument as a remote path: absolute, or
        ``role:relative`` against ``[paths]``.

        The result always starts with ``/``. The fixed tools' remote scripts
        pass it to ``find``, ``grep`` and ``stat``, and a leading ``/`` is
        what keeps it from being read as an option there (``find`` takes no
        ``--`` before its starting points).
        """
        if raw.startswith("$"):
            # A `$ROLE/...` spelling never expands: the path is shell-quoted
            # into the payload, so it always read as "file missing".
            raise SiteError(f"path {raw!r} would not expand; spell it as role:relative/path")
        if raw.startswith("/"):
            return raw
        if ":" in raw:
            role, _, rest = raw.partition(":")
            base = self.path(role)
            if not base.startswith("/"):
                raise SiteError(f"path role {role!r} is {base!r}, not an absolute path")
            return base.rstrip("/") + "/" + rest
        raise SiteError(
            f"path {raw!r} is neither absolute nor role:relative "
            f"(known roles: {', '.join(sorted(self.paths)) or '(none)'})"
        )

    def path(self, role: str) -> str:
        """Resolve a path role (e.g. ``scratch``) to an absolute remote path."""
        try:
            return self.paths[role]
        except KeyError:
            known = ", ".join(sorted(self.paths)) or "(none defined)"
            raise SiteError(
                f"site {self.name!r} defines no path role {role!r}; known roles: {known}"
            ) from None


def sites_dir() -> Path:
    """Directory holding the user's site TOML files.

    ``~/.config/hpc-tether/sites/`` by default, outside the package: an
    installed plugin is replaced wholesale on update, so a site file kept
    inside it would be lost. The package's own ``sites/`` holds templates
    only (see ``templates_dir``). ``HPC_TETHER_SITES`` overrides the
    location, for tests and relocation.
    """
    override = os.environ.get("HPC_TETHER_SITES")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "hpc-tether" / "sites"


def templates_dir() -> Path:
    """The package's template site files (``example.toml``, ``<site>.example.toml``); never loaded."""
    return Path(__file__).resolve().parent.parent / "sites"


def _no_site_hint() -> str:
    """Where to put a site file, for errors raised when none is found."""
    return (
        f"Site files live in {sites_dir()}; copy a template from "
        f"{templates_dir()} there and edit it (see reference/setup.md)."
    )


def available() -> list[str]:
    """Names of all site files present, sorted.

    ``<name>.trust.toml`` was a site's per-file trust list (an older format,
    replaced by ``script_roots``), not a site. A stale one left behind must
    not be offered as a site name or make ``default_site_name`` see two names
    where the user has one site.
    """
    directory = sites_dir()
    if not directory.is_dir():
        return []
    return sorted(
        p.stem for p in directory.glob("*.toml") if not p.name.endswith(".trust.toml")
    )


def default_site_name() -> str | None:
    """The site to use when ``--site`` is omitted.

    ``HPC_TETHER_SITE`` wins; otherwise, if exactly one site file exists, use
    it. Ambiguity is an error rather than a guess -- picking the wrong cluster
    silently is worse than asking.
    """
    override = os.environ.get("HPC_TETHER_SITE")
    if override:
        return override
    names = available()
    if len(names) == 1:
        return names[0]
    return None


def _validate_root(root: str, label: str, path: Path) -> None:
    """Reject a root too broad or too vague to confine a destructive command.

    Shared by the scratch role and every ``writable_roots`` entry: both widen
    what the guard will send, so both must be absolute, free of ``..``, and at
    least two levels deep. A shallow or relative root silently disarms path
    confinement for the whole filesystem, which is the one thing these values
    must never do.
    """
    if not root.startswith("/") or ".." in root.split("/"):
        raise SiteError(
            f"{path}: {label} {root!r} must be an absolute path with no "
            "'..' segment"
        )
    if len([s for s in root.strip("/").split("/") if s]) < 2:
        raise SiteError(
            f"{path}: {label} {root!r} is too broad; name a specific "
            "user-owned directory, at least two levels deep"
        )


def load(name: str | None = None) -> Site:
    """Load a site by name, or the default site when ``name`` is None.

    Raises:
        SiteError: unknown site, ambiguous default, malformed file, or a
            scheduler this toolkit cannot parse.
    """
    if name is None:
        name = default_site_name()
        if name is None:
            if not available():
                raise SiteError(f"no site files found. {_no_site_hint()}")
            names = ", ".join(available())
            raise SiteError(
                "no site specified and no unambiguous default; "
                f"pass --site NAME or set HPC_TETHER_SITE. Available: {names}"
            )

    path = sites_dir() / f"{name}.toml"
    if not path.is_file():
        names = ", ".join(available()) or "(none found)"
        raise SiteError(
            f"unknown site {name!r} ({path} not found). Available: {names}. "
            + _no_site_hint()
        )

    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise SiteError(f"{path}: malformed TOML: {exc}") from exc
    except OSError as exc:
        raise SiteError(f"{path}: cannot read: {exc}") from exc

    for required in ("host", "scheduler", "user"):
        if not isinstance(raw.get(required), str) or not raw[required].strip():
            raise SiteError(f"{path}: missing or non-string required key {required!r}")

    scheduler = raw["scheduler"].strip().lower()
    if scheduler not in SUPPORTED_SCHEDULERS:
        raise SiteError(dispatch_unsupported(scheduler, path))

    warnings: list[str] = []
    policy = _table(raw, "policy", _POLICY_KEYS, path)
    filesystem = _table(raw, "filesystem", _FILESYSTEM_KEYS, path)
    environment = _table(raw, "environment", _ENVIRONMENT_KEYS, path)

    paths = raw.get("paths", {})
    if not isinstance(paths, dict):
        raise SiteError(f"{path}: [paths] must be a table")
    paths = dict(paths)
    # Two keys that once lived in [paths] are facts about the machine, not
    # places the user works in. Old files keep working, with a note.
    home = _moved(filesystem, "home", paths, "home", None, "[filesystem] home",
                  "[paths] home", path, warnings)
    python_bin = _moved(environment, "python_bin", paths, "python_bin", None,
                        "[environment] python_bin", "[paths] python_bin", path, warnings)
    paths.pop("home", None)
    paths.pop("python_bin", None)

    resolved: dict[str, str] = {}
    for key, value in paths.items():
        if not _KEY_RE.match(key):
            raise SiteError(
                f"{path}: path role {key!r} is not a valid identifier "
                "(letters, digits, underscore; must not start with a digit)"
            )
        if not isinstance(value, str):
            raise SiteError(f"{path}: path role {key!r} must be a string")
        resolved[key] = value
    for key, value, label in (("home", home, "[filesystem] home"),
                              ("python_bin", python_bin, "[environment] python_bin")):
        if value is None:
            continue
        if not isinstance(value, str) or not value.startswith("/"):
            raise SiteError(f"{path}: {label} must be an absolute path string")
        resolved[key] = value

    # The scratch role is validated exactly like a write root: it is one
    # of the roots inside which `mv` is write-tier, so a site declaring
    # scratch = "/" or a relative path would make the guard treat a move
    # anywhere as routine.
    if "scratch" in resolved:
        _validate_root(resolved["scratch"], "scratch role", path)

    writable = _moved(policy, "write_roots", raw, "writable_roots", [],
                      "[policy] write_roots", "writable_roots", path, warnings)
    if not isinstance(writable, list) or not all(isinstance(v, str) for v in writable):
        raise SiteError(f"{path}: write_roots must be a list of strings")
    for root in writable:
        _validate_root(root, "write_roots entry", path)

    protected = policy.get("protected", [])
    if not isinstance(protected, list) or not all(isinstance(v, str) for v in protected):
        raise SiteError(f"{path}: [policy] protected must be a list of strings")
    for root in protected:
        if not root.startswith("/") or ".." in root.split("/"):
            raise SiteError(f"{path}: protected entry {root!r} must be an absolute "
                            "path with no '..' segment")

    script_roots = raw.get("script_roots", [])
    if not isinstance(script_roots, list) or not all(
        isinstance(v, str) for v in script_roots
    ):
        raise SiteError(f"{path}: script_roots must be a list of strings")
    for root in script_roots:
        _validate_root(root, "script_roots entry", path)
    if script_roots:
        warnings.append("script_roots: list each repo as [repos.<name>] with "
                        "remote = \"...\" and trusted = true instead")
    script_confirm = raw.get("script_confirm", [])
    if not isinstance(script_confirm, list) or not all(
        isinstance(v, str) for v in script_confirm
    ):
        raise SiteError(f"{path}: script_confirm must be a list of strings")
    if script_confirm:
        warnings.append("script_confirm: give each pattern to the repo it is for, "
                        "as confirm = [...] in its [repos.<name>] entry")

    aliases = _moved(filesystem, "aliases", raw, "aliases", {},
                     "[filesystem] aliases", "[aliases]", path, warnings)
    if not isinstance(aliases, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in aliases.items()
    ):
        raise SiteError(f"{path}: aliases must map path strings to path strings")
    for alias, real in aliases.items():
        # Both sides are validated like a root: an alias of "/" or "/home"
        # would make the guard treat a whole filesystem as one directory.
        _validate_root(alias, "aliases key", path)
        _validate_root(real, "aliases value", path)

    nc_max = _moved(policy, "nc_max_read_mb", raw, "nc_max_read_mb", 100,
                    "[policy] nc_max_read_mb", "nc_max_read_mb", path, warnings)
    if isinstance(nc_max, bool) or not isinstance(nc_max, int) or not 1 <= nc_max <= 100_000:
        raise SiteError(f"{path}: nc_max_read_mb must be a whole number of MB, 1 to 100000")

    repos = _load_repos(raw.get("repos", {}), path)

    # A repo's role names its checkout ($HPC_REPO); trusted repos are the
    # script roots; a repo's confirm patterns apply to that root alone.
    roots = [v.rstrip("/") for v in script_roots]
    root_confirm: dict[str, tuple[str, ...]] = {}
    for repo in repos.values():
        if repo.role:
            if repo.role in resolved:
                raise SiteError(f"{path}: [repos] {repo.name!r} role {repo.role!r} "
                                "is already a path role")
            resolved[repo.role] = repo.remote
        if repo.trusted:
            if repo.remote not in roots:
                roots.append(repo.remote)
            if repo.confirm:
                root_confirm[repo.remote] = repo.confirm

    sandbox_roots = _load_sandbox_roots(policy.get("sandbox_roots", []), path, resolved,
                                        writable, protected, aliases)
    # One sandbox root is also the path role `sandbox` ($HPC_SANDBOX). With
    # several, name the ones the agent should know in [paths] yourself.
    if len(sandbox_roots) == 1 and "sandbox" not in resolved:
        resolved["sandbox"] = sandbox_roots[0]

    profile = _moved(policy, "profile", raw, "profile", profile_mod.DEFAULT,
                     "[policy] profile", "profile", path, warnings)
    if not isinstance(profile, str) or profile.strip() not in profile_mod.LEVELS:
        raise SiteError(
            f"{path}: profile {profile!r} is not one of "
            f"{', '.join(profile_mod.LEVELS)}"
        )

    return Site(
        name=str(raw.get("name", name)),
        host=raw["host"].strip(),
        scheduler=scheduler,
        user=raw["user"].strip(),
        paths=resolved,
        writable_roots=[v.rstrip('/') or '/' for v in writable],
        script_roots=roots,
        script_confirm=list(script_confirm),
        source=path,
        profile=profile.strip(),
        aliases={k.rstrip("/"): v.rstrip("/") for k, v in aliases.items()},
        nc_max_read_mb=nc_max,
        repos=repos,
        protected=[v.rstrip("/") for v in protected],
        root_confirm=root_confirm,
        warnings=tuple(warnings),
        sandbox_roots=sandbox_roots,
    )


def _load_sandbox_roots(roots, path: Path, roles: dict, writable: list,
                        protected: list, aliases: dict) -> list[str]:
    """Validate ``[policy] sandbox_roots``: each inside a write root (or the
    scratch role) and clear of every protected path. A sandbox widens
    nothing the profile allows; it only drops the token inside it, so it
    must lie where writes are allowed at all."""
    if not isinstance(roots, list) or not all(isinstance(v, str) for v in roots):
        raise SiteError(f"{path}: [policy] sandbox_roots must be a list of strings")
    return [_check_sandbox_root(r.strip(), path, roles, writable, protected, aliases)
            for r in roots]


def _check_sandbox_root(root: str, path: Path, roles: dict, writable: list,
                        protected: list, aliases: dict) -> str:
    _validate_root(root, "sandbox_roots entry", path)
    root = root.rstrip("/")
    pairs = sorted(((k.rstrip("/"), v.rstrip("/")) for k, v in aliases.items()),
                   key=lambda kv: -len(kv[0]))

    def canon(p: str) -> str:
        for alias, real in pairs:
            if p == alias or p.startswith(alias + "/"):
                return real + p[len(alias):]
        return p

    def inside(p: str, r: str) -> bool:
        return p == r or p.startswith(r.rstrip("/") + "/")

    c_root = canon(root)
    allowed = [canon(r.rstrip("/")) for r in writable]
    if roles.get("scratch"):
        allowed.append(canon(roles["scratch"].rstrip("/")))
    if not any(inside(c_root, r) for r in allowed):
        raise SiteError(f"{path}: sandbox_roots entry {root!r} is not inside a "
                        "write root or the scratch role")
    for prot in protected:
        c_prot = canon(prot.rstrip("/"))
        if inside(c_root, c_prot) or inside(c_prot, c_root):
            raise SiteError(f"{path}: sandbox_roots entry {root!r} overlaps "
                            f"protected path {prot!r}")
    return root


#: Keys each new-style table may hold. Strict, like [repos]: a misspelled
#: key in [policy] would otherwise silently leave a default in force.
_POLICY_KEYS = frozenset({"profile", "write_roots", "protected", "nc_max_read_mb",
                          "sandbox_roots"})
_FILESYSTEM_KEYS = frozenset({"home", "aliases"})
_ENVIRONMENT_KEYS = frozenset({"python_bin"})


def _table(raw: dict, name: str, keys: frozenset, path: Path) -> dict:
    table = raw.get(name, {})
    if not isinstance(table, dict):
        raise SiteError(f"{path}: [{name}] must be a table")
    unknown = sorted(set(table) - keys)
    if unknown:
        raise SiteError(f"{path}: [{name}] has unknown key(s) {', '.join(unknown)}; "
                        f"known: {', '.join(sorted(keys))}")
    return table


def _moved(new: dict, new_key: str, old: dict, old_key: str, default,
           new_label: str, old_label: str, path: Path, warnings: list):
    """A value from its new place, or from where it used to live (with a
    note). Both at once is an error: which one wins would be a guess."""
    if new_key in new and old_key in old:
        raise SiteError(f"{path}: {old_label} and {new_label} are both set; "
                        f"keep {new_label} only")
    if old_key in old:
        warnings.append(f"{old_label} has moved to {new_label}")
        return old[old_key]
    return new.get(new_key, default)


def _load_repos(table, path: Path) -> dict[str, Repo]:
    """Validate ``[repos]``. Strict: an unknown key is an error, not ignored,
    because a misspelled ``branch`` or ``mode`` would silently grant more."""
    if not isinstance(table, dict):
        raise SiteError(f"{path}: [repos] must be a table of [repos.<name>] entries")
    repos: dict[str, Repo] = {}
    for name, entry in table.items():
        where = f"{path}: [repos] {name!r}"
        if not _REPO_NAME_RE.match(name):
            raise SiteError(f"{where}: name must be letters, digits, '.', '_' or '-'")
        if not isinstance(entry, dict):
            raise SiteError(f"{where} must be a table: [repos.{name}] with remote = \"...\"")
        unknown = sorted(set(entry) - _REPO_KEYS)
        if unknown:
            raise SiteError(f"{where}: unknown key(s) {', '.join(unknown)}; "
                            f"known: {', '.join(sorted(_REPO_KEYS))}")
        if not isinstance(entry.get("remote"), str) or not entry["remote"].strip():
            raise SiteError(f"{where}: missing or non-string 'remote'")
        local = entry.get("local")
        if local is not None and (not isinstance(local, str) or not local.strip()):
            raise SiteError(f"{where}: 'local' must be a path string (or left out)")
        remote = entry["remote"].strip()
        _validate_root(remote, f"[repos] {name!r} remote", path)
        branch = entry.get("branch")
        if branch is not None and (not isinstance(branch, str) or not branch.strip()
                                   or branch.strip().startswith("-")):
            raise SiteError(f"{where}: branch must be a branch name")
        mode = entry.get("mode", "pull-only")
        if mode not in REPO_MODES:
            raise SiteError(f"{where}: mode {mode!r} is not one of {', '.join(REPO_MODES)}")
        trusted = entry.get("trusted", False)
        if not isinstance(trusted, bool):
            raise SiteError(f"{where}: trusted must be true or false")
        role = entry.get("role")
        if role is not None and (not isinstance(role, str) or not _KEY_RE.match(role)):
            raise SiteError(f"{where}: role must be a name like \"repo\" "
                            "(letters, digits, underscore)")
        confirm = entry.get("confirm", [])
        if not isinstance(confirm, list) or not all(isinstance(p, str) for p in confirm):
            raise SiteError(f"{where}: confirm must be a list of patterns")
        if confirm and not trusted:
            raise SiteError(f"{where}: confirm applies only to a trusted repo "
                            "(an untrusted repo's scripts already need a token)")
        preview = entry.get("preview", True)
        if not isinstance(preview, bool):
            raise SiteError(f"{where}: preview must be true or false")
        if "preview" in entry and not trusted:
            raise SiteError(f"{where}: preview applies only to a trusted repo "
                            "(it governs previewing its confirm matches)")
        repos[name] = Repo(
            name=name,
            local=str(Path(local.strip()).expanduser()) if local else None,
            remote=remote.rstrip("/"),
            branch=branch.strip() if branch else None,
            mode=mode,
            trusted=trusted,
            role=role,
            confirm=tuple(confirm),
            preview=preview,
        )
    remotes = sorted((r.remote, r.name) for r in repos.values())
    for (a, an), (b, bn) in zip(remotes, remotes[1:]):
        if b == a or b.startswith(a + "/"):
            raise SiteError(f"{path}: [repos] {an!r} and {bn!r} overlap ({a}, {b}); "
                            "a checkout is listed once")
    roles = [r.role for r in repos.values() if r.role]
    if len(roles) != len(set(roles)):
        raise SiteError(f"{path}: two [repos] entries share a role")
    return repos


def dispatch_unsupported(scheduler: str, path: Path) -> str:
    """Message for a scheduler that is recognized but not implemented.

    This is the dispatch point for PBS/LSF support. Nothing here parses their
    output, and nothing should until it can be tested against a real system --
    an untested scheduler parser reports confident wrong job states, which is
    worse than reporting none.
    """
    return (
        f"{path}: scheduler {scheduler!r} is not supported. "
        "This toolkit implements SLURM only (squeue/sacct/srun). "
        "PBS and LSF are deliberately unimplemented: their output formats "
        "cannot be tested here, and a wrong job-state parse is worse than no "
        "parse. There is no fallback path through this toolkit -- every verb "
        "loads the site through this function and will refuse the same way, "
        "hpc-run included. Run that scheduler's commands by hand, or add "
        "SUPPORTED_SCHEDULERS support with parsers tested against a real "
        "system."
    )
