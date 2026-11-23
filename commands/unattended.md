---
description: Switch this session to unattended (refuse instead of asking), or back. Args - on [HH:MM] | off
argument-hint: on [HH:MM] | off
disable-model-invocation: true
allowed-tools: Bash(python3:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/skills/hpc-tether/bin/hpc-unattended" $ARGUMENTS --session "${CLAUDE_SESSION_ID}"`

Show the output above to the user exactly as printed, in one code block. Add
nothing else. Do not run anything.
