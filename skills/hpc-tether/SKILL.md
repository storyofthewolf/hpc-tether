---
name: hpc-tether
description: Required for all contact with an HPC cluster (a host with a site file in ~/.config/hpc-tether/sites/): remote commands, jobs and logs, netCDF files, transfers, cluster checkouts. Load it before the first ssh/scp/rsync and never hand-roll them; also before asking the user to run cluster commands and paste the output. Not for other hosts (workstations, git servers).
---

# HPC tether

The toolkit is at `${CLAUDE_PLUGIN_ROOT}/skills/hpc-tether`. Paths below are
relative to it; run each verb by its full path,
`${CLAUDE_PLUGIN_ROOT}/skills/hpc-tether/bin/hpc-check`. Every verb takes
`--help` and `--site NAME`. The cluster gets only what it must run; heavy work
happens locally. **Never copy, clone or push this toolkit to a cluster.** The
toolkit steers and catches common mistakes; it is not a security boundary.
Why each rule exists, and what it does not catch: `reference/design.md`.

## Verbs: use the specific tool, not a script

A payload that only runs `ls`, `stat`, `tail`, `grep`, `squeue` or `sacct`
is one of these tools, done worse. `hpc-run` is for what none of them does,
and at the default profile it is refused.

| Task | Tool |
|---|---|
| Start any remote work; see the profile | `bin/hpc-check` |
| Read a file or log | `bin/hpc-read PATH --tail N` (`--head N`, `--grep PAT`) |
| Grep several files, or a pattern in one directory | `bin/hpc-read P1 P2 --grep PAT`, `bin/hpc-read 'role:dir/*.log' --grep PAT` |
| List a directory; a file's size, time or link target | `bin/hpc-ls PATH` (`--sort time`, `--limit N`) |
| Queue state | `bin/hpc-jobs` |
| One job, including after it finishes | `bin/hpc-jobs --job ID` (bare `squeue -j` forgets a finished job) |
| Check on a job again (a watch) | `bin/hpc-jobs --job ID --brief`: one line, `ID STATE ELAPSED EXITCODE` |
| A running job whose state is unclear | `bin/hpc-probe`, then `reference/job-diagnostics.md` |
| Inside a netCDF file: header, stats with NaN/fill counts, a few values, a diff | `bin/hpc-nc header\|stats\|slice\|diff` |
| Bring a file down | `bin/hpc-pull SRC [DST]` |
| Bring a directory down | `bin/hpc-fetch SRC [DST]`, with `--dry-run` first |
| Send an edited file back, or a new one | `bin/hpc-push LOCAL --yes`; new: `--to role:path` |
| Local, GitHub and HPC checkouts agree? | `bin/hpc-sync-status NAME` or `--all` (site `[repos]`), then `reference/git-sync.md` |
| What was sent, and what data moved | `bin/hpc-audit`; `--transfers --session current` |
| The user asks what the plugin is doing | `bin/hpc-status` (or they run `/hpc-tether:status`): print its output as is |
| Whether a payload is a trusted script, and why not | `bin/hpc-trust list` / `check -` |
| Anything no tool above does | `... \| bin/hpc-run -` |
| No master, or no `~/.ssh/config` entry | `reference/setup.md` |
| An error with no remote output | `reference/troubleshooting.md` |

Remote paths are absolute or `role:relative/path`, using the site's `[paths]`.

## Running a payload

- **Script on stdin only**: `... | hpc-run -`. A command string and a script
  file under `/tmp` or a scratchpad are refused. A script worth keeping goes
  in `lib/`.
- **Strict mode**: the first failing command ends the payload, and stderr
  names the line. Give a command allowed to fail `|| true`. A pipeline cut
  short by `head`, `grep -m` or `sed q` (status 141) is not a failure.
- **A remote watchdog** runs every payload, sized below `--timeout`. A
  watchdog notice means it was killed on the cluster. A plain local timeout
  means the transport stalled, and the remote may still be running: check
  before retrying.
- **Size `--timeout` to the work, never to the Bash tool.** If a payload may
  run past ~5 minutes, run the Bash call with `run_in_background: true` and
  give a generous `--timeout` (2–3× the estimate; 3600 is fine). Never above
  ~540 on a foreground call. Work of hours or heavy memory is a batch job.
- A child that leaves the process group (`setsid`, a daemon) can survive the
  watchdog: look again after a payload that backgrounds work.
- **An `sbatch` with a literal `-J`/`--job-name`** first checks the queue on
  the cluster, and nothing runs if that name is active or `squeue` fails
  (exit 79). Otherwise you get a note: run `hpc-jobs --name NAME` yourself
  before proposing the submit. Cancel and resubmit in separate payloads.
- **Everything is audited** in `~/.local/state/hpc-tether/`, each payload's
  whole text included (`hpc-audit --full`).

## Profiles (exit 77)

The user's site file sets how much may be sent at all. Each level includes
the ones above it:

| profile | adds |
|---|---|
| `observe` (default) | the fixed tools only (queue, logs, listings, probes, `hpc-nc`, `hpc-pull`, `hpc-fetch`): their scripts are the toolkit's. `hpc-run` is refused |
| `prepare` | free-form shell: reads, and writes inside the write roots (`hpc-push`, `mkdir`/`cp`/`mv`/redirects, `git pull`). Starts no process |
| `operate` | trusted scripts with no token; `sbatch`, `scancel <id>`, `kill` |
| `improvise` | code you write (`python -c`, `python - <<EOF`, `srun`, `make`), and commands the guard doesn't know |

On `REFUSED by profile`, **never edit the site file or offer to**: tell the
user what was refused and which level would allow it, or do the part the
current level allows.

## The guard (exit 77)

`hpc-run` classifies every payload before sending it. It reads shell text:
it catches habits and mistakes, not a payload written to get past it. Print
a refused command for the user to run by hand. Never split a refused
payload, or re-spell it in Python or a nested shell, to get it through.

- **Refused at every profile, under any flag:**
  - **Every deletion**: `rm`, `rmdir`, `unlink`, `shred`, `find -delete`,
    `git clean`, in any wrapping, not even of its own temp files. That
    includes a compressor replacing its input (`gzip f`: add `-k`, or `-c`
    to stdout), `tar --remove-files` and `zip -m`.
  - **Any write outside the write roots, or inside a `protected` path**
    (scratch plus `[policy] write_roots`, through the aliases): every target the guard sees, including redirects,
    git repositories and NCO/CDO outputs. A target it can't place counts as
    outside. A relative path is placed only by `cd <dir> &&` before it on the
    same line, or by the login directory in a payload with no `cd`.
    Otherwise use an absolute or `$HPC_ROLE/...` path.
  - **In-place editing** (`sed -i`, `perl -i`, `vi`, `ex`, `ed`, `patch`), and
    **a code file written into a script root**.
  - **A link (`ln`) to anything outside the write roots**, and **setting a
    `$HPC_*` role** in the payload.
  - `git reset --hard`, force pushes, `find -exec`, `chown`, recursive
    `chmod`, `dd`, `truncate`, `crontab`/`at`, `scancel -u` without a job id.
- **Traversal, never sent: bound it.** `find` needs `-maxdepth`; `du` needs
  `-s`/`--max-depth` (both still walk the whole tree); no `ls -R`, no
  recursive `grep`.
- **Unknown or interpreter: `improvise` plus `--allow-exec`.** That means:
  - a command the guard doesn't know, or a known read with an option it
    doesn't list (`sed -f`, a `sed` program beyond print/delete/substitute,
    `find -fprint`, `sort --compress-program`, `git -c`, `xxd -r`);
  - `env -C`, and anything behind `xargs` or `parallel` but a few plain
    reads (`grep`, `wc`, `cat`, `head`, `tail`, `ls`, `stat`, ...);
  - an interpreter, `eval`, `source`, or a nested `bash -c`;
  - `srun`, `sbatch --wrap`, `make`;
  - `curl`/`wget`/`scp`/`rsync`, `ssh` to another node, or a file run by path.

  Prefer a narrower command. If a harmless read is refused, show
  the user the `hpc-trust check -` output for it: teaching the guard a
  command is the maintainer's change, never yours. `hpc-probe`'s own `srun` is fixed and read-only.
- **Write: `prepare` plus `--allow-write`**, every target inside a write
  root. Job control (`sbatch`, `scancel <id>`, `kill`, `scontrol` changes)
  needs `operate`. Read a `mv`'s source and destination back to the user.

The guard is not a security boundary: never say a thing is "impossible" or
that observe "cannot write". The backstops are the user's approval prompts,
the account's own permissions, and backups.

**Tokens**: `--allow-write`, `--allow-exec`, `--yes`, `--force`,
`--overwrite`, `--merge`, `--force-size` and `--budget-add`. A token is a
label, not a permission: it declares what kind of operation the command is,
and the tool refuses an operation whose label is missing. Add the token an
operation needs and send the command. The plugin's hook turns each token into
the user's permission prompt, and the user's answer there is the approval, so
don't ask in chat first. Say it plainly ("this needs `--allow-exec`; you'll be
asked to approve it"), not as your own decision to grant. Never add a token
the operation does not need. Each prompt covers one command; approval never
carries over. If a token draws no prompt (the hook is not loaded), stop and
ask in chat before sending another. **Subagents are read-only:** they
pass no token unless the user approves that write as it comes up. A dispatch
cannot authorize writes in advance. Read-only requests need no asking.

## Trusted scripts

A `.py` in a repo the site marks `trusted = true` (`[repos.<name>]`; these
are the script roots) runs with no token from `operate` up, whatever its
arguments. A match for that repo's `confirm` patterns needs
`--allow-write`. **Preview a confirm match first:** run the same command
without the flag that makes it match (`--execute`, `--send-it`), show the user
what it would act on, then send the real one. Skip the preview for a repo
that `hpc-trust list` shows with `preview: off`, or when the user says to.
**Never pipe an answer (`echo yes |`) into a script's own prompt**: if a
script stops to ask, tell the user. It is recognized only when its path is spelled one of the
ways in `reference/trusted-scripts.md`; check with `hpc-trust check -`. Code
reaches a root only through `git pull`, and each root must be a clean git
checkout or nothing runs (exit 78); a symlink as the script, or an untracked
file, is refused. Declare each git submodule under a root as a root of its
own. The payload shell has no `python` on its PATH: start a Python payload
with `export PATH="$HPC_PYTHON_BIN:$PATH"`, the one PATH change a trusted
script allows. Any other `PYTHONPATH`, `PATH` or loader variable set in the
payload means no trusted script runs. **Never trust a repo or loosen
its `confirm`**: those are the user's. The guard can't see inside these
scripts.

## Files and data

- **Read a repo's code in its local checkout.** For a `[repos]` entry with
  a `local` side (`bin/hpc-trust list` shows both paths), read and search
  its code there with Read and Grep: no round trip, no audit entry, and the
  same code, once `bin/hpc-sync-status NAME` says the two agree. If they
  don't (another branch, unpushed or unpulled commits), say so before
  reasoning from the local copy. Read on the cluster only what isn't
  there: files git doesn't track (registries, generated scripts, output),
  repos with no `local`, or a checkout that differs.
- **Never edit a remote file in place.** `hpc-pull` it, edit locally, and
  `hpc-push` it back: the push refuses drift and backs up first
  (`reference/file-transfer.md`). A push is also refused outside the write
  roots, for code into a script root, and for a git-tracked file. Tracked
  files change by commit, push, then `git pull` (`reference/git-sync.md`).
- **A new file (a job script, a config) is written locally and pushed**
  with `hpc-push LOCAL --to role:path --yes`. Not a heredoc through
  `hpc-run`: the guard doesn't read heredocs and refuses them.
- **Git on a `[repos]` checkout with a `local` side follows its mode**,
  whatever the write roots:
  `pull-only` allows `fetch`, `pull --ff-only` and `switch <branch>`;
  `two-way` adds `add`, `commit`, `push`, `merge`. A pinned `branch` refuses
  switching away. Before a git write there, the payload checks the cluster
  checkout's origin against the local repository's (exit 80 on a mismatch).
  A refused pull means divergence: stop and show the user.
- **Directories come down through `hpc-fetch`**, never a loop of pulls or a
  fetch script. Show the user the `--dry-run` count and size first. 2 GiB and
  2000 files at most; `--exclude` the oversized file.
- **The session transfer budget starts at 2 GiB.** Past it, a transfer sends
  nothing (exit 77). Tell the user the total and what the next transfer is
  for, and only with their agreement add `--budget-add SIZE`: one question per
  step, not per file.
- **`hpc-nc`** reads at most `nc_max_read_mb` (site file, default 100 MB) per
  call. Past that, the work belongs in a trusted script.
- **After moving data, report one summary** from
  `hpc-audit --transfers --session current`. Never list files one by one, and
  never discard a transfer verb's output.
- Pull into the repo the session started in, never an agent scratch. On the
  cluster write to the scratch role, never `/tmp`.

## Connection and sites

- **Never poll on your own.** Ask the user whether to poll, and how often.
- **Only the user can reopen an expired ControlMaster** (`ssh -fN <host>`,
  2FA). Test a claim of "unreachable" with `hpc-check`, never with a
  hand-rolled `ssh -O check`.
- **The site file is `~/.config/hpc-tether/sites/<name>.toml`** (templates
  in `sites/`); `~/.ssh/config` stays authoritative on connections. Each
  `[paths]` entry (common paths), and each repo's `role`, is `$HPC_<KEY>` in
  a payload: never hardcode a cluster path. SLURM only. `[policy]` (profile,
  write roots, protected paths), `[repos]`, `[filesystem]` and
  `[environment]` are the user's to set: never edit them to get past a
  refusal. A hook blocks writes to the site directory; to help, print
  the exact lines for the user to add (for `[repos]`, after finding the
  checkouts read-only and comparing origins).
