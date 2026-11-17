#!/usr/bin/env python3
"""PreToolUse hook: refuse a raw ssh/scp/sftp/rsync command aimed at an HPC site.

The persistent ControlMaster socket is usable by any process running as the
local user, which includes every Bash tool call in every Claude Code session
and subagent. The hpc-tether skill says "never hand-roll ssh" -- but a rule
the model has to remember is not a control. This hook is the control: the
harness runs it before every Bash command, and a command that invokes a
transport program directly is blocked before it runs, with the toolkit verb
named in its place.

What it blocks: a command whose verb, in any pipeline segment, is one of
ssh, slogin, scp, sftp, rsync, sshfs, or `ssh -O` -- including behind env
assignments, `command`, `env`, `nohup`, `timeout` and similar prefixes --
AND whose arguments name an HPC host. HPC hosts are the `host` of every site
file in ~/.config/hpc-tether/sites/, expanded through ~/.ssh/config: the
Host aliases and HostName of a stanza that reaches a site, and the hosts its
ProxyCommand/ProxyJump goes through (a cluster's login or gateway host).

Scoped to sites: ssh to hosts that are not HPC sites (workstations,
laptops, git servers) is ordinary work, and a hook that blocked every ssh
stopped it. It still fails closed: with no readable site file it
blocks every transport, as before, and a transport whose target is a shell
variable (`ssh $H ls`, `scp f $DEST`) is blocked because it is not visible.
Only the words that can name the target are read: options and the host for
ssh, the host part of `host:path` for scp and rsync. A `$` in a remote
command or a local path, and a directory that shares a site's name, pass.

What it does not block, by design:
  * the hpc-tether verbs (`bin/hpc-run`, `hpc-pull`, ...). They invoke ssh
    from Python via subprocess, which is not a Bash tool command line.
  * a script that shells out to ssh (`python3 fetch.py`). The command line
    says `python3`, not `ssh`. Such a script is interpreter-tier work and the
    skill governs it; use `hpc-fetch` for directory pulls instead.
  * `ssh` appearing as an argument, not a verb: `grep ssh log`, `man ssh`.

This is a steering hook, not a boundary. It reads command text, and a Bash
call can spell ssh in ways text does not show (a multiline `$(...)`, a
variable, a script). The ControlMaster socket is reachable from the agent's
own shell whatever this hook says; see "Known limits" in
skills/hpc-tether/reference/design.md. The literal body of `SHELL -c '...'`
is read as a command line.

Exit 0 always. A block is a JSON deny: its reason goes back to the model,
and a systemMessage tells the user what the agent tried.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python < 3.11 (macOS /usr/bin/python3): read `host` by regex.
    tomllib = None

HOST_LINE = re.compile(r'^\s*host\s*=\s*["\']([^"\']+)["\']', re.M)

TRANSPORTS = frozenset({"ssh", "slogin", "scp", "sftp", "rsync", "sshfs", "mosh"})

# Prefixes that introduce a command without being one.
PREFIXES = frozenset(
    {"command", "env", "nohup", "nice", "time", "timeout", "stdbuf", "exec",
     "setsid", "ionice", "caffeinate", "sudo", "doas", "builtin",
     "if", "then", "else", "elif", "while", "until", "do", "for", "{", "(", "!"}
)
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
DURATION = re.compile(r"^\d+(\.\d+)?[smhd]?$")

# Split on command separators. Quoted spans are blanked first: a `|` inside a
# grep pattern is not a pipe, and `'... | rsync = ...'` inside quotes made
# `rsync` look like a verb (it blocked a grep over this repo's own sources).
# A quoted string is never a verb in practice, so blanking only ever removes
# false positives; backticks are kept as separators because `cmd` is real
# substitution, not quoting.
SPLIT = re.compile(r"\|\||&&|;|\||\n|\$\(|<\(|>\(|`")
QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")


BARE_WORD = re.compile(r"[A-Za-z0-9_./-]*")
# One double-quoted argument with no space in it: `"$H"`, `"$HOME/out/"`,
# `"u@host:/p"`. Kept, unquoted, so the target check can see a variable.
DQ_WORD = re.compile(r"[A-Za-z0-9_./@:${}~+,=%-]*")
ESCAPED_CHAR = re.compile(r"\\([A-Za-z0-9])")
ESCAPED_SEP = re.compile(r"\\[|;&]")


def _substitutions_only(text: str) -> str:
    """``text`` blanked, except command substitutions, which the shell runs
    wherever they sit: `$(...)` to its closing paren, and backtick pairs."""
    out = [" "] * len(text)
    i = 0
    while i < len(text):
        if text.startswith("$(", i):
            depth, j = 0, i + 1
            while j < len(text):
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            out[i:j + 1] = text[i:j + 1]
            i = j + 1
        elif text[i] == "`":
            j = text.find("`", i + 1)
            j = len(text) - 1 if j < 0 else j
            out[i:j + 1] = text[i:j + 1]
            i = j + 1
        else:
            i += 1
    return "".join(out)


def _blank_quotes(command: str) -> str:
    """Remove quoted text that can't be a command, and keep what can.

    * A quoted single word is unquoted, not blanked: `'ssh' host ls` runs
      ssh, and so does `s"s"h host ls`. A double-quoted one may hold a
      variable (`ssh "$H" ls`), which the target check must see.
    * A substitution inside double quotes still runs: `echo "$(ssh h ls)"`.
    * Everything else in quotes is blanked, as before.
    * `s\\sh` is ssh: a backslash before a letter or digit is dropped.

    All three passed the hook unseen.
    """
    def swap(m):
        span = m.group(0)
        inner = span[1:-1]
        if BARE_WORD.fullmatch(inner):
            return inner
        if span[0] == '"':
            if DQ_WORD.fullmatch(inner):
                return inner
            return " " + _substitutions_only(inner) + " "
        return " " * len(span)

    text = QUOTED.sub(swap, command)
    # An escaped separator is a literal character in an argument, not a
    # separator: `grep a\|b f` is one command.
    text = ESCAPED_SEP.sub("_", text)
    return ESCAPED_CHAR.sub(r"\1", text)


# A heredoc opener: `<<EOF`, `<<-'EOF'`, `<< "EOF"`; not the here-string `<<<`.
HEREDOC = re.compile(r"(?<!<)<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1(?!<)")
# Programs that run a heredoc body as commands.
SHELLS = frozenset({"bash", "sh", "zsh", "ksh", "dash", "csh", "tcsh", "fish", "eval"})


def _blank_data_heredocs(command: str) -> str:
    """Blank heredoc bodies that are data, so prose in them is not read as commands.

    A commit message fed by `git commit -F - <<EOF`, with a line starting
    `ssh -O check`, was blocked as a raw ssh. Blanking the body
    also stops an apostrophe in the prose from pairing with a quote further
    on and exposing text that was quoted. A body is data unless the line that
    opens it runs a shell (`bash <<EOF`, `cat <<EOF | sh`): that body runs,
    so it is still scanned. An unterminated heredoc keeps its body.

    A data body under an unquoted delimiter is still expanded by the shell,
    so its substitutions run: `cat <<EOF` with a `$(ssh host ls)` line ran
    ssh. Those are kept; the prose around
    them is blanked.
    """
    lines = command.split("\n")
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = HEREDOC.search(line)
        if not m:
            i += 1
            continue
        delim = m.group(2)
        words = {w.rsplit("/", 1)[-1]
                 for w in re.split(r"[\s|;&()]+", _blank_quotes(line)) if w}
        end = next((j for j in range(i + 1, len(lines)) if lines[j].strip() == delim), None)
        if end is None:
            i += 1
            continue
        body = lines[i + 1:end]
        if words & SHELLS:
            out.extend(body)
        elif not m.group(1):
            out.extend(_substitutions_only(b) for b in body)
        else:
            out.extend([""] * len(body))
        out.append(lines[end])
        i = end + 1
    return "\n".join(out)


# `env` options that take a value as the next token: `env -u NAME ssh host`
# read NAME as the verb.
ENV_VALUE_FLAGS = frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"})


def _skip_prefixes(toks) -> int:
    """Index of the first token that is not a prefix of the command."""
    i, in_env = 0, False
    while i < len(toks):
        t = toks[i]
        if t in PREFIXES or ASSIGN.match(t) or t == "--" or DURATION.match(t):
            in_env = in_env or t == "env"
            i += 1
            continue
        if t.startswith("-"):
            i += 2 if in_env and t in ENV_VALUE_FLAGS else 1
            continue
        break
    return i


# `bash -c 'body'`, `sh -lc "body"`, `bash -e -c '...'`: the body is a command
# line the shell runs, and quoting hides it from the scan below.
SHELL_C = re.compile(
    r"(?<![\w./-])(?:[\w./-]*/)?(?:bash|sh|zsh|ksh|dash)"
    r"(?:\s+--?[A-Za-z][\w-]*)*?\s+-[A-Za-z]*c[A-Za-z]*\s+"
    r"('[^']*'|\"(?:[^\"\\]|\\.)*\")")


def _shell_bodies(text: str):
    """The literal bodies of `SHELL -c '...'` commands in ``text``.

    The shell word must be in command position: only prefixes (`env`,
    `timeout 5`, an assignment) may precede it in its segment, so `git commit
    -m "use bash -c 'ssh host ls'"` is not a hit.
    """
    for m in SHELL_C.finditer(text):
        before = SPLIT.split(text[:m.start()])[-1].split()
        if _skip_prefixes(before) != len(before):
            continue
        body = m.group(1)[1:-1]
        if m.group(1)[0] == '"':
            body = re.sub(r"\\([\"\\$`])", r"\1", body)
        yield body


def commands(command: str):
    """Yield (verb, args) for each command in ``command``."""
    text = _blank_data_heredocs(command)
    for body in _shell_bodies(text):
        yield from commands(body)
    for seg in SPLIT.split(_blank_quotes(text)):
        toks = seg.strip().split()
        i = _skip_prefixes(toks)
        # A verb with no arguments is not a remote command: `ssh` alone
        # is an interactive prompt the deny list already covers, and a
        # bare `ssh` in backticked prose inside a heredoc is the common
        # false positive (it tripped on this repo's own docs).
        if i + 1 < len(toks):
            yield toks[i].rsplit("/", 1)[-1], toks[i + 1:]


def verbs(command: str):
    for verb, _ in commands(command):
        yield verb


# ---------------------------------------------------------------- HPC hosts

def _sites_dir() -> Path:
    override = os.environ.get("HPC_TETHER_SITES")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "hpc-tether" / "sites"


def _ssh_config() -> Path:
    override = os.environ.get("HPC_TETHER_SSH_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".ssh" / "config"


def site_hosts() -> set[str] | None:
    """The `host` of each site file, lowercased; None when none is readable."""
    hosts = set()
    try:
        files = sorted(_sites_dir().glob("*.toml"))
    except OSError:
        return None
    for f in files:
        if f.name.endswith(".example.toml") or f.name.endswith(".trust.toml"):
            continue
        try:
            text = f.read_text()
            if tomllib is not None:
                host = tomllib.loads(text).get("host")
            else:
                m = HOST_LINE.search(text.split("\n[", 1)[0])
                host = m.group(1) if m else None
        except (OSError, ValueError):
            continue
        if isinstance(host, str) and host.strip():
            hosts.add(host.strip().lower())
    return hosts or None


def _bare_host(word: str) -> str:
    """`user@host:port` -> `host`."""
    return word.rsplit("@", 1)[-1].split(":", 1)[0].lower()


def _stanzas(path: Path):
    """(patterns, hostname, proxy hosts) per `Host` stanza of an ssh config."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out, cur = [], None
    for line in lines:
        parts = line.strip().split(None, 1)
        if not parts or parts[0].startswith("#"):
            continue
        key = parts[0].lower().rstrip("=")
        val = parts[1].strip().lstrip("=").strip() if len(parts) > 1 else ""
        if key in ("host", "match"):
            cur = ({p.lower() for p in val.split()} if key == "host" else set(), None, set())
            out.append(cur)
        elif cur is None:
            continue
        elif key == "hostname":
            cur = (cur[0], val.lower(), cur[2])
            out[-1] = cur
        elif key == "proxyjump":
            cur[2].update(_bare_host(w) for w in val.split(",") if w and w != "none")
        elif key == "proxycommand":
            cur[2].update(_bare_host(w) for w in val.split()
                          if "." in w and not w.startswith(("-", "%")))
    return out


def hpc_hosts() -> set[str] | None:
    """Site hosts plus every ssh alias, HostName and proxy that reaches them."""
    hosts = site_hosts()
    if hosts is None:
        return None
    stanzas = _stanzas(_ssh_config())
    changed = True
    while changed:
        changed = False
        for patterns, hostname, proxies in stanzas:
            literal = {p for p in patterns if not any(c in p for c in "*?!")}
            if literal & hosts or (hostname and hostname in hosts):
                new = literal | proxies | ({hostname} if hostname else set())
                if not new <= hosts:
                    hosts |= new
                    changed = True
    return hosts


TARGET_SPLIT = re.compile(r"[=,@:/\[\]]")

# ssh options that take a value as the next word (`-p 22`, `-J gate`).
SSH_VALUE_FLAGS = frozenset("BbcDEeFIiJLlmOoPpQRSWw")
SSH_LIKE = frozenset({"ssh", "slogin", "mosh"})
# For the file transports: options whose value can name a host or carry an
# ssh command line. (rsync's -o and -S are --owner and --sparse: no value.)
HOST_FLAGS = {
    "scp": frozenset({"-J", "-o", "-S"}),
    "sftp": frozenset({"-J", "-o", "-S"}),
    "sshfs": frozenset({"-o"}),
    "rsync": frozenset({"-e"}),
}


def _variable(word: str) -> bool:
    return "$" in word or "`" in word


def _names_host(word: str, hosts) -> bool:
    """An option or its value: any piece of it is an HPC host
    (`ProxyJump=gate`, `u@gate:22`, `-oHostName=host`)."""
    return any(p in hosts for p in TARGET_SPLIT.split(word.lower()) if p)


def _operand_hit(word: str, hosts) -> bool:
    """A file transport's operand: `host:path`, `u@host:path`, `scheme://host/`
    or a bare word. Only its host part counts. A word with a `/` before any
    `:` is a local path, so `./cluster/` and `laptop:cluster/` are not the
    site `cluster` (both were once blocked). A variable in the
    host part, or a bare `$SRC` that could hold `host:path`, is unreadable."""
    if "://" in word:
        head = word.split("://", 1)[1].split("/", 1)[0]
    else:
        head = word.split(":", 1)[0]
        if "/" in head:
            return False
    if _variable(head):
        return True
    return head.rsplit("@", 1)[-1].split(":", 1)[0].strip("[]").lower() in hosts


def _ssh_hit(args, hosts) -> bool:
    """`ssh [options] host [command...]`: the options and the host are read;
    in the remote command only a nested transport's own words are, so
    `ssh workstation "ls $HOME"` and `ssh workstation ls cluster/` pass."""
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-") and len(a) > 1:
            if _variable(a) or _names_host(a, hosts):
                return True
            if len(a) == 2 and a[1] in SSH_VALUE_FLAGS and i + 1 < len(args):
                v = args[i + 1]
                if _variable(v) or _names_host(v, hosts):
                    return True
                i += 1
            i += 1
            continue
        if _variable(a) or _names_host(a, hosts):
            return True
        rest = args[i + 1:]
        if any(w.rsplit("/", 1)[-1] in TRANSPORTS for w in rest):
            return any(_operand_hit(w, hosts) for w in rest if not w.startswith("-"))
        return False
    return False


def _transfer_hit(verb: str, args, hosts) -> bool:
    flags = HOST_FLAGS.get(verb, frozenset())
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-") and len(a) > 1:
            if _names_host(a, hosts):
                return True
            if a[:2] in flags or a.startswith("--rsh"):
                if _variable(a):
                    return True
                if len(a) == 2 and i + 1 < len(args):
                    v = args[i + 1]
                    if _variable(v) or _names_host(v, hosts):
                        return True
                    i += 1
        elif _operand_hit(a, hosts):
            return True
        i += 1
    return False


def hpc_target(command: str, verb: str, args, hosts) -> bool:
    """Whether a transport command may reach an HPC host.

    Only the words that can name the target are read: a `$` elsewhere in the
    command (a remote command's `$HOME`, a local `"$HOME/x/"` path) used to
    block ssh between the user's own machines."""
    if hosts is None:
        return True
    if verb in SSH_LIKE:
        return _ssh_hit(args, hosts)
    return _transfer_hit(verb, args, hosts)



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


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command", "") or ""
    transports = [(v, a) for v, a in commands(command) if v in TRANSPORTS]
    if not transports:
        return 0
    hosts = hpc_hosts()
    hit = next((v for v, a in transports if hpc_target(command, v, a, hosts)), None)
    if hit is None:
        return 0
    return deny(
        f"blocked: raw `{hit}` to an HPC site (or a target the hook cannot read)\n"
        "from the Bash tool. Hosts without a site file are not blocked;\n"
        "write the target host literally.\n"
        "All cluster contact goes through the hpc-tether verbs, which ride the\n"
        "same ControlMaster with a payload guard, a remote watchdog, and an audit\n"
        "record: bin/hpc-check, hpc-read, hpc-ls, hpc-jobs, hpc-probe, hpc-nc,\n"
        "hpc-pull, hpc-fetch, hpc-push, hpc-sync-status, or `... | bin/hpc-run -`.\n"
        "If none fits, print the exact command for the user to run by hand.\n"
        "(hook: hpc-tether/hooks/block_raw_ssh.py)\n",
        f"hpc-tether ▸ BLOCKED: the agent tried raw `{hit}`; nothing ran. "
        "It was told to use the hpc-* tools instead.")


if __name__ == "__main__":
    sys.exit(main())
