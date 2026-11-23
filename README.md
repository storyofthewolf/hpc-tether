# hpc-tether

A Claude Code plugin that lets an AI agent work on an HPC cluster (check the
queue, read logs, inspect netCDF output, pull results down, and, if you allow
it, edit files and run jobs) through one guarded, audited path. Built for
SLURM clusters reached over an SSH ControlMaster, and used daily on one.

The agent acts as you on shared, mostly unbacked-up storage. The toolkit
steers it: it routes work through fixed tools, refuses the mistakes agents
commonly make (a deletion, a write outside your directories, a second job of
the same name), asks you before each write, and logs everything. The last
section says what it does not catch.

The name says how it works: the agent works on the cluster on a line you
hold. The profile sets how far it reaches, and each approval prompt is yours
to give or withhold.

![How hpc-tether works: the agent reaches the cluster only through hpc-tether's fixed tools; hooks ask you before each write and block raw ssh; a payload guard applies the profile you set; every call is logged and shown back to you; commands ride an SSH ControlMaster you open yourself.](docs/hpc-tether-overview.svg)

## Why tools instead of plain `ssh`

When an agent runs unattended, nobody reads its commands, whether they are
`ssh` lines or `hpc-*` payloads. The difference is what reads them on your
behalf:

- **A plain `ssh` command** reaches the cluster as written. Nothing checks it,
  nothing decides whether it needs you, and the only record is the agent's
  own account. Claude Code's permissions can't tell `ssh host ls` from
  `ssh host rm -rf …`, so you either approve every call or allow them all.
- **An `hpc-*` call** passes a second reader (the guard refuses common
  mistakes before anything is sent), a gate (reads run freely; anything that
  writes, submits or deletes stops for your approval, with a plain-words
  line saying what it will do), and a record (the audit log keeps the full
  payload, and a receipt line shows it as it happens).

The skill steers the agent to the right tool, the tools carry the connection
plumbing (the shared master connection, tcsh quoting, remote timeouts), and a
hook blocks raw `ssh` so the agent can't skip them by forgetting. Together
they shape a well-meaning agent's work into controlled channels: mistakes get
caught, consequential actions ask you first, and everything is logged.

## What you choose: a profile

Your site file sets how much the agent may do. Start with `observe`; it is
the default.

| Profile | The agent may | Protection |
|---|---|---|
| `observe` | Fixed tools only: queue, job state, logs, file contents, directory listings, netCDF headers and statistics; pull files down within a transfer budget. No free-form shell | Their remote scripts are written by the toolkit, and the agent's input reaches them only as quoted arguments |
| `prepare` | Also change files, only inside directories you list: push an edited file back, `mkdir`, `cp`, `mv`, `git pull` | Each write asks you first. Never starts a process |
| `operate` | Also run your own trusted scripts, and submit or cancel jobs | The scripts are code you committed and reviewed |
| `improvise` | Also run code the agent writes itself | You read each payload in the prompt before it runs |

No flag the agent can pass raises the profile. It is set in your site file;
hooks stop the agent editing that file or pointing the toolkit at another
one, and a refusal tells it to ask you.

**A profile sets what may be done; it never pre-approves.** At every
profile, each write and each run of the agent's own code asks you, except
inside the sandbox.

### Sandbox and unattended sessions (optional)

- **`sandbox_roots`** under `[policy]` in your site file names directories
  where what your profile already allows runs without asking: writes and
  pushes into them, jobs whose script is in one, code run from one. It
  never raises the profile, and your trusted scripts' `confirm` patterns
  still ask. Everything outside asks as before. See
  `reference/design.md`, "The sandbox".
- **`/hpc-tether:unattended on [HH:MM]`** switches one session to refuse
  instead of asking, so a run left overnight never waits on a prompt. Work
  in the sandbox and reads go on; anything else is refused and left for
  your morning. `/hpc-tether:unattended off` ends it, or it ends at `HH:MM`.
  The status bar shows `sandbox` and `UNATTENDED`.

## What the toolkit does at every profile

- **It refuses deletions.** `rm` and the other spellings it recognizes are
  not sent, under any profile or flag. Deleting is done by you, by hand.
- **It refuses writes outside your write roots**: your scratch directory plus
  the directories you list. A write the guard can't place is refused.
- **It refuses in-place editing.** Files are pulled, edited locally, and
  pushed back with a backup and a check that nobody changed them meanwhile.
- **It blocks raw `ssh`/`scp`/`rsync` in the agent's Bash calls** with a
  hook. Everything goes through the toolkit, which runs each payload under a
  remote watchdog and logs it to `~/.local/state/hpc-tether/` (see
  `hpc-audit`).
- **It asks you before each write it sends**: a Claude Code permission
  prompt, which the agent cannot answer for you, including in auto mode. A
  command the guard takes for a read sends no prompt.

## What it does not promise

- **It catches accidents, not adversaries.** The agent runs on your machine, as you,
  beside an open SSH connection, and a scan of shell text cannot stop a
  payload written to get past it. The guard catches the mistakes agents make
  by habit; the ones it knows it misses are listed under "Known limits" in
  `skills/hpc-tether/reference/design.md`. The real backstops are your
  approval prompts, your account's own permissions, and backups.
- **The agent's own shell can reach the SSH socket directly**, as can a
  program it writes, which can also load a site file of its own. Claude
  Code's sandboxing, not this plugin, controls that.
- **Trusted means committed.** The script that runs must be a regular file
  tracked in a clean checkout; gitignored files it imports are not checked.
  Declare each git submodule under a script root as a root of its own: the
  tracked-file check reads the parent's index, which holds only the gitlink. If an agent
  can commit and push to a trusted repository, its code arrives through
  `git pull`. Review those commits.
- **Codex and other agents** can use the tools, but without Claude Code there
  is no approval prompt and no `ssh` block; only the profile and guard apply.
- SLURM only. Tested on one cluster: a gateway login with 2FA, GPFS storage.

## Requirements

- **Locally:** Claude Code, Python 3.11 or newer on `PATH` (the tools find
  it), and OpenSSH.
- **On the cluster:** `bash`, GNU `timeout`, SLURM (`squeue`, `sacct`), and
  `git`. For `hpc-nc`, a Python with `numpy` and `netCDF4` (often from a
  module; the site file's `python_bin` points at it).
- **An SSH ControlMaster** you open yourself, since it needs your 2FA:
  `ssh -fN <host>`. The agent can use it, never reopen it.
  `skills/hpc-tether/reference/setup.md` has the `~/.ssh/config` recipe.

Before you start, **check your center's acceptable-use policy** on persistent
SSH connections and on automated agents acting under your account.

## Install

```
/plugin marketplace add storyofthewolf/hpc-tether
/plugin install hpc-tether@hpc-tether
```

Then create your site file. Copy the template
`skills/hpc-tether/sites/example.toml` to
`~/.config/hpc-tether/sites/<name>.toml` and edit it:

- `user`, and your `scratch` path under `[paths]`.
- `[policy]` first: leave `profile = "observe"` until you have watched it
  work; set `write_roots` (a sandbox directory, or the directories that are
  yours alone, never a whole group area) and any `protected` paths inside
  them that must never change.
- `[repos.<name>]` for each code repository on the cluster; `local` if you
  also have it here, `trusted = true` if its scripts may run with no token.
- `[filesystem] aliases` if a directory has two spellings: on GPFS
  sites, `readlink -f /home/$USER` often shows a different real path.

Your site file lives outside the plugin, so updates never overwrite it.
Check the connection with the agent's first command, `hpc-check`.

## Day to day

Ask in plain language: "what's in my queue", "tail the log of job 1234",
"what's the global mean of TS in the last history file", "pull that file
down". The agent picks the tool:

| Tool | Does |
|---|---|
| `hpc-check` | Is the connection up? Which profile? |
| `hpc-jobs`, `hpc-probe` | Queue and job state (one line with `--brief`); is a running job progressing |
| `hpc-read` | Tail or head a named file; grep one file, several, or `dir/*.log` |
| `hpc-ls` | List one directory: type, size, time, link target |
| `hpc-nc` | netCDF header, statistics, a few values, a diff of two files |
| `hpc-pull`, `hpc-fetch` | Bring a file or directory down |
| `hpc-push` | Send an edited file back (profile `prepare`) |
| `hpc-run` | Anything else, as a guarded script on stdin (profile `prepare` and up) |
| `hpc-audit`, `hpc-trust` | What was sent, and how a payload would be classified |

## Seeing it work

You don't have to take the agent's word for what happened on the cluster.
The plugin tells you itself, from its audit log:

- **At first use**, when the agent loads the skill or runs its first
  cluster command: one line per site, profile and whether the connection is
  up (and, if not, the `ssh -fN` to run). A session that never touches the
  cluster shows nothing; the plugin adds only its skill's one-line
  description to the model's context until the skill is used.
- **In the status bar**, if you turn it on (below): the same line for the
  rest of the session, where it doesn't scroll away.
- **After each cluster call**, a receipt line marked `hpc-tether ▸`: read
  or write, which approval it carried, what was asked, and how it ended.
  Refusals say "nothing sent". Transfers give direction and size.
- **When the agent tries a way around it** (a raw `ssh`, an edit to your
  site file), a `BLOCKED` line.
- **On demand**, `/hpc-tether:status`: profile, connection, and this
  session's tally of reads, writes, refusals, failures and data moved. To
  leave the agent out of it entirely, run the tool yourself at the prompt:
  `! <plugin>/skills/hpc-tether/bin/hpc-status`.

Once you trust it, quiet the receipts in
`~/.config/hpc-tether/config.toml`:

```toml
receipts = "notable"   # only writes, approvals, transfers, refusals, failures
# receipts = "off"     # nothing
```

The agent can't edit that file, so it can't turn its own receipts off.

To keep the status in Claude Code's status bar, add this to
`~/.claude/settings.json` (it replaces any status line you already have):

```json
"statusLine": {
  "type": "command",
  "command": "python3 \"$(cat ~/.local/state/hpc-tether/plugin-root)\"/hooks/receipts.py --statusline"
}
```

The bar stays empty until hpc-tether is used in a session, then shows
`hpc-tether ▸ <site>: profile <level> · connected`. The plugin records its
own location at each session start, so the line survives plugin updates.

## Running the tests

From the repository root, with [uv](https://docs.astral.sh/uv/) installed:

```
uv run --no-project --python 3.14 --with pytest --with numpy --with netCDF4 pytest skills/hpc-tether/tests hooks
```

uv builds a throwaway environment for the run and caches it, so nothing is
installed globally and a Python upgrade can't break it. Without numpy and
netCDF4 (a plain `pytest` run), the `hpc-nc` tests are skipped. One test
needs bash 4 or newer, which macOS does not ship (`brew install bash`).

Use pytest, not `python -m unittest`: `tests/conftest.py` points the audit
log at a temporary file, and only pytest loads it. Under unittest the tests
write into your real audit log.

## Reporting a false refusal

The guard errs toward refusing. If it refuses something harmless, run
`... | hpc-trust check -` on the payload and open an issue with the output.

## License

MIT. See `LICENSE`.
