#!/usr/bin/env python3
"""PreToolUse hook: the user, not the agent, approves every hpc-tether token.

The confirmation tokens (--allow-write, --allow-exec, --yes, ...) are flags
the agent passes itself. They still do two jobs -- they declare intent, so a
payload the agent thought was read-only but that writes is refused, and they
leave an audit record -- but they are not consent. Consent has to come from
outside the agent. This hook supplies it: when a Bash command's text names
an hpc-tether tool and any token, it returns "ask", and Claude Code shows
the user a permission prompt for that exact command.

Why a hook rather than settings `ask` rules (plan §4.4): a settings rule
matches a command prefix, so another spelling slips past it, and a "don't
ask again" answer turns it into a permanent allow. A hook decides on every
call, and per the Claude Code docs an allow rule or "don't ask again" does
not skip a hook's "ask". In auto mode the prompt goes to the user and the
classifier does not decide it.

Modes where no one can answer: in `dontAsk` mode and non-interactive `-p`
runs Claude Code denies a call that would prompt. In `bypassPermissions`
mode it would skip the prompt, so this hook denies instead: a token there
would otherwise be the agent approving itself.

What it does not cover: a program the agent writes that reaches the
ControlMaster socket itself (Python importing hpctether, or shelling out to
ssh). That is outside any command-line check; sandboxing is the control for
it. Under Codex there is no hook, and the tokens are the only check.

Reads, and trusted scripts at `operate`, carry no token and never prompt.
"""
# Plugin hooks run with whatever `python3` is on PATH, which on macOS can be
# 3.9: annotations must not be evaluated at import, or the hook crashes and
# a crashed hook blocks nothing.
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "skills", "hpc-tether", "bin")

#: Each token, and what approving it lets through.
TOKENS = {
    "--allow-write": "change files or jobs on the cluster (inside your write roots)",
    "--allow-exec": "run code the agent wrote, which the guard cannot read",
    "--yes": "replace a file on the cluster (backed up first)",
    "--force": "overwrite a cluster file whose drift can't be checked",
    "--overwrite": "overwrite local files",
    "--merge": "merge into a local directory that already holds files",
    "--force-size": "move more than the 2 GiB per-transfer cap",
    "--budget-add": "raise this session's transfer budget",
}

#: Programs that run a heredoc body as commands.
_SHELLS = frozenset({"bash", "sh", "zsh", "ksh", "dash", "csh", "tcsh", "fish", "eval"})
_HEREDOC = re.compile(r"(?<!<)<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1(?!<)")
# Any `hpc-<something>` word, glob spellings included -- but not the name
# `hpc-tether` itself, which is the plugin's directory, not a tool: `cd
# ~/tools/hpc-tether && git push --force` asked for approval to "overwrite a
# cluster file".
_TOOL = re.compile(r"(?<![\w-])hpc-(?!tether(?![\w-]))[\w*?\[\]{}$-]")


def _executed_text(command: str) -> str:
    """``command`` with heredoc bodies that are only data removed.

    A heredoc fed to an hpc-tether verb is the payload: it runs on the
    cluster, and a `--force` in it belongs to the script it calls, not to
    the verb. A body is kept when the local shell would run it: the line
    that opens it names a shell (`bash <<EOF`), or the delimiter is unquoted
    and the body holds a substitution, which the shell expands.
    """
    lines = command.split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = _HEREDOC.search(line)
        if not m:
            i += 1
            continue
        end = next((j for j in range(i + 1, len(lines))
                    if lines[j].strip() == m.group(2)), None)
        if end is None:
            i += 1
            continue
        body = lines[i + 1:end]
        words = {w.rsplit("/", 1)[-1] for w in re.split(r"[\s|;&()'\"]+", line) if w}
        expands = not m.group(1) and any("$(" in b or "`" in b for b in body)
        if words & _SHELLS or expands:
            out.extend(body)
        out.append(lines[end])
        i = end + 1
    return "\n".join(out)


def _tokens_used(command: str) -> list[str]:
    """The tokens a command could be passing to an hpc-tether verb.

    This matches text; it does not parse. The parser it replaces looked for
    the verb at the head of each pipeline segment, and every wrapper it did
    not know was a silent pass: `bash -c 'hpc-run --allow-write -'`,
    `env -i hpc-push --yes f` and a heredoc fed to `bash` all ran with no
    prompt. Now any command whose text names
    an hpc-tether tool and a token, anywhere, asks. Quotes and backslashes
    are dropped first, so `--allow-"write"` is still the token. A false
    match (a grep for the token, a commit message that quotes one) costs a
    prompt; a miss costs a write nobody approved.
    """
    text = _executed_text(command)
    for ch in ("\\\n", "\\", "'", '"'):
        text = text.replace(ch, "")
    if not _TOOL.search(text):
        return []
    return [tok for tok in TOKENS
            if re.search(r"(?<![\w-])" + re.escape(tok) + r"(?![\w-])", text)]


# --- what the command will do, in words ------------------------------------
#
# The prompt shows the exact command, but a payload worth approving is often
# hard to read. These build one plain phrase to put above it. For hpc-run the
# phrase comes from the toolkit's own guard (`hpc-trust describe -`), not from
# anything the agent wrote about it. Best effort: when the payload can't be
# found in the command text, or the toolkit can't run, the prompt says so and
# still lists the tokens.

def _heredoc_payload(command: str) -> str | None:
    """The heredoc body fed to hpc-run, when the command spells it inline."""
    lines = command.split("\n")
    for i, line in enumerate(lines):
        if "hpc-run" not in line:
            continue
        m = _HEREDOC.search(line)
        if not m:
            continue
        end = next((j for j in range(i + 1, len(lines))
                    if lines[j].strip() == m.group(2)), None)
        if end is not None:
            return "\n".join(lines[i + 1:end]) + "\n"
    return None


_PRINTF = re.compile(r"""printf\s+(?:--\s+)?'([^']*)'\s*\|[^|]*hpc-run""")


def _printf_payload(command: str) -> str | None:
    """`printf 'a\\nb\\n' | hpc-run -`: the single-quoted text, \\n and \\t decoded.
    Anything with a % conversion is left alone: its output depends on args."""
    m = _PRINTF.search(command)
    if not m or "%" in m.group(1).replace("%%", ""):
        return None
    return m.group(1).replace("\\n", "\n").replace("\\t", "\t").replace("%%", "%")


_PRINTF_LINES = re.compile(r"""printf\s+(?:--\s+)?'%s\\n'((?:\s+'[^']*')+)\s*\|[^|]*hpc-run""")


def _printf_lines_payload(command: str) -> str | None:
    """`printf '%s\\n' 'line one' 'line two' | hpc-run -`: each single-quoted
    argument is one line, taken literally."""
    m = _PRINTF_LINES.search(command)
    if not m:
        return None
    return "".join(a + "\n" for a in re.findall(r"'([^']*)'", m.group(1)))


_ECHO = re.compile(r"""\becho\s+'([^']*)'\s*\|[^|]*hpc-run""")


def _echo_payload(command: str) -> str | None:
    """`echo 'mkdir ...' | hpc-run -`, the form agents write most: the
    single-quoted text plus echo's newline. No option (-e, -n) is matched, so
    no escape decoding is needed."""
    m = _ECHO.search(command)
    return m.group(1) + "\n" if m else None


def _site_arg(words: list) -> list:
    for i, w in enumerate(words):
        if w == "--site" and i + 1 < len(words):
            return ["--site", words[i + 1]]
        if w.startswith("--site="):
            return ["--site", w.split("=", 1)[1]]
    return []


def _words(command: str, verb: str) -> list:
    """The words of the first shell line naming ``verb``, from the verb on."""
    for line in command.split("\n"):
        if verb not in line:
            continue
        for seg in re.split(r"\|\||&&|;|\|", line):
            if verb in seg:
                try:
                    words = shlex.split(seg)
                except ValueError:
                    words = seg.split()
                for k, w in enumerate(words):
                    if w.rsplit("/", 1)[-1] == verb:
                        return words[k:]
    return []


def _operands(words: list) -> list:
    out, skip = [], False
    takes_value = {"--to", "--site", "--timeout", "--budget-add", "--exclude",
                   "--max-depth", "--max-files"}
    for w in words[1:]:
        if skip:
            skip = False
            continue
        if w in takes_value:
            skip = True
            continue
        if not w.startswith("-"):
            out.append(w)
    return out


def _value(words: list, name: str) -> str | None:
    for i, w in enumerate(words):
        if w == name and i + 1 < len(words):
            return words[i + 1]
        if w.startswith(name + "="):
            return w.split("=", 1)[1]
    return None


#: hpc-trust describe's marker for lines the guard refuses at every profile.
#: hpc-run then sends nothing at all (exit 77).
REFUSED = "REFUSED, never sent: "


def _all_refused(command: str, what: str) -> bool:
    """True when the one hpc-run payload in ``command`` will be refused whole,
    so no approval is needed. Only for a command with exactly
    one hpc-run and no other token-carrying verb: a second payload or a
    transfer would still need the prompt."""
    if command.count("hpc-run") != 1 or _describe_transfer(command):
        return False
    return "never sent: " in what


def _describe_run(command: str) -> str:
    payload = (_heredoc_payload(command) or _printf_payload(command)
               or _printf_lines_payload(command) or _echo_payload(command))
    if payload is None:
        return "runs a payload the hook can't read from the command line (see the command)"
    argv = [sys.executable, os.path.join(BIN, "hpc-trust")]
    argv += _site_arg(_words(command, "hpc-run")) + ["describe", "-"]
    try:
        p = subprocess.run(argv, input=payload, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=6)
    except (OSError, subprocess.TimeoutExpired):
        return "could not summarize the payload (see the command)"
    text = p.stdout.strip()
    if p.returncode != 0 or not text:
        return "could not summarize the payload (see the command)"
    if text.startswith(REFUSED):
        return "is refused by the guard, never sent: " + text[len(REFUSED):]
    return "will " + text


def _describe_transfer(command: str) -> list:
    out = []
    push = _words(command, "hpc-push")
    if push:
        ops = _operands(push)
        local = ops[0] if ops else "?"
        to = _value(push, "--to")
        if to:
            # --to may name a new file; the hook can't look
            line = ("will write local %s to %s (if a file is already there, "
                    "it is backed up first)" % (local, to))
        else:
            line = ("will replace the cluster file it was pulled from with local %s "
                    "(the cluster copy is backed up first)" % local)
        if "--force" in push:
            line += ", even though its drift can't be checked"
        out.append(line)
    for verb in ("hpc-pull", "hpc-fetch"):
        words = _words(command, verb)
        if not words:
            continue
        ops = _operands(words)
        src = ops[0] if ops else "?"
        dst = ops[1] if len(ops) > 1 else "the current directory"
        what = "will pull" if verb == "hpc-pull" else "will fetch directory"
        if "--overwrite" in words:
            out.append("%s %s into %s, overwriting local files" % (what, src, dst))
        elif "--merge" in words:
            out.append("%s %s into %s, merging with files already there" % (what, src, dst))
        elif "--force-size" in words:
            out.append("%s %s past the 2 GiB per-transfer cap" % (what, src))
        add = _value(words, "--budget-add")
        if add:
            out.append("will raise this session's transfer budget by %s for %s" % (add, src))
    return out


def describe(command: str) -> str:
    """One phrase for the prompt: what the command will do on the cluster."""
    parts = []
    if _words(command, "hpc-run"):
        parts.append(_describe_run(command))
    parts.extend(_describe_transfer(command))
    return "; ".join(parts)


def _decision(kind: str, reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": kind,
                                   "permissionDecisionReason": reason}}


def decide(payload: dict) -> dict | None:
    """The hook's decision for one PreToolUse event, or None to stay silent."""
    if payload.get("tool_name") != "Bash":
        return None
    command = (payload.get("tool_input") or {}).get("command", "") or ""
    used = _tokens_used(command)
    if not used:
        return None
    lines = [f"{tok}: {TOKENS[tok]}" for tok in used]
    try:
        what = describe(command)
    except Exception:  # a summary is a courtesy; the prompt must still appear
        what = ""
    if payload.get("permission_mode") == "bypassPermissions":
        # The generic token label said "replace a file" for a
        # push that would have created one; lead with the specific phrase.
        head = ("hpc-tether: this %s. " % what) if what else "hpc-tether: "
        return _decision("deny", (
            head + "That needs a person's approval, and bypassPermissions "
            "mode skips the prompt that asks for it, so it is refused ("
            + ", ".join(used)
            + "). Leave bypass mode to run it, or run the command yourself."))
    if what and _all_refused(command, what):
        return None  # the guard refuses it locally and sends nothing
    head = ("hpc-tether: this %s.\n" % what) if what else ""
    return _decision("ask", head + "Approving " + "; ".join(lines))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    result = decide(payload)
    if result is not None:
        json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
