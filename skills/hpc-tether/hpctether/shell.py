"""Shell parsing for the payload guard: text in, commands out. No policy.

This module reads a bash payload the way the guard needs it read: logical
lines (backslash continuations joined), segments split at ``;``, ``&&``,
``||``, ``|`` and ``&`` outside quotes, ``$(...)``/backtick/process
substitutions masked in the outer line and returned as bodies of their own,
words tokenized, and the prefixes that introduce a command (keywords,
assignments, wrappers such as ``timeout`` or ``env``) stepped over to find
the command itself. It also tracks the one piece of shell state the guard
relies on: the directory a ``cd DIR &&`` puts the next command in.

It decides nothing. What a command is allowed to do is in
hpctether/rules.py and hpctether/commands.py; hpctether/guard.py joins
the two. The parser is lexical and best-effort: it reads text, it does not
evaluate the shell. Where it cannot tell, it errs toward exposing more
text to the rules (an unbalanced substitution is scanned to the end of the
line; a line with a comment is never joined to the next).
"""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass, field

from hpctether.commands import (
    WRAPPERS,
    WRAPPER_POSITIONALS,
    WRAPPER_VALUE_FLAGS,
)

#: A function definition header: `f()` or `f(){` with no space before the
#: brace, which shlex glues into a single token. The body is real code on the
#: same segment, so the header is a prefix to step over like any keyword.
FUNC_DEF_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\(\)\{?$")

#: Shell keywords that introduce a command without being one. Splitting
#: `if true; then rm -rf /x; fi` on `;` leaves a segment that STARTS with
#: `then` -- so `then` was read as the command and the `rm` behind it was
#: never classified. Control flow is exactly where a destructive command hides
#: under context pressure, so these must be stepped over, not stopped on.
LOOP_HEADERS = frozenset({"for", "select", "case"})

SHELL_KEYWORDS = frozenset(
    {
        "if", "then", "else", "elif", "fi", "while", "until", "for", "do",
        "done", "case", "esac", "in", "select", "function", "{", "}", "!",
    }
)

SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|;|\|")


def split_segments(line: str) -> list[str]:
    """Split a line on shell separators, ignoring separators inside quotes.
    See :func:`split_segments_ops`, which also reports each separator."""
    return [s for s, _ in split_segments_ops(line) if s.strip()]


def split_segments_ops(line: str) -> list[tuple[str, str]]:
    """``(segment, separator_after)`` pairs; the last separator is ``""``.

    Empty segments are kept, so a separator always belongs to the segment
    written before it -- :func:`cd_dirs` needs to know that a ``cd`` and the
    command after it are joined by ``&&`` and by nothing else.

    A naive ``SEGMENT_SPLIT_RE.split`` shatters a quoted grep alternation such
    as ``grep -n "rm -rf\\|if \\[ -d" f.sh`` into fragments like
    ``grep -n "rm -rf\\`` — an unterminated quote whose ``-rf`` then reads as
    short flags, so the pattern text is misclassified as a recursive grep.
    That refused a read-only command repeatedly in real use.

    Splitting is quote-aware here. This only ever REMOVES false segments; a
    real separator outside quotes still splits, so genuinely dangerous
    commands after ``;`` or ``|`` are still classified. Verified by
    test_quoted_separator_does_not_hide_real_command.
    """
    segments: list[tuple[str, str]] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if quote:
            buf.append(ch)
            # Inside double quotes a backslash escapes the next character;
            # inside single quotes bash treats backslash literally.
            if ch == "\\" and quote == '"' and i + 1 < n:
                buf.append(line[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        # `&` inside a redirection -- `2>&1`, `>&2`, `<&0`, `&>file` -- is
        # part of the redirect, not a separator. Splitting there left a
        # "command" named `1` or `2`, which became a false refusal once
        # unknown commands stopped passing as reads. What follows such an `&`
        # is a redirect target, never a command, so nothing is hidden.
        if ch == "&" and not line.startswith("&&", i) and (
            (i > 0 and line[i - 1] in "<>") or line.startswith("&>", i)
        ):
            buf.append(ch)
            i += 1
            continue
        if ch in ("|", "&", ";"):
            # Consume a two-character operator as one separator.
            if line.startswith("||", i) or line.startswith("&&", i):
                op = line[i:i + 2]
                i += 2
            else:
                op = ch
                i += 1
            segments.append(("".join(buf), op))
            buf = []
            continue
        buf.append(ch)
        i += 1
    segments.append(("".join(buf), ""))
    return segments


def cd_target(segment: str, paths: dict[str, str], home: str | None = None) -> str | None:
    """The absolute directory ``segment`` changes into, if it is exactly
    ``cd <absolute-path>``, ``cd <role>[/sub]`` or ``cd ~[/sub]`` (with a
    ``home``); None for anything else.

    Deliberately narrow. A variable other than a site role, ``cd -``,
    ``~user``, a relative target, a glob, extra arguments or any ``..`` all
    return None:
    this scan does not evaluate shell state, and a directory it cannot name
    with certainty must not be used to resolve a trusted path.
    """
    tokens = tokenize(segment)
    if len(tokens) != 2 or tokens[0] != "cd":
        return None
    from .trust import expand_tilde

    target = expand_tilde(tokens[1], home)
    for role, root in (paths or {}).items():
        var = f"HPC_{role.upper()}"
        for prefix in (f"${{{var}}}", f"${var}"):
            if target == prefix or target.startswith(prefix + "/"):
                target = root.rstrip("/") + target[len(prefix):]
                break
        else:
            continue
        break
    if (not target.startswith("/") or "$" in target or "~" in target
            or any(c in target for c in "*?[")
            or ".." in target.split("/")):
        return None
    return posixpath.normpath(target)


def cd_dirs(
    pairs: list[tuple[str, str]], paths: dict[str, str], home: str | None = None,
) -> list[str | None]:
    """For each segment, the directory a relative trusted path resolves in.

    Only a segment IMMEDIATELY preceded by ``cd <dir> &&`` gets one. ``&&`` is
    the whole point: if the ``cd`` fails the command never runs, so it cannot
    run from some other directory where the same relative name is a different
    file. ``;``, ``||``, a pipe, a background ``&``, or any command in between
    leaves the directory unknown and the invocation at interpreter tier.

    The ``cd`` must itself run unconditionally in the current shell: first on
    the line, or after ``;`` or ``&&``. After ``||`` it is skipped whenever the
    command before it succeeds (``a || cd X && python f.py`` is
    ``(a || cd X) && python f.py``); after ``|`` it runs in a pipeline
    subshell and changes nothing. Either way the ``&&`` would still fire from
    the old directory.
    """
    out: list[str | None] = [None] * len(pairs)
    for k in range(1, len(pairs)):
        prev_seg, prev_op = pairs[k - 1]
        before_cd = pairs[k - 2][1] if k >= 2 else ""
        if prev_op == "&&" and before_cd in ("", ";", "&&"):
            out[k] = cd_target(prev_seg.strip(), paths, home)
    return out


def chain_dirs(
    pairs: list[tuple[str, str]], paths: dict[str, str], home: str | None = None,
) -> list[str | None]:
    """For each segment, the directory it runs in, for write confinement.

    Wider than :func:`cd_dirs`, which trusts a script only directly after
    its ``cd``: here a ``cd <dir> &&`` holds for the whole chain that
    follows, as long as every link is ``&&`` or ``|`` and none changes
    directory. ``cd X && git log && git pull`` writes the repository in X.
    ``;``, ``||`` or a background ``&`` ends it: past one of those the ``cd``
    may not have run. The ``cd`` itself must run unconditionally in this
    shell, exactly as in ``cd_dirs``.
    """
    out: list[str | None] = [None] * len(pairs)
    cur: str | None = None
    for k, (seg, op) in enumerate(pairs):
        out[k] = cur
        prev_op = pairs[k - 1][1] if k else ""
        target = cd_target(seg.strip(), paths, home)
        if target is not None and prev_op in ("", ";", "&&"):
            cur = target if op == "&&" else None
        elif CD_WORD_RE.match(seg) or op not in ("&&", "|"):
            cur = None
    return out


CD_WORD_RE = re.compile(r"\s*\(?\s*(?:cd|pushd|popd)(?:\s|$)")

#: Any change of directory anywhere in a payload. With none, a relative
#: write target is relative to the login directory; with one, it is relative
#: only to a `cd <dir> &&` directly before it on the same line.
ANY_CD_RE = re.compile(r"(?:^|[\s;&|(`])(?:cd|pushd|popd)(?:\s|$|;)", re.M)

#: A payload that defines its own ``cd`` (function or alias) makes every
#: ``cd <dir> &&`` a claim the scan cannot check, so relative trusted paths
#: are not resolved anywhere in it.
CD_REDEFINED_RE = re.compile(
    r"(^|[\s;&|(])(function\s+cd\b|cd\s*\(\s*\)|alias\s+cd=)"
)
BACKTICK_RE = re.compile(r"`([^`]*)`")

#: ``HOME`` anywhere except as a ``$HOME`` / ``${HOME}`` read: an assignment,
#: ``export``, ``read``, ``declare`` ... Tilde expansion reads ``$HOME``, so
#: once a payload may have changed it, ``~`` is not resolved anywhere in it.
HOME_TOUCHED_RE = re.compile(r"(?<![$\w{])HOME\b")


def substitutions(line: str) -> list[str]:
    """Contents of every ``$(...)`` and backtick substitution in ``line``.

    ``$(...)`` is extracted by balancing parentheses rather than by regex: a
    non-greedy ``[^()]*`` stops at the first inner paren, so
    ``$(python -c 'print(1)')`` would yield nothing and its contents would
    never be scanned at all. Substituted text runs, so anything missed here is
    unclassified execution.

    Extraction recurses: ``$(echo $(sbatch j.sh))`` yields the outer body and
    the inner one, so a construct cannot be hidden by one more layer of
    nesting.

    Process substitution -- ``<(cmd)`` and ``>(cmd)`` -- is extracted the same
    way. ``diff <(ls a) <(rm -rf b)`` runs the ``rm`` exactly as ``$(...)``
    would, and it reached read tier because only ``$(`` was searched for
   .
    """
    found: list[str] = []
    i = 0
    while True:
        candidates = [
            k for k in (line.find(op, i) for op in ("$(", "<(", ">(")) if k != -1
        ]
        if not candidates:
            break
        start = min(candidates)
        depth = 0
        for j in range(start + 1, len(line)):
            if line[j] == "(":
                depth += 1
            elif line[j] == ")":
                depth -= 1
                if depth == 0:
                    body = line[start + 2 : j]
                    found.append(body)
                    found.extend(substitutions(body))
                    i = j + 1
                    break
        else:
            # Unbalanced: scan the remainder rather than silently dropping it.
            remainder = line[start + 2 :]
            found.append(remainder)
            found.extend(substitutions(remainder))
            break
    for match in BACKTICK_RE.finditer(line):
        body = match.group(1)
        found.append(body)
        found.extend(substitutions(body))
    return found


#: What a substitution becomes in the outer line (see mask_substitutions):
#: ``__HPC_SUBST_<n>__`` — one token, no quotes, no separators, never a valid
#: command or path. This prefix is what the command-name rule looks for.
SUBST_PLACEHOLDER = "__HPC_SUBST_"


def mask_substitutions(line: str) -> "tuple[str, list[str]]":
    """``line`` with every top-level ``$(...)``, ``<(...)``, ``>(...)`` and
    backtick substitution replaced by :data:`SUBST_PLACEHOLDER`.

    The bodies are scanned on their own (:func:`substitutions`, same extents,
    same unbalanced-remainder rule), so the OUTER line must not also carry
    their text: splitting and tokenizing it with the text in place misreads
    the command. ``x=$(echo y) rm -rf /x`` tokenized as ``x=$(echo`` (an
    assignment, skipped), ``y)`` (taken as the command) — and the ``rm`` was
    never classified: read tier, no token. The same misreading refused the
    read-only ``n=$(ls $d | grep -c x)`` as "``$d`` runs a file", after
    splitting the substitution's own pipe. Masked, an
    assignment prefix is one token and the verb behind it is the command.

    Returns ``(masked, originals)``: placeholder ``n`` stands for
    ``originals[n]``, so what is recorded about the line (a trusted script's
    arguments in the audit) can name the text that actually runs.
    """
    originals: list[str] = []

    def hold(text: str) -> str:
        originals.append(text)
        return f"{SUBST_PLACEHOLDER}{len(originals) - 1}__"

    out: list[str] = []
    i = 0
    while True:
        candidates = [
            k for k in (line.find(op, i) for op in ("$(", "<(", ">(")) if k != -1
        ]
        if not candidates:
            out.append(line[i:])
            break
        start = min(candidates)
        out.append(line[i:start])
        depth = 0
        for j in range(start + 1, len(line)):
            if line[j] == "(":
                depth += 1
            elif line[j] == ")":
                depth -= 1
                if depth == 0:
                    out.append(hold(line[start:j + 1]))
                    i = j + 1
                    break
        else:
            out.append(hold(line[start:]))
            break
    masked = BACKTICK_RE.sub(lambda m: hold(m.group(0)), "".join(out))
    return masked, originals


def unmask(text: str, originals: "list[str]") -> str:
    """Put the original substitution text back in place of its placeholders."""
    return re.sub(re.escape(SUBST_PLACEHOLDER) + r"(\d+)__",
                  lambda m: originals[int(m.group(1))], text)


def tokenize(segment: str) -> list[str]:
    """Best-effort shell tokenization; fall back to whitespace on bad quoting.

    A ``)`` closing a subshell is dropped from the last word. ``(seq 3 |
    head -1)`` left ``head`` with the option ``-1)``, refused as unknown
   , and ``(touch $HPC_SCRATCH/x)`` named the file ``x)``.
    Substitutions are masked before this runs, so an unquoted ``)`` ending
    a segment can only close a group.
    """
    try:
        tokens = shlex.split(segment, comments=False, posix=True)
    except ValueError:
        tokens = segment.split()
    raw = segment.rstrip()
    if tokens and raw.endswith(")") and tokens[-1].endswith(")"):
        last = tokens[-1]
        closing = min(len(raw) - len(raw.rstrip(")")), len(last) - len(last.rstrip(")")))
        if last[:-closing]:
            tokens[-1] = last[:-closing]
        else:
            tokens.pop()
    return tokens


def strip_wrappers(tokens: list[str]) -> list[str]:
    """Drop leading keywords, env assignments and wrappers to find the real verb.

    Everything skipped here is a *prefix* -- it does not itself act, it
    introduces the command that does. Stopping early is the dangerous failure:
    the token we stop on is treated as the command, so the real verb behind it
    is never classified and the whole segment defaults to read tier. Four such
    prefixes each let a bare ``rm`` through:

    ==========================  ================  ==============================
    payload                     stopped on        now
    ==========================  ================  ==============================
    ``if ...; then rm -rf /x``  ``then``          keyword skipped
    ``command -- rm -rf /x``    ``--``            end-of-options skipped
    ``env -i rm -rf /x``        ``-i``            wrapper flag skipped
    ``xargs -n 1 rm /x``        ``1``             flag *and its value* skipped
    ==========================  ================  ==============================

    Wrapper flags are skipped along with their arguments; the previous code
    handled the ``FOO=bar`` assignment form but not the flag form, which is why
    ``env FOO=1 rm`` was caught and ``env -i rm`` was not.
    """
    tokens = list(tokens)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        # `for x in a b`, `select x in ...`, `case $x in`: a header names a
        # variable and a word list, and runs nothing. Without this the loop
        # variable was read as the command (`for c in ...` -> command `c`).
        # Substitutions inside the words are scanned separately by the guard.
        if tok in LOOP_HEADERS:
            return []
        # A subshell: `(cd /x && ls)`. The `(` opens a group; the command is
        # what follows it. `$(` is a substitution, masked before this runs.
        if tok == "(":
            i += 1
            continue
        if tok.startswith("(") and not tok.startswith("$("):
            tokens[i] = tok[1:]
            continue
        # A case label followed by its command: `pt01) echo a`. A label alone
        # on its line is not recognized and is refused as unknown below
        # improvise; put the label and its command on one line.
        if (tok.endswith(")") and "(" not in tok and i + 1 < len(tokens)
                and not tok.startswith("-")):
            i += 1
            continue
        # `then rm ...`, `do rm ...` -- a keyword introduces, it does not act.
        if tok in SHELL_KEYWORDS:
            i += 1
            continue
        # A function header: `f()`, or `f(){` when no space precedes the brace.
        # The body follows on the same segment and must still be classified.
        if FUNC_DEF_RE.match(tok):
            i += 1
            continue
        # `--` ends option parsing; the verb is whatever follows.
        if tok == "--":
            i += 1
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            i += 1
            continue
        if tok in WRAPPERS or tok == "xargs":
            i += 1
            i = skip_wrapper_args(tokens, i, tok)
            continue
        break
    return tokens[i:]


def skip_wrapper_args(tokens: list[str], i: int, wrapper: str) -> int:
    """Advance past a wrapper's own flags (and their values) to the real verb."""
    positionals_left = WRAPPER_POSITIONALS.get(wrapper, 0)
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            return i + 1
        if tok.startswith("-"):
            # `--opt=value` carries its value; a bare value-taking flag eats
            # the next token. `xargs -n 1` is the live case.
            takes_value = WRAPPER_VALUE_FLAGS.get(wrapper, frozenset())
            if "=" not in tok and tok in takes_value:
                i += 2
            else:
                i += 1
            continue
        # timeout's duration precedes the command and is not a flag.
        if wrapper == "timeout" and re.match(r"^\d+(\.\d+)?[smhd]?$", tok):
            i += 1
            continue
        # `flock LOCKFILE cmd`: the lock path is an operand, not the verb.
        if positionals_left:
            positionals_left -= 1
            i += 1
            continue
        break
    return i


def path_args(tokens: list[str]) -> list[str]:
    """Non-flag arguments after the command token."""
    return [t for t in tokens[1:] if not t.startswith("-") and t != "--"]


def short_flags(tokens: list[str]) -> str:
    """All single-dash flag letters after the command, concatenated."""
    letters = []
    for t in tokens[1:]:
        if t.startswith("-") and not t.startswith("--"):
            letters.append(t.lstrip("-"))
    return "".join(letters)


def continues(line: str) -> bool:
    """True if ``line`` ends in a backslash that joins it to the next line.

    An odd run of trailing backslashes continues the line; an even run is
    literal. A backslash inside a comment does not continue anything, and
    joining across one would hide the next line inside the comment. So a line
    with any ``#`` in it is never joined: its continuation is then checked as
    a line of its own, which can only refuse more, never less.
    """
    stripped = line.rstrip()
    run = len(stripped) - len(stripped.rstrip("\\"))
    return run % 2 == 1 and "#" not in stripped


def logical_lines(script: str):
    """``(first_lineno, text)`` per logical line, joining ``\\``-continued lines.

    ``cd $HPC_REPO && \\`` followed by ``python x.py`` is one command to bash.
    Classified as two lines, the second's first word stood alone and the
    first ended in a stray ``\\`` read as a command.
    """
    lines = script.splitlines()
    i = 0
    while i < len(lines):
        start = i
        text = lines[i]
        while continues(text) and i + 1 < len(lines):
            i += 1
            text = text.rstrip()[:-1] + " " + lines[i].lstrip()
        yield start + 1, text
        i += 1


def command_lines(script: str):
    """``(lineno, raw, line)`` for every logical line that holds commands.

    Blank lines and comment lines are skipped. ``line`` is ``raw`` stripped,
    with a trailing comment dropped when the line has no quotes it could be
    hiding in.
    """
    for lineno, raw in logical_lines(script):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "'" not in line and '"' not in line:
            line = re.sub(r"\s+#.*$", "", line)
        yield lineno, raw, line


@dataclass
class Segment:
    """One command as the guard sees it.

    ``text`` is the segment's source (substitutions masked, for a top-level
    segment). ``cd_dir`` is the directory a relative trusted-script path
    resolves in (only directly after ``cd DIR &&``); ``write_dir`` the
    directory a relative write lands in (a ``cd DIR &&`` chain). Both are
    None when the parser cannot name the directory, and always None for a
    substitution body.
    """

    text: str
    cd_dir: str | None = None
    write_dir: str | None = None


@dataclass
class Line:
    """One logical line: its segments, then the substitution bodies' segments."""

    lineno: int
    raw: str
    text: str
    masked: str
    originals: list[str] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)


def parse_line(
    lineno: int, raw: str, line: str, paths: dict[str, str],
    trusted_home: str | None, write_home: str | None,
    track_trusted: bool, cd_redefined: bool,
) -> Line:
    """Split one command line into segments with their directories.

    ``track_trusted`` resolves ``cd_dir`` (only when trusted scripts are in
    play); a payload that redefines ``cd`` resolves no directory at all.
    ``trusted_home`` and ``write_home`` are what ``~`` means for each.
    """
    masked, originals = mask_substitutions(line)
    pairs = split_segments_ops(masked)
    dirs = (cd_dirs(pairs, paths, trusted_home)
            if track_trusted and not cd_redefined else [None] * len(pairs))
    write_dirs = (chain_dirs(pairs, paths, write_home)
                  if not cd_redefined else [None] * len(pairs))
    segments = [Segment(s, d, w) for (s, _), d, w in zip(pairs, dirs, write_dirs)
                if s.strip()]
    # Substitutions run too: their bodies are segments of their own, and a
    # body may itself be a pipeline.
    for inner in substitutions(line):
        if inner.strip():
            segments.extend(Segment(s) for s in split_segments(inner))
    return Line(lineno, raw, line, masked, originals, segments)
