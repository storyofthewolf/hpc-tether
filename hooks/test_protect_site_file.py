"""The agent never writes the site files: Edit/Write and plain Bash spellings."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent / "protect_site_file.py"
SITE = "~/.config/hpc-tether/sites/cluster.toml"
ABS = os.path.expanduser(SITE)


def blocked(proc) :
    """2 for a JSON deny (the hook's block), else the exit code: the cases
    were written when a block was exit 2, and still read that way."""
    if proc.returncode == 0 and proc.stdout.strip():
        out = json.loads(proc.stdout)
        if out["hookSpecificOutput"]["permissionDecision"] == "deny":
            return 2
    return proc.returncode


def run(tool, tool_input, env=None):
    p = subprocess.run([sys.executable, str(HOOK)],
                       input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
                       capture_output=True, text=True, env=env or dict(os.environ))
    return blocked(p)


def bash(cmd, env=None):
    return run("Bash", {"command": cmd}, env)


class TestEditTools(unittest.TestCase):
    def test_blocked(self):
        for tool in ("Edit", "Write", "MultiEdit"):
            with self.subTest(tool=tool):
                self.assertEqual(run(tool, {"file_path": ABS}), 2)
        self.assertEqual(run("Write", {"file_path": os.path.expanduser("~/.config/hpc-tether/new.toml")}), 2)

    def test_elsewhere_allowed(self):
        self.assertEqual(run("Write", {"file_path": "/tmp/x.toml"}), 0)
        self.assertEqual(run("Edit", {"file_path": os.path.expanduser("~/.config/other/x")}), 0)
        self.assertEqual(run("Read", {"file_path": ABS}), 0)

    def test_override_dir(self):
        env = dict(os.environ, HPC_TETHER_SITES="/srv/mysites")
        self.assertEqual(run("Write", {"file_path": "/srv/mysites/a.toml"}, env), 2)


class TestBash(unittest.TestCase):
    def test_writes_blocked(self):
        for cmd in (f"echo 'profile = \"improvise\"' >> {SITE}",
                    f"echo x > {ABS}",
                    f"echo x >$HOME/.config/hpc-tether/sites/d.toml",
                    f"printf x | tee -a {SITE}",
                    f"sed -i '' 's/observe/operate/' {SITE}",
                    f"perl -pi -e 's/a/b/' {SITE}",
                    f"cp /tmp/x.toml {SITE}",
                    f"mv /tmp/x.toml ~/.config/hpc-tether/sites/",
                    f"touch {SITE}",
                    f"rm {SITE}",
                    "cd ~/.config/hpc-tether/sites && echo x >> cluster.toml",
                    "cd ~/.config/hpc-tether/sites; sed -i '' s/a/b/ cluster.toml",
                    f"dd if=/tmp/x of={ABS}"):
            with self.subTest(cmd=cmd):
                self.assertEqual(bash(cmd), 2)

    def test_reads_allowed(self):
        for cmd in (f"cat {SITE}", f"grep repos {SITE}",
                    f"cp {SITE} /tmp/backup.toml",
                    "ls ~/.config/hpc-tether/sites",
                    "echo x > /tmp/out 2>&1",
                    "cd ~/.config/hpc-tether/sites && cat cluster.toml",
                    "git commit -m 'site file docs'"):
            with self.subTest(cmd=cmd):
                self.assertEqual(bash(cmd), 0)


class TestOverrides(unittest.TestCase):
    """The agent can't point the toolkit at
    site files of its own."""

    def test_setting_an_override_is_blocked(self):
        for cmd in ("HPC_TETHER_SITES=/tmp/x bin/hpc-run -",
                    "export HPC_TETHER_SITES=/tmp/x",
                    "env HPC_TETHER_SITES=/tmp/x bin/hpc-check",
                    'env "HPC_TETHER_SITES=/tmp/x" bin/hpc-check',
                    "cd /x && HPC_TETHER_SITES=/tmp/x bin/hpc-push f --yes",
                    "bash -c 'HPC_TETHER_SITES=/tmp/x hpc-run -'",
                    "HPC_TETHER_AUDIT=/dev/null bin/hpc-run -",
                    "HPC_TETHER_MANIFEST=/tmp/m.json bin/hpc-push f --yes",
                    "read HPC_TETHER_SITES < f",
                    "declare -x HPC_TETHER_SITES; HPC_TETHER_SITES+=/x"):
            with self.subTest(cmd=cmd):
                self.assertEqual(bash(cmd), 2)

    def test_reading_or_naming_it_is_allowed(self):
        for cmd in ("echo $HPC_TETHER_SITES",
                    'ls "${HPC_TETHER_SITES}"',
                    "grep -rn HPC_TETHER_SITES skills/",
                    "unset HPC_TETHER_SITES",
                    "printenv HPC_TETHER_SITES"):
            with self.subTest(cmd=cmd):
                self.assertEqual(bash(cmd), 0)


class TestUserNotice(unittest.TestCase):
    def notice(self, tool, tool_input):
        p = subprocess.run([sys.executable, str(HOOK)],
                           input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        return json.loads(p.stdout)["systemMessage"]

    def test_edit_of_site_file(self):
        self.assertIn("BLOCKED: the agent tried to change your hpc-tether settings (Write on",
                      self.notice("Write", {"file_path": ABS}))

    def test_override_variable(self):
        self.assertIn("tried to set HPC_TETHER_AUDIT",
                      self.notice("Bash", {"command": "HPC_TETHER_AUDIT=/tmp/x hpc-run -"}))


if __name__ == "__main__":
    unittest.main()
