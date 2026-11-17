"""The plugin's hooks.json runs both hooks, under any python3 on PATH."""

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOKS = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]["PreToolUse"]
COMMANDS = [h["command"] for entry in HOOKS for h in entry["hooks"]]
EVENT = {"tool_name": "Bash", "permission_mode": "auto",
         "tool_input": {"command": "/x/bin/hpc-push f.csv --yes"}}

# Every python3 we can find: the system one (3.9 on macOS) is the one that
# broke the approval hook at import.
PYTHONS = sorted({p for p in ("/usr/bin/python3", shutil.which("python3")) if p and os.path.exists(p)})


class TestHooksJson(unittest.TestCase):
    def test_hooks_registered(self):
        self.assertEqual([e["matcher"] for e in HOOKS],
                         ["Bash", "Edit|Write|MultiEdit|NotebookEdit"])
        bash = [h["command"] for h in HOOKS[0]["hooks"]]
        for name in ("block_raw_ssh.py", "approve_hpc_writes.py", "protect_site_file.py"):
            self.assertTrue(any(name in c for c in bash), name)
        self.assertTrue(all("protect_site_file.py" in h["command"] for h in HOOKS[1]["hooks"]))

    def test_receipts_registered(self):
        events = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
        for name in ("SessionStart", "PostToolUse", "PostToolUseFailure"):
            with self.subTest(event=name):
                cmds = [h["command"] for e in events[name] for h in e["hooks"]]
                self.assertTrue(any("receipts.py" in c for c in cmds))
        # The skill's invocation is a first use too: it brings the status.
        self.assertEqual([e["matcher"] for e in events["PostToolUse"]], ["Bash|Skill"])
        self.assertEqual([e["matcher"] for e in events["PostToolUseFailure"]], ["Bash"])
        self.assertTrue((ROOT / "hooks" / "receipts.py").is_file())

    def test_every_hook_file_exists(self):
        for c in COMMANDS:
            rel = c.split('"${CLAUDE_PLUGIN_ROOT}"/', 1)[1]
            self.assertTrue((ROOT / rel).is_file(), rel)

    def test_commands_run_as_claude_code_runs_them(self):
        for py in PYTHONS:
            for c in COMMANDS:
                with self.subTest(python=py, command=c):
                    env = dict(os.environ, CLAUDE_PLUGIN_ROOT=str(ROOT),
                               PATH=f"{Path(py).parent}:/usr/bin:/bin")
                    line = c.replace("${CLAUDE_PLUGIN_ROOT}", "$CLAUDE_PLUGIN_ROOT")
                    p = subprocess.run(["sh", "-c", line], input=json.dumps(EVENT),
                                       capture_output=True, text=True, env=env)
                    self.assertEqual(p.returncode, 0, p.stderr)
                    if "approve" in c:
                        out = json.loads(p.stdout)["hookSpecificOutput"]
                        self.assertEqual(out["permissionDecision"], "ask")


class TestManifests(unittest.TestCase):
    def test_plugin_and_marketplace_agree(self):
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
        self.assertEqual(plugin["name"], "hpc-tether")
        self.assertEqual([p["name"] for p in market["plugins"]], ["hpc-tether"])
        self.assertEqual(market["plugins"][0]["source"], "./")

    def test_skill_where_the_plugin_looks(self):
        self.assertTrue((ROOT / "skills" / "hpc-tether" / "SKILL.md").is_file())


if __name__ == "__main__":
    unittest.main()
