#!/usr/bin/env python3
"""PreToolUse hook: the agent never writes the hpc-tether site files.

The site file (``~/.config/hpc-tether/sites/<name>.toml``) is where the user
sets what the agent may do on the cluster: ``[policy]`` (profile, write
roots, protected paths) and which ``[repos]`` are trusted. An agent that could edit it could widen its own
permissions, so SKILL.md says never to. A rule the model has to remember is
not a control; this hook is.

What it blocks:
  * Edit, Write, MultiEdit and NotebookEdit on any path under the config
    directory (``$HPC_TETHER_SITES`` too, when set), symlinks resolved.
  * A Bash command that writes there in a spelling it can read: a redirect
    (``>``, ``>>``) to such a path; ``tee``, ``touch``, ``truncate``, ``rm``,
    ``unlink``, ``chmod`` naming one; ``cp``/``mv``/``ln``/``install`` whose
    target is one; ``sed -i``/``perl -i`` on one; ``dd of=``; and any of these
    after a ``cd`` into the directory.

  * A Bash command that sets ``HPC_TETHER_SITES``, ``HPC_TETHER_AUDIT`` or
    ``HPC_TETHER_MANIFEST``: those point the toolkit at other site files,
    another audit log, another manifest.

What it does not block, stated plainly: reading (``cat``, ``grep``, copying a
site file *out*), and writes it can't read -- a Python one-liner, a script, a
variable holding the path. It catches habit and drift, not a determined
workaround; the skill's rule still applies.

Exit 0 always. A block is a JSON deny: its reason goes back to the model,
and a systemMessage tells the user what the agent tried.
Must run under any python3 (macOS system 3.9): no 3.10+ syntax.
"""
import json
import os
import re
import sys

EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
ANY_OPERAND = frozenset({"tee", "touch", "truncate", "rm", "unlink", "chmod", "chown",
                         "shred", "mkdir"})
LAST_OPERAND = frozenset({"cp", "mv", "ln", "install", "rsync"})
IN_PLACE = frozenset({"sed", "perl", "gsed"})
PREFIXES = frozenset({"command", "env", "nohup", "nice", "time", "sudo", "exec", "builtin"})
SPLIT = re.compile(r"\|\||&&|;|\||\n|\$\(|`")

MESSAGE = (
    "blocked: {what}.\n"
    "The hpc-tether site files set what the agent may do on the cluster\n"
    "([policy] and which [repos] are trusted), so only the user edits\n"
    "them. Print the exact change -- the lines to add, and the file -- for the\n"
    "user to make in their own editor.\n"
    "(hook: hpc-tether/hooks/protect_site_file.py)\n"
)


#: Environment variables that point the toolkit at other files: site files
#: (the profile and roots), the audit log, the pull manifest. An agent that
#: sets one swaps in its own: `HPC_TETHER_SITES=/tmp/x hpc-run -` ran under
#: a site file the agent wrote, at any profile it chose.
_OVERRIDES = r"HPC_TETHER_(?:SITES|AUDIT|MANIFEST)"
_OVERRIDE_SET = re.compile(r"(?<![$\w{])(" + _OVERRIDES + r")\+?=")
_OVERRIDE_BUILTIN = re.compile(
    r"\b(?:read|declare|typeset|local|setenv|printf\s+-v)\b[^;|&\n]*?(?<![$\w{])("
    + _OVERRIDES + r")\b")

OVERRIDE_MESSAGE = (
    "blocked: this command sets {name}.\n"
    "That variable points hpc-tether at different site files, audit log or\n"
    "manifest, which would replace the profile and roots the user chose or\n"
    "hide what was sent. Only the user sets it, in their own shell. Run the\n"
    "command without it.\n"
    "(hook: hpc-tether/hooks/protect_site_file.py)\n"
)


def override_set(command):
    """The override variable a Bash command sets, or None. Quotes and
    backslashes are dropped first: `env "HPC_TETHER_SITES=x"` is the same."""
    text = command
    for ch in ("\\\n", "\\", "'", '"'):
        text = text.replace(ch, "")
    m = _OVERRIDE_SET.search(text) or _OVERRIDE_BUILTIN.search(text)
    return m.group(1) if m else None


def protected_dirs():
    home = os.path.expanduser("~")
    dirs = [os.path.join(home, ".config", "hpc-tether")]
    override = os.environ.get("HPC_TETHER_SITES")
    if override:
        dirs.append(os.path.expanduser(override))
    out = []
    for d in dirs:
        out.append(os.path.normpath(d))
        out.append(os.path.realpath(d))
    return sorted(set(out))


def _under(path, dirs):
    p = os.path.realpath(os.path.expanduser(path))
    n = os.path.normpath(os.path.expanduser(path))
    return any(x == d or x.startswith(d + os.sep) for x in (p, n) for d in dirs)


def _mentions(token, dirs):
    """True if a shell word names a path under a protected directory."""
    t = token.strip("'\"")
    home = os.path.expanduser("~")
    for prefix in ("$HOME", "${HOME}", "~"):
        if t == prefix or t.startswith(prefix + "/"):
            t = home + t[len(prefix):]
            break
    if ".config/hpc-tether" in t:
        return True
    return t.startswith("/") and _under(t, dirs)


def _words(segment):
    # Separate redirect operators from their targets: `>x` and `> x` alike.
    seg = re.sub(r"(\d?>>?|&>)", r" \1 ", segment)
    return seg.split()


def _in_place_flag(verb, arg):
    """sed: -i, -i.bak, --in-place. perl: an -i anywhere in a short-flag
    cluster (-pi, -pie, -i.bak)."""
    if arg.startswith("--in-place"):
        return True
    if not arg.startswith("-") or arg.startswith("--"):
        return False
    if verb == "perl":
        return "i" in arg[1:].split(".")[0]
    return arg.startswith("-i")


def bash_write(command, dirs):
    """The first write into a protected directory this command spells, or None."""
    in_dir = False
    for seg in SPLIT.split(command):
        words = _words(seg)
        if not words:
            continue
        # Redirects anywhere in the segment.
        for i, w in enumerate(words[:-1]):
            if not re.fullmatch(r"\d?>>?|&>", w):
                continue
            target = words[i + 1]
            if _mentions(target, dirs):
                return f"a redirect to {target}"
            if in_dir and not target.startswith(("/", "~", "$", "&")):
                return f"a redirect into {target} after cd into the site directory"
        i = 0
        while i < len(words) and (words[i] in PREFIXES or "=" in words[i].split("/")[0]):
            i += 1
        if i >= len(words):
            continue
        verb = words[i].rsplit("/", 1)[-1]
        args = [a for a in words[i + 1:] if not re.fullmatch(r"\d?>>?|&>", a)]
        operands = [a for a in args if not a.startswith("-")]
        if verb == "cd":
            in_dir = bool(operands) and _mentions(operands[0], dirs)
            continue
        relative_hit = in_dir and any(not o.startswith(("/", "~", "$")) for o in operands)
        if verb in ANY_OPERAND and (any(_mentions(o, dirs) for o in operands) or relative_hit):
            return f"`{verb}` on the site directory"
        if verb in LAST_OPERAND and operands and (
                _mentions(operands[-1], dirs) or (in_dir and not operands[-1].startswith(("/", "~", "$")))):
            return f"`{verb}` into {operands[-1]}"
        if verb in IN_PLACE and any(_in_place_flag(verb, a) for a in args) and (
                any(_mentions(o, dirs) for o in operands) or relative_hit):
            return f"`{verb} -i` on the site directory"
        if verb == "dd" and any(a.startswith("of=") and _mentions(a[3:], dirs) for a in args):
            return "`dd of=` into the site directory"
    return None



def deny(reason, notice):
    """Block the call: ``reason`` goes to the agent, ``notice`` to the user.

    A JSON deny rather than exit 2, so the user gets a line of their own
    (systemMessage) instead of only the agent-facing text in the tool row.
    """
    json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                      "permissionDecision": "deny",
                                      "permissionDecisionReason": reason},
               "systemMessage": notice}, sys.stdout)
    return 0


def main():
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    dirs = protected_dirs()
    what = None
    if tool in EDIT_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if path and _under(path, dirs):
            what = f"{tool} on {path}"
    elif tool == "Bash":
        command = tool_input.get("command", "") or ""
        name = override_set(command)
        if name:
            return deny(OVERRIDE_MESSAGE.format(name=name),
                        "hpc-tether ▸ BLOCKED: the agent tried to set %s, which would "
                        "redirect hpc-tether's settings or audit log; nothing ran." % name)
        what = bash_write(command, dirs)
    if what is None:
        return 0
    return deny(MESSAGE.format(what=what),
                "hpc-tether ▸ BLOCKED: the agent tried to change your hpc-tether "
                "settings (%s); nothing ran. Only you edit them." % what)


if __name__ == "__main__":
    sys.exit(main())
