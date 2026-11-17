---
description: Show hpc-tether's settings, connection, and what this session sent to the cluster (local only)
allowed-tools: Bash(python3:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/skills/hpc-tether/bin/hpc-status"`

Show the output above to the user exactly as printed, in one code block. Add
nothing before it. After it, add at most one sentence, and only if something
needs the user's action: a site NOT connected (give the `ssh -fN` command),
a refusal, or a failure. Do not run anything else.
