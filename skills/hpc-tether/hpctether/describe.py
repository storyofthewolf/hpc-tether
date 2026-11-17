"""Say in plain words what a payload will do, for the user's approval prompt.

A payload worth approving is often hard to read: substitutions, loops,
`$HPC_*` roles, long paths. The approval hook (hooks/approve_hpc_writes.py)
puts a short phrase above the command, built here from the guard's own
report, so the words come from the toolkit and not from the agent:

    create directory scratch:new; move scratch:a.nc → scratch:old/a.nc;
    submit job run_A (+2 read-only lines)

Only the lines the guard flagged (writes, job control, code, unknown and
forbidden commands) and the trusted scripts it recognized are phrased. Every
other command line is counted as read-only. A command with no phrase here is
named as itself (`run ncks`), never dropped: the summary may be terse, but it
never hides a flagged line.

Descriptive only. The guard decides; nothing here changes what is sent.
"""

from __future__ import annotations

import posixpath
import re

from hpctether import confine, guard, shell
from hpctether.commands import READ_COMMANDS

#: Most phrases shown before "and N more".
MAX_PHRASES = 6

_ROLE = re.compile(r"\$\{?HPC_([A-Z0-9_]+)\}?/?")
_PY = frozenset({"python", "python3", "perl", "Rscript", "ruby", "node", "julia"})
_SHELLS = frozenset({"bash", "sh", "zsh", "ksh", "dash", "csh", "tcsh"})
_NETCDF = frozenset({"ncks", "ncrcat", "ncra", "ncea", "ncecat", "ncap2", "ncatted",
                     "ncrename", "ncpdq", "ncwa", "ncbo", "ncflint", "cdo", "nccopy"})
_COMPRESS = frozenset({"gzip", "bzip2", "xz", "zstd", "zip", "gunzip", "bunzip2", "unxz", "unzip"})
_NETWORK = frozenset({"scp", "rsync", "curl", "wget", "ssh", "sftp"})
_SKIP = frozenset({"cd", "export", "set", "true", ":"})


def _role_of(word: str, paths: dict[str, str]) -> str | None:
    for role, root in sorted(paths.items(), key=lambda kv: -len(kv[1])):
        if root and (word == root or word.startswith(root.rstrip("/") + "/")):
            return role + ":" + word[len(root.rstrip("/")) + 1:]
    return None


def role_path(word: str, paths: dict[str, str] | None = None, width: int = 48,
              aliases: dict[str, str] | None = None) -> str:
    """``$HPC_SCRATCH/a/b`` -> ``scratch:a/b``; a long path keeps its file name.

    With ``aliases`` (the site's [aliases]), a path spelled either way
    matches a role root spelled either way, so the phrase never shows
    the physical path for a path the agent wrote with the alias."""
    word = _ROLE.sub(lambda m: m.group(1).lower() + ":", word, count=1)
    paths = paths or {}
    found = _role_of(word, paths)
    if found is None and aliases:
        pairs = confine.sort_aliases(aliases)
        found = _role_of(confine.canonical(word, pairs),
                         {r: confine.canonical(root, pairs) for r, root in paths.items()})
    word = found if found is not None else word
    if word.endswith(":"):
        return word[:-1]
    if len(word) > width and "/" in word:
        head, _, _ = word.partition(":") if ":" in word.split("/", 1)[0] else ("", "", "")
        word = (head + ":" if head else "") + "…/" + word.rstrip("/").rsplit("/", 1)[-1]
    return word


def _redirects(tokens: list[str]) -> list[tuple[str, str]]:
    """``(op, target)`` for each file redirect; fd copies and /dev/null skipped."""
    out = []
    for i, tok in enumerate(tokens):
        m = re.match(r"^\d*(>>?|&>>?)(.*)$", tok)
        if not m or m.group(2).startswith("&"):
            continue
        target = m.group(2) or (tokens[i + 1] if i + 1 < len(tokens) else "")
        if target and target != "/dev/null":
            out.append((">>" if m.group(1).endswith(">>") else ">", target))
    return out


def _strip_redirects(tokens: list[str]) -> list[str]:
    out, skip = [], False
    for tok in tokens:
        if skip:
            skip = False
            continue
        m = re.match(r"^\d*(>>?|&>>?|<)(.*)$", tok)
        if m:
            skip = not m.group(2)
            continue
        out.append(tok)
    return out


def _option(tokens: list[str], *names: str) -> str | None:
    for i, tok in enumerate(tokens):
        for name in names:
            if tok == name and i + 1 < len(tokens):
                return tokens[i + 1]
            if name.startswith("--") and tok.startswith(name + "="):
                return tok.split("=", 1)[1]
            if not name.startswith("--") and tok.startswith(name) and len(tok) > len(name):
                return tok[len(name):]
    return None


def phrase(tokens: list[str], paths: dict[str, str] | None = None,
           aliases: dict[str, str] | None = None) -> list[str]:
    """Phrases for one command's tokens (wrappers already stripped)."""
    if not tokens:
        return []
    p = lambda w: role_path(w, paths, aliases=aliases)  # noqa: E731
    out = []
    for op, target in _redirects(tokens):
        out.append(("append to " if op == ">>" else "write file ") + p(target))
    tokens = _strip_redirects(tokens)
    if not tokens:
        return out
    cmd = posixpath.basename(tokens[0])
    args = shell.path_args(tokens)
    flags = shell.short_flags(tokens)
    if cmd in _SKIP or cmd in READ_COMMANDS:
        # A read inside a flagged line (`date` in `echo $(date) >> f`). A
        # read flagged for a writing option still gets a phrase: the line
        # falls back to "line N: write" when nothing else on it has one.
        return out
    if cmd == "mkdir":
        out.append("create directory " + ", ".join(p(a) for a in args))
    elif cmd in ("mv", "cp") and len(args) >= 2:
        verb = "move" if cmd == "mv" else "copy"
        no_clobber = " (no overwrite)" if "n" in flags else ""
        out.append(f"{verb} {', '.join(p(a) for a in args[:-1])} → {p(args[-1])}{no_clobber}")
    elif cmd == "ln" and args:
        out.append(f"link {p(args[-1])} → {p(args[0])}")
    elif cmd == "touch":
        out.append("touch " + ", ".join(p(a) for a in args))
    elif cmd == "tee":
        out.extend(("append to " if "a" in flags else "write file ") + p(a) for a in args)
    elif cmd == "tar":
        bare = len(tokens) > 1 and not tokens[1].startswith("-")
        mode = (tokens[1] if bare else "") + flags
        rest = args[1:] if bare else args
        archive = _option(tokens, "-f", "--file") or ("f" in mode and rest and rest[0]) or ""
        if "x" in mode or "--extract" in tokens:
            out.append("extract archive " + p(archive or "?"))
        elif "c" in mode or "--create" in tokens:
            out.append("create archive " + p(archive or "?"))
        else:
            out.append("run tar")
    elif cmd in _COMPRESS:
        out.append(("decompress " if cmd.startswith(("gun", "bun", "unx", "unz")) else "compress ")
                   + ", ".join(p(a) for a in args))
    elif cmd == "git":
        where = _option(tokens, "-C")
        rest = [t for t in tokens[1:] if t not in ("-C", where)]
        sub = next((t for t in rest if not t.startswith("-")), "")
        in_dir = f" in {p(where)}" if where else ""
        out.append({
            "pull": f"update git checkout{in_dir} (pull)",
            "fetch": f"fetch git history{in_dir}",
            "commit": f"commit{in_dir}",
            "push": f"push git commits{in_dir} to their remote",
            "merge": f"merge branches{in_dir}",
            "add": f"stage files{in_dir}",
            "switch": f"switch branch{in_dir}",
            "checkout": f"switch branch or restore files{in_dir}",
            "clone": "clone a repository" + (f" into {p(args[-1])}" if len(args) > 2 else ""),
        }.get(sub, f"git {sub}{in_dir}"))
    elif cmd == "sbatch":
        name = _option(tokens, "-J", "--job-name")
        script = next((a for a in args if a != name), "")
        if "--wrap" in " ".join(tokens):
            out.append("submit a job wrapping a command line")
        else:
            out.append("submit job " + (name or posixpath.basename(script) or "?")
                       + (f" ({posixpath.basename(script)})" if name and script else ""))
    elif cmd == "scancel":
        out.append("cancel job " + ", ".join(args) if args else "cancel jobs")
    elif cmd == "scontrol":
        sub = args[0] if args else ""
        settings = [a for a in args[1:] if "=" in a]
        job = next((a.split("=", 1)[1] for a in settings if a.lower().startswith("jobid=")), "")
        changes = [a for a in settings if not a.lower().startswith("jobid=")]
        if sub in ("hold", "release", "requeue", "suspend", "resume"):
            out.append(f"{sub} job " + " ".join(args[1:]))
        else:
            out.append(f"change job {job or '?'}: " + ", ".join(changes) if changes else f"scontrol {sub}")
    elif cmd == "kill":
        out.append("signal process " + " ".join(args))
    elif cmd in ("chmod", "chgrp"):
        out.append(f"change permissions on " + ", ".join(p(a) for a in args[1:]))
    elif cmd in _PY or cmd.startswith("python"):
        if "-c" in tokens or args[:1] == ["-"] or not args:
            out.append(f"run code the agent wrote ({cmd})")
        else:
            out.append(_untrusted(posixpath.basename(args[0])))
    elif cmd in _SHELLS or cmd in ("eval", "source", "."):
        out.append(f"run a nested shell ({cmd})")
    elif cmd == "srun":
        out.append("run on a compute node (srun)")
    elif cmd == "make":
        out.append("run make")
    elif cmd in _NETCDF:
        target = _option(tokens, "-o") or (args[-1] if args else "?")
        out.append(f"write netCDF {p(target)} ({cmd})")
    elif cmd in _NETWORK:
        out.append(f"network transfer ({cmd})")
    elif cmd in ("rm", "rmdir", "unlink", "shred"):
        out.append("delete " + ", ".join(p(a) for a in args))
    else:
        out.append(f"run {cmd}")
    return out


def _untrusted(name: str) -> str:
    return f"run {name} (not in a trusted repo)"


def _segments(line: str) -> list[str]:
    """Each command in ``line``, substitutions included."""
    masked, _ = shell.mask_substitutions(line)
    segs = list(shell.split_segments(masked))
    for body in shell.substitutions(line):
        segs.extend(shell.split_segments(body))
    return segs


def _line_phrases(line: str, paths: dict[str, str] | None,
                  aliases: dict[str, str] | None = None) -> list[str]:
    out = []
    for seg in _segments(line):
        tokens = shell.strip_wrappers(shell.tokenize(seg))
        # An assignment whose value is a substitution: the substitution is
        # phrased as its own segment.
        tokens = [tok for tok in tokens if shell.SUBST_PLACEHOLDER not in tok]
        out.extend(phrase(tokens, paths, aliases))
    return out


def describe(script: str, report: "guard.Report", paths: dict[str, str] | None = None,
             aliases: dict[str, str] | None = None) -> str:
    """One short line saying what ``script`` will do, from ``report``."""
    phrases: list[str] = []
    flagged: set[int] = set()
    never: list[str] = []
    for use in report.uses:
        phrases.append(f"run trusted {posixpath.basename(use.path)}"
                       + (f" {use.args}" if use.args else "")
                       + (" (matches its repo's confirm pattern)" if use.tier == "write" else ""))
    # A confirm match also flags its line. The generic phrasing of that line
    # would call the same script untrusted ("run trusted my-script.py …; run
    # my-script.py (not in a trusted repo)"), so it is left out;
    # anything else on the line is still phrased.
    already = {_untrusted(posixpath.basename(u.path)) for u in report.uses}
    for v in report.violations:
        if v.lineno in flagged:
            continue
        flagged.add(v.lineno)
        found = [x for x in _line_phrases(v.line, paths, aliases) if x not in already]
        if not found and any(u.tier == "write" for u in report.uses):
            continue
        found = found or [f"line {v.lineno}: {v.kind}"]
        if v.kind in (guard.FORBIDDEN, guard.TRAVERSAL):
            never.extend(found)
        else:
            phrases.extend(found)
    seen: set[str] = set()
    phrases = [x for x in phrases if not (x in seen or seen.add(x))]
    reads = 0
    for lineno, line in enumerate(script.splitlines(), 1):
        s = line.strip()
        if (lineno in flagged or not s or s.startswith("#")
                or re.match(r"^(export|cd|set|for|while|until|if|case|done|fi|then|else|elif|do|esac|\}|\{)\b", s)):
            continue
        if any(posixpath.basename(u.path) in s for u in report.uses):
            continue
        reads += 1
    shown = phrases[:MAX_PHRASES]
    text = "; ".join(shown)
    if len(phrases) > MAX_PHRASES:
        text += f"; and {len(phrases) - MAX_PHRASES} more"
    if reads:
        text += (" " if text else "") + f"(+{reads} read-only line{'s' if reads != 1 else ''})" \
            if text else f"read only ({reads} line{'s' if reads != 1 else ''})"
    if never:
        text += ("; " if text else "") + "REFUSED, never sent: " + "; ".join(never[:3])
    return text or "nothing to run"
