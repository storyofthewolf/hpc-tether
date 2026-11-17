#!/usr/bin/env python3
"""Tests for block_raw_ssh.py.

Run as ``python3 hooks/test_block_raw_ssh.py``. The cases live in this file
rather than on a command line because the hook is live in the session that
develops it: a Bash tool command whose text contains ``ssh host ...`` is
exactly what the hook blocks, test harness or not.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "block_raw_ssh.py"

# A fixture site and ssh config, so the cases do not depend on the user's
# real files: the site host is `host`, reached through the proxy
# gate.example.org, and also known as `cluster`. `workstation` has an ssh
# stanza but no site file, so it is not an HPC host.
_FIXTURE = Path(tempfile.mkdtemp(prefix="hpc-hook-test-"))
(_FIXTURE / "sites").mkdir()
(_FIXTURE / "sites" / "test.toml").write_text('name = "test"\nhost = "host"\n')
(_FIXTURE / "sites" / "other.example.toml").write_text('host = "workstation"\n')
(_FIXTURE / "ssh_config").write_text(
    "Host host\n"
    "    HostName host.example.org\n"
    "    ProxyCommand ssh -l u gate.example.org direct %h\n"
    "Host cluster cluster.example.org\n"
    "    HostName host.example.org\n"
    "Host workstation\n"
    "    HostName workstation.example.net\n"
    "Host *\n"
    "    ServerAliveInterval 60\n")
ENV = dict(os.environ, HPC_TETHER_SITES=str(_FIXTURE / "sites"),
           HPC_TETHER_SSH_CONFIG=str(_FIXTURE / "ssh_config"))


def blocked(proc) -> int:
    """2 for a JSON deny (the hook's block), else the exit code: the cases
    were written when a block was exit 2, and still read that way."""
    if proc.returncode == 0 and proc.stdout.strip():
        out = json.loads(proc.stdout)
        if out["hookSpecificOutput"]["permissionDecision"] == "deny":
            return 2
    return proc.returncode


def run(command: str, tool: str = "Bash", env=None) -> int:
    payload = json.dumps({"tool_name": tool, "tool_input": {"command": command}})
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input=payload, text=True, capture_output=True,
        env=env or ENV,
    )
    return blocked(proc)


BLOCKED = [
    "ssh cluster.example.org hostname",
    "ssh -O check cluster",
    "ssh -fN cluster",
    "scp file cluster:/x",
    "scp cluster:/x file",
    "sftp cluster",
    "rsync -av cluster:/a/ ./b/",
    "sshfs cluster:/a mnt",
    "cd /x && ssh host ls",
    "true; ssh host ls",
    "FOO=1 nohup ssh host ls",
    "command ssh host ls",
    "env -i ssh host ls",
    "timeout 30 ssh host ls",
    "echo $(ssh host ls)",
    "echo `ssh host ls`",
    "ls | ssh host cat",
    "/usr/bin/ssh host ls",
    "ssh host 'bash -c \"rm -rf /x\"'",
    "diff <(ssh host cat f) g",
    # A heredoc fed to a shell runs its body, so the body is still scanned.
    "bash <<'EOF'\nssh host ls\nEOF",
    "sh -s <<EOF\nscp a host:/b\nEOF",
    "cat <<EOF | bash\nssh host ls\nEOF",
    # An unterminated heredoc keeps its body.
    "cat <<EOF\nssh host ls",
    # A substitution runs wherever it sits, and
    # a quoted command word is still the command.
    "cat <<EOF\n$(ssh host hostname)\nEOF",
    "cat <<EOF\nthe date is `ssh host date` today\nEOF",
    "cat <<-EOF\n\tit isn't $(scp a host:/b) done\nEOF",
    'echo "$(ssh host hostname)"',
    'echo "now: `ssh host date`"',
    'x="$(rsync -a host:/a ./b)"',
    "'ssh' host hostname",
    '"ssh" host hostname',
    's"s"h host hostname',
    "ss''h host hostname",
    "s\\sh host hostname",
    "\\ssh host hostname",
    "'/usr/bin/ssh' host ls",
    # The literal body of SHELL -c is a command line, and
    # env's option values are not the verb.
    "bash -c 'ssh host ls'",
    'bash -c "ssh host ls"',
    "sh -c 'scp a host:/b'",
    "zsh -c 'rsync -a host:/a ./b'",
    "ksh -c 'ssh host ls'",
    "dash -c 'ssh host ls'",
    "bash -lc 'ssh host ls'",
    "bash -e -c 'ssh host ls'",
    "/bin/bash -c 'cd /x && ssh host ls'",
    "timeout 30 bash -c 'ssh host ls'",
    "env FOO=1 bash -c 'ssh host ls'",
    "echo hi; sh -c 'ssh host ls'",
    "env -u NAME ssh host ls",
    "env --unset NAME ssh host ls",
    "env -u A -u B ssh host ls",
    "env -i -u NAME scp a host:/b",
    "env -C /tmp ssh host ls",
    # Aliases, HostNames, proxies and user@ forms of the
    # site host are all HPC targets; a variable target is unreadable.
    "ssh host.example.org ls",
    "ssh gate.example.org ls",
    "ssh u@host ls",
    "ssh -J gate.example.org other ls",
    "ssh -o ProxyJump=gate.example.org other ls",
    "rsync -av -e ssh ./a/ u@cluster:/b/",
    "ssh $H ls",
    'ssh "$H" ls',
    "ssh ${HOST} ls",
    # The target words are read one by one. A variable or
    # a site host in any of them still blocks.
    'scp notes.md "$DEST"',
    "scp notes.md $H:/tmp/",
    "rsync -av ./out/ u@$H:/b/",
    "rsync -av ./out/ $DEST",
    'ssh -J "$GATE" workstation ls',
    "ssh -p 22 host ls",
    "ssh -l u -p 22 cluster ls",
    "scp -J gate.example.org notes.md workstation:",
    "scp -o ProxyJump=host notes.md workstation:",
    "rsync -av -e 'ssh' cluster:/a/ ./b/",
    "sftp u@host",
    "rsync -av rsync://host/module/ ./b/",
    "ssh workstation ssh host ls",
    "ssh workstation scp a cluster:/b",
]

ALLOWED = [
    'printf "%s\\n" "squeue -u $HPC_USER" | bin/hpc-run -',
    "skills/hpc-tether/bin/hpc-check",
    "bin/hpc-pull role:x/y",
    "python3 fetch_cases.py",
    "grep ssh ~/.ssh/config",
    "man ssh",
    "which ssh",
    "git push origin main",
    "ls -la ~/.ssh/sockets",
    "echo ssh",
    # A bare transport name with no arguments: interactive at best, and the
    # common shape of backticked prose inside a heredoc.
    "ssh",
    "cat > f <<EOF\nthe toolkit never hand-rolls `ssh` or `scp` calls\nEOF",
    "git commit -m 'hooks: block raw ssh from the Bash tool'",
    # Separators inside quotes are not separators: this grep pattern blocked
    # a read of the repo's own sources.
    "grep -n 'subprocess.run\\|def scp\\|rsync = \\|proc = ' bin/hpc-pull",
    'grep -nE "ssh -O check|rsync -a" reference/setup.md',
    "python3 - <<'EOF'\nimport subprocess; subprocess.run(['ssh', 'h', 'ls'])\nEOF",
    # A heredoc body that is data: a commit message with a line
    # starting `ssh -O check`, and prose whose apostrophe used to pair with a
    # later quote and expose quoted text.
    "git commit -q -F - <<'EOF'\nFix the hook\n\nssh -O check false alarm, noted\nEOF",
    "cat > notes.md <<'EOF'\nit isn't run\nssh host ls is what we refuse\nEOF",
    "python3 - <<'EOF'\n# prose isn't code\nx = \"ssh host ls\"\nEOF",
    # A quoted delimiter stops expansion, so a substitution there is text.
    "cat > notes.md <<'EOF'\nrun $(ssh host ls) by hand\nEOF",
    # Prose in an expanding heredoc, with no substitution, is still data.
    "cat > notes.md <<EOF\nssh host ls is what we refuse, isn't it\nEOF",
    # Quoted words that are arguments, not commands.
    "grep 'ssh' ~/.ssh/config",
    'echo "ssh host ls"',
    "git commit -m 'ssh'",
    'git commit -m "block raw ssh host calls"',
    "grep -n ssh\\|rsync notes.md",
    # A `bash -c` that is only mentioned, or whose body is not a transport.
    "bash -c 'ls /tmp'",
    'bash -c "echo hello"',
    "bash -c 'grep ssh ~/.ssh/config'",
    "git commit -m \"use bash -c 'ssh host ls' for this\"",
    "echo \"bash -c 'ssh host ls'\"",
    "grep \"sh -c 'ssh host'\" notes.md",
    "cat > notes.md <<'EOF'\nbash -c 'ssh host ls'\nEOF",
    "env -u NAME ls",
    "env -u NAME python3 fetch.py",
    "env FOO=1 ls -la",
    # Hosts that are not HPC sites pass.
    "ssh workstation ls",
    "ssh workstation.example.net uptime",
    "ssh -T git@git.example.com",
    "scp notes.md laptop:~/Desktop/",
    "rsync -av ./out/ workstation:/home/u/out/",
    "ssh workstation 'echo $PATH'",
    "ssh-keygen -lf ~/.ssh/id_ed25519.pub",
    # A `$` that is not the target, and a directory that
    # shares a site's name, blocked ssh between the user's own machines.
    'ssh workstation "ls $HOME/data"',
    "ssh workstation ls $HOME/data",
    'ssh workstation "cd proj && git pull"',
    "ssh -p 2222 workstation uptime",
    'rsync -a "$HOME/x/" laptop:backup/',
    "rsync -a $HOME/x/ laptop:backup/",
    "rsync -a ./out/ laptop:backup-$(date +%F)/",
    "rsync -a ./cluster/ laptop:/x/",
    "rsync -a ./host/ laptop:/x/",
    "scp notes.txt laptop:cluster/",
    "scp laptop:~/cluster/notes.txt ./host/",
    "ssh workstation ls cluster/",
    "ssh workstation cat ~/host/notes.md",
    'scp -i "$HOME/.ssh/id_ed25519" notes.md laptop:',
    "rsync -a --exclude=\"$PAT\" ./out/ laptop:out/",
    "rsync -a -o ./cluster/ laptop:/x/",
]


class TestHook(unittest.TestCase):
    def test_blocked(self):
        for c in BLOCKED:
            self.assertEqual(run(c), 2, c)

    def test_allowed(self):
        for c in ALLOWED:
            self.assertEqual(run(c), 0, c)

    def test_other_tools_ignored(self):
        self.assertEqual(run("ssh host ls", tool="Read"), 0)

    def test_no_site_file_blocks_everything(self):
        empty = tempfile.mkdtemp(prefix="hpc-hook-empty-")
        env = dict(ENV, HPC_TETHER_SITES=empty)
        self.assertEqual(run("ssh workstation ls", env=env), 2)
        self.assertEqual(run("ssh -T git@git.example.com", env=env), 2)

    def test_garbage_input_allows(self):
        proc = subprocess.run(
            [sys.executable, str(HOOK)], input="not json", text=True, capture_output=True
        )
        self.assertEqual(proc.returncode, 0)


class TestUserNotice(unittest.TestCase):
    def test_block_tells_agent_and_user(self):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "scp a host:b"}})
        proc = subprocess.run([sys.executable, str(HOOK)], input=payload,
                              text=True, capture_output=True, env=ENV)
        self.assertEqual(proc.returncode, 0)
        out = json.loads(proc.stdout)
        self.assertIn("hpc-tether verbs", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(out["systemMessage"],
                         "hpc-tether ▸ BLOCKED: the agent tried raw `scp`; nothing ran. "
                         "It was told to use the hpc-* tools instead.")

    def test_allowed_prints_nothing(self):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        proc = subprocess.run([sys.executable, str(HOOK)], input=payload,
                              text=True, capture_output=True)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
