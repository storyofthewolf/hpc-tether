#!/usr/bin/env python3
"""SessionStart and PostToolUse hook, and the status bar: show the user what
hpc-tether did.

Under Claude Code the toolkit is invisible: the user sees the agent's Bash
calls and the agent's account of them, never the toolkit's own. That is fine
once the user trusts it, and no help while they are deciding whether to.
This hook speaks to the user directly (``systemMessage``), from the audit log
rather than from anything the agent says.

  * SessionStart: silent. A session that never uses the cluster sees
    nothing of the plugin. It records where the plugin lives (for the status
    bar) and starts this session's receipts from now.
  * First use in a session (the skill is invoked, or the first cluster
    command runs): the status, once, one line per site: profile,
    connection, receipt setting. With no site file, that setup is unfinished.
  * PostToolUse / PostToolUseFailure on Bash: one receipt line per audit
    entry this session wrote since the last receipt: what was sent, whether
    it was a read or a write, which approval it carried, how it ended.

How much is shown is the user's setting, in
``~/.config/hpc-tether/config.toml``:

    receipts = "all"       # every cluster contact (the default)
    receipts = "notable"   # writes, approvals, trusted scripts, transfers,
                           # refusals and failures; no plain reads
    receipts = "off"       # nothing: no status, no receipts, no status bar

The status bar (``--statusline``) shows the same status line for the rest of a
session once the plugin has been used in it, and nothing before. It is opt-in:
a ``statusLine`` entry in the user's settings.json (README, "Seeing it work").

The file sits in the directory protect_site_file.py keeps the agent out of,
so the agent can't turn its own receipts off.

The hook never blocks and never fails a tool call: any error is swallowed,
and the worst case is a missing receipt. The audit log stays the record.
Must run under any python3 (macOS system 3.9): no 3.10+ syntax, no tomllib.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(os.path.dirname(HOOK_DIR), "skills", "hpc-tether", "bin")

SETTINGS = ("all", "notable", "off")
DEFAULT = "all"
PREFIX = "hpc-tether ▸ "

#: Verbs that send nothing to the cluster; a Bash call naming only these
#: needs no look at the log.
_TOOL = re.compile(r"\bhpc-(?:run|read|jobs|ls|nc|probe|pull|fetch|push|sync-status)\b")

#: How long the status bar reuses a site's connection check, in seconds.
STATUS_TTL = 15


# --- where things are ------------------------------------------------------

def config_dir():
    return os.path.join(os.path.expanduser("~"), ".config", "hpc-tether")


def sites_dir():
    override = os.environ.get("HPC_TETHER_SITES")
    if override:
        return os.path.expanduser(override)
    return os.path.join(config_dir(), "sites")


def audit_dir():
    override = os.environ.get("HPC_TETHER_AUDIT")
    if override:
        path = os.path.expanduser(override)
        return os.path.dirname(path) if path.endswith(".jsonl") else path
    return os.path.join(os.path.expanduser("~"), ".local", "state", "hpc-tether")


def audit_file():
    """The log the toolkit is writing to now (see hpctether/audit.py)."""
    override = os.environ.get("HPC_TETHER_AUDIT")
    if override and override.endswith(".jsonl"):
        return os.path.expanduser(override)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    return os.path.join(audit_dir(), "audit-%s.jsonl" % month)


def cursor_file(session):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session) or "none"
    return os.path.join(audit_dir(), "receipts", safe + ".json")


# --- the user's setting ----------------------------------------------------

def setting():
    """``receipts`` from config.toml; the default when absent or unreadable.

    One key, read with a regex: the hook runs under python 3.9, which has no
    tomllib. An unknown value falls back to the default, so a typo shows more,
    never less.
    """
    try:
        with open(os.path.join(config_dir(), "config.toml")) as fh:
            text = fh.read()
    except OSError:
        return DEFAULT
    m = re.search(r'^\s*receipts\s*=\s*["\']([a-z]+)["\']', text, re.M)
    return m.group(1) if m and m.group(1) in SETTINGS else DEFAULT


# --- cursor: where this session's receipts got to --------------------------

def read_cursor(session):
    try:
        with open(cursor_file(session)) as fh:
            c = json.load(fh)
        return c.get("path"), int(c.get("offset", 0))
    except (OSError, ValueError, TypeError):
        return None, 0


def write_cursor(session, path, offset):
    target = cursor_file(session)
    try:
        os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
        tmp = target + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"path": path, "offset": offset}, fh)
        os.replace(tmp, target)
    except OSError:
        pass


def new_entries(session):
    """This session's audit entries since the last receipt, and advance.

    With no cursor (the plugin enabled mid-session, or the start hook never
    ran) the whole month's file is read, still filtered to this session.
    """
    path = audit_file()
    prev_path, offset = read_cursor(session)
    if prev_path != path:
        offset = 0
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if offset > size:  # file replaced or truncated by hand
                offset = 0
            fh.seek(offset)
            data = fh.read()
    except OSError:
        return []
    # Only whole lines: a line still being written is read next time.
    end = data.rfind(b"\n") + 1
    write_cursor(session, path, offset + end)
    out = []
    for raw in data[:end].splitlines():
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if entry.get("session") == session:
            out.append(entry)
    return out


# --- one entry, one line ---------------------------------------------------

def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024
    return "%d B" % n


def clip(text, width=90):
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[:width - 1] + "…"


def short_path(word, width=40):
    """``archive:a/b/c/file.nc`` -> ``archive:…/file.nc`` when long.

    The file name and the arguments after it are what tell one call from the
    next (``hpc-nc stats …/x.nc TS``); the directories rarely are.
    """
    if len(word) <= width or "/" not in word:
        return word
    role, sep, rest = word.partition(":")
    if not sep or "/" in role:
        role, rest = "", word
    return (role + ":" if role else "") + "…/" + rest.rstrip("/").rsplit("/", 1)[-1]


def _prelude(line):
    """The `export PATH="$HPC_PYTHON_BIN:$PATH"` line python payloads open with."""
    s = line.strip()
    return s.startswith("export PATH=") and "HPC_PYTHON_BIN" in s


def what(entry):
    """The command as the agent asked for it."""
    verb = entry.get("verb", "")
    if verb == "hpc-run":
        body = entry.get("body") or ""
        lines = [l.strip() for l in body.splitlines()
                 if l.strip() and not l.strip().startswith("#") and not _prelude(l)]
        first = entry.get("detail", "")
        # Entries written before the toolkit skipped the PATH line carry it
        # as `detail`; a refusal's detail is the refused line and is kept.
        if _prelude(first) or not first:
            first = lines[0] if lines else first
        more = len(lines) - 1
        return "hpc-run: " + clip(first, 70) + (" (+%d lines)" % more if more > 0 else "")
    argv = " ".join(short_path(a) for a in entry.get("argv", "").split())
    return clip(("%s %s" % (verb, argv)).strip())


def outcome(entry):
    rc = entry.get("rc")
    secs = entry.get("seconds")
    took = (" %.1fs" % secs) if isinstance(secs, (int, float)) else ""
    if rc == 0:
        return "ok" + took
    if rc == 124:
        return "TIMED OUT" + took
    if rc is None:
        return "no exit code"
    # The toolkit's own remote stops (otherwise shown as "FAILED rc 79").
    if rc == 79:
        return "STOPPED by the job-name check (already queued or running, or squeue failed); nothing submitted" + took
    if rc == 78:
        return "STOPPED, a script root is not a clean checkout; nothing ran" + took
    return "FAILED rc %s%s" % (rc, took)


def nature(entry):
    """read / write / trusted script / transfer, and the approval it carried."""
    kind = entry.get("kind")
    verb = entry.get("verb", "")
    level = entry.get("level")
    if entry.get("trusted"):
        names = ", ".join(os.path.basename(t.get("path", "")) + (" " + t["args"] if t.get("args") else "")
                          for t in entry["trusted"])
        label = "trusted script %s" % clip(names, 50)
    elif verb == "hpc-push" and entry.get("step") == "precheck":
        label = "read (push precheck)"
    elif verb == "hpc-push" or level in ("prepare",):
        label = "write"
    elif level == "operate":
        label = "job control"
    elif level == "improvise":
        label = "agent-written code"
    else:
        label = "read"
    tokens = entry.get("tokens") or []
    if tokens:
        label += ", approved " + " ".join(tokens)
    return label


def receipt(entry):
    site = entry.get("site", "?")
    kind = entry.get("kind")
    if kind == "refused":
        level = entry.get("level")
        if entry.get("refusal"):
            need = " (%s)" % entry["refusal"]
        else:
            need = (" (needs profile %s)" % level) if level else ""
        if entry.get("verb") == "hpc-push":
            # its git and drift checks are a read; no file goes out
            return "%s%s · REFUSED, nothing written%s · %s" % (PREFIX, site, need, what(entry))
        return "%s%s · REFUSED, nothing sent%s · %s" % (PREFIX, site, need, what(entry) if entry.get("verb") == "hpc-run" else clip(entry.get("detail", "")))
    if kind == "budget":
        return "%s%s · transfer budget raised, approved: %s" % (PREFIX, site, entry.get("detail", ""))
    if kind in ("scp", "rsync"):
        direction = entry.get("direction")
        if entry.get("rc") != 0 or not direction:
            return "%s%s · %s %s · %s" % (PREFIX, site, kind, clip(entry.get("detail", "")), outcome(entry))
        files = entry.get("files", 0)
        size = human(entry.get("bytes", 0))
        noun = "file" if files == 1 else "files"
        if direction == "in":
            move = "pulled %d %s, %s ← %s" % (files, noun, size, short_path(entry.get("remote", "")))
        else:
            move = "PUSHED %d %s, %s → %s" % (files, noun, size, short_path(entry.get("remote", "")))
        tokens = entry.get("tokens") or []
        approved = (", approved " + " ".join(tokens)) if tokens else ""
        return "%s%s · %s%s · %s" % (PREFIX, site, move, approved, outcome(entry))
    return "%s%s · %s · %s · %s" % (PREFIX, site, nature(entry), what(entry), outcome(entry))


def notable(entry):
    """Worth a line at receipts = "notable": anything but a plain, clean read."""
    if entry.get("kind") in ("refused", "budget", "scp", "rsync"):
        return True
    # A push's read-only precheck and its final rename are covered by the
    # PUSHED line; show them only when they fail, or one push
    # prints three receipts.
    # A pull's size check likewise precedes its `pulled` line.
    if (entry.get("verb") == "hpc-push" and entry.get("step") in ("precheck", "commit")) \
            or (entry.get("verb") == "hpc-pull" and entry.get("step") == "sizecheck"):
        return entry.get("rc") != 0
    if entry.get("tokens") or entry.get("trusted"):
        return True
    if entry.get("verb") == "hpc-push" or entry.get("level") in ("prepare", "operate", "improvise"):
        return True
    return entry.get("rc") != 0


# --- the two events --------------------------------------------------------

def site_names():
    try:
        return sorted(n[:-5] for n in os.listdir(sites_dir())
                      if n.endswith(".toml") and not n.endswith(".trust.toml"))
    except OSError:
        return []


def site_line(name):
    """``cluster: profile observe · connected`` from bin/hpc-check (local only)."""
    try:
        p = subprocess.run([sys.executable, os.path.join(BIN, "hpc-check"), "--site", name],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return "%s: could not run hpc-check" % name
    text = p.stdout + p.stderr
    m = re.search(r"profile (\w+)", text)
    profile = ("profile " + m.group(1)) if m else None
    if profile and re.search(r"sandbox /\S+", text):
        profile += " · sandbox"
    if p.returncode == 0:
        conn = "connected"
    elif p.returncode == 69:
        hint = re.search(r"run:\s+(.+)", text)
        conn = "NOT connected (you run: %s)" % hint.group(1).strip() if hint else "NOT connected"
    else:
        first = text.strip().splitlines()[0] if text.strip() else "rc %d" % p.returncode
        conn = "problem: " + clip(first, 80)
    return "%s: %s" % (name, " · ".join(x for x in (profile, conn) if x))


def announced_file(session):
    return cursor_file(session)[:-len(".json")] + ".announced"


def announced(session):
    return os.path.exists(announced_file(session))


def mark_announced(session):
    try:
        os.makedirs(os.path.dirname(announced_file(session)), mode=0o700, exist_ok=True)
        open(announced_file(session), "w").close()
    except OSError:
        pass


def plugin_root_file():
    return os.path.join(audit_dir(), "plugin-root")


def record_plugin_root():
    """Where this plugin lives now; the status bar's command reads it, so it
    survives an update that moves the plugin to a new versioned directory."""
    root = os.path.dirname(HOOK_DIR)
    try:
        with open(plugin_root_file()) as fh:
            if fh.read().strip() == root:
                return
    except OSError:
        pass
    try:
        os.makedirs(audit_dir(), mode=0o700, exist_ok=True)
        tmp = plugin_root_file() + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(root + "\n")
        os.replace(tmp, plugin_root_file())
    except OSError:
        pass


def on_start(event):
    """Silent: record the plugin's place and start receipts from now."""
    record_plugin_root()
    session = event.get("session_id") or ""
    if session and event.get("source") not in ("resume", "compact"):
        # Receipts start from now, not from the month's earlier entries.
        try:
            write_cursor(session, audit_file(), os.path.getsize(audit_file()))
        except OSError:
            write_cursor(session, audit_file(), 0)
    return None


def status(mode, line=None):
    """The status: one line per site, profile and connection."""
    line = line or site_line
    names = site_names()
    if not names:
        return (PREFIX + "loaded, but no site file in %s: cluster tools won't run "
                "until you add one (reference/setup.md)" % sites_dir())
    lines = [line(n) for n in names]
    tail = "receipts: %s (set in %s)" % (mode, os.path.join(config_dir(), "config.toml"))
    if len(lines) == 1:
        return PREFIX + "active · " + lines[0] + " · " + tail
    return PREFIX + "active · " + tail + "\n" + "\n".join("  " + l for l in lines)


def is_first_use_skill(event):
    if event.get("tool_name") != "Skill":
        return False
    return "hpc-tether" in str((event.get("tool_input") or {}).get("skill", ""))


def on_tool(event):
    tool_input = event.get("tool_input") or {}
    if is_first_use_skill(event):
        entries_wanted = False
    elif event.get("tool_name") == "Bash" and _TOOL.search(tool_input.get("command", "") or ""):
        entries_wanted = True
    else:
        return None
    mode = setting()
    session = event.get("session_id") or ""
    if mode == "off" or not session:
        return None
    lines = []
    if not announced(session):
        mark_announced(session)
        lines.append(status(mode))
    if entries_wanted:
        entries = new_entries(session)
        if mode == "notable":
            entries = [e for e in entries if notable(e)]
        lines += [receipt(e) for e in entries]
    return "\n".join(lines) or None


# --- the status bar --------------------------------------------------------

def cached_site_line(name):
    """``site_line``, reused for STATUS_TTL seconds: the bar refreshes often."""
    path = os.path.join(audit_dir(), "status-cache.json")
    now = datetime.now(timezone.utc).timestamp()
    try:
        with open(path) as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        cache = {}
    hit = cache.get(name)
    if isinstance(hit, list) and len(hit) == 2 and now - hit[0] < STATUS_TTL:
        return hit[1]
    line = site_line(name)
    cache[name] = [now, line]
    try:
        os.makedirs(audit_dir(), mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(cache, fh)
        os.replace(tmp, path)
    except OSError:
        pass
    return line


def unattended_label(session):
    """``UNATTENDED until 08:00`` for this session, or "" (the state file of
    skills/hpc-tether/hpctether/unattended.py, read fresh, never cached)."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session) or "none"
    try:
        with open(os.path.join(audit_dir(), "unattended", safe + ".json")) as fh:
            data = json.load(fh)
        if not data.get("since"):
            return ""
        until = data.get("until")
        if not until:
            return "UNATTENDED"
        end = datetime.fromisoformat(until)
        if datetime.now(timezone.utc) >= end:
            return ""
        return "UNATTENDED until " + end.astimezone().strftime("%H:%M")
    except Exception:
        return ""


def statusline(event):
    """The status bar: nothing until the plugin is used in this session."""
    session = event.get("session_id") or ""
    if not session or setting() == "off":
        return ""
    alone = unattended_label(session)
    if not announced(session):
        # Switching unattended on is itself a use worth showing.
        return (PREFIX + alone) if alone else ""
    names = site_names()
    if not names:
        return PREFIX + "no site file"
    line = " | ".join(cached_site_line(n) for n in names)
    return PREFIX + line + ((" · " + alone) if alone else "")


def main():
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return 0
    if "--statusline" in sys.argv[1:]:
        try:
            sys.stdout.write(statusline(event))
        except Exception:  # an empty bar, never an error in it
            pass
        return 0
    try:
        name = event.get("hook_event_name")
        if name == "SessionStart":
            message = on_start(event)
        elif name in ("PostToolUse", "PostToolUseFailure"):
            message = on_tool(event)
        else:
            message = None
    except Exception:  # never in the user's way
        return 0
    if message:
        json.dump({"systemMessage": message}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
