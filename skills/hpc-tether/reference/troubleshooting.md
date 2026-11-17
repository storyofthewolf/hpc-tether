# When someone bypassed hpc-run

`bin/hpc-run` passes an argv list straight to `execve` and pipes the payload to
`bash -s` on stdin. No local shell and no remote login shell parses any of it,
so none of the failures below can occur through it. If you are seeing one, a
raw `ssh` command was constructed somewhere. **The fix is to stop escaping and
go back through `hpc-run`** — not to add another layer of backslashes.

## Two shells get a say, and the local one goes first

The Bash tool runs under the user's login shell, which here is **tcsh**. Inside
a *double-quoted* ssh argument, `$`, `!`, and backslash-newline are expanded or
rejected **before ssh ever runs**. The remote login shell may also be tcsh,
which does not understand bash redirections like `2>/dev/null`.

The tell for a local failure: an error that names no host and shows no remote
output at all.

| Error | Origin |
|---|---|
| `Bad : modifier in $ (...)` | local tcsh — history/variable expansion |
| `Illegal variable name`, no remote output | local tcsh |
| `Unmatched '` / `Unmatched "` | local tcsh |
| `Ambiguous output redirect` | remote tcsh — bash redirection under csh |
| `Illegal variable name` *after* remote output | remote tcsh |

Each round of added backslashes has to survive two parsers, and the failure
mode gets less legible every time. This is why `hpc-run` refuses a command
string: there is no quoting left to get wrong.

## Recovering

1. Put the commands in a file (or pipe them on stdin) and run
   `bin/hpc-run script.sh`. Nothing is quoted, so nothing can be mangled, and
   `--dry-run` shows the exact payload before it goes anywhere.
2. If the error was `Ambiguous output redirect`, the old workaround was a
   `bash -c` wrapper. Don't reintroduce it — `hpc-run` already pipes to
   `bash -s`, which covers the remote-tcsh problem for free.
3. Do **not** ask the user to run a read-only command and paste the output
   back. With a live master and `hpc-run` there is nothing to fight; run it.

## Do not nest quotes to write remote files

Writing a remote file via nested quoting — `cat local | ssh host 'cat > remote'`,
a heredoc piped into `cat >`, an in-place `sed` over ssh — fails in ways that
are slow to debug and whose errors do not point at quoting. This is moot in
practice: remote writes only ever originate from a reviewed local copy with
explicit per-write authorization, never from a command constructed and executed
in one shot.

## Connection-level failures

| Symptom | Meaning | Action |
|---|---|---|
| `hpc-check` exits 69 | master expired or never opened | ask the user to run the `ssh -fN <host>` it names |
| Hangs with no output | `BatchMode` missing — a raw ssh fell back to an interactive prompt | use `hpc-run`, which always sets it |
| Worked, then stopped after a suspend or VPN flap | stale master | `ssh -O exit <host>`, then the user reruns `ssh -fN <host>` |
| Exit 255 | ssh transport failure | check the host alias resolves in `~/.ssh/config` |
| Exit 77 | guard or profile refusal; nothing was transmitted | read the printed violations. `REFUSED by profile` → the site's level doesn't allow it; tell the user which level would, never edit the site file. Forbidden (every deletion, any write outside the write roots, in-place editing) → hand the command to the user; a write target the guard "can't place" → spell it absolutely or as `$HPC_ROLE/...`; traversal → add bounds; a heredoc writing a file → write it locally and `hpc-push --to`; unknown or interpreter → prefer a narrower command, else get confirmation and `--allow-exec` (profile `improvise`); write → get confirmation, then `--allow-write`. Never split a payload to evade the scan, and never re-spell a refused operation in Python or a nested shell |
| Exit 64 from `hpc-run`, "is not defined for site" | the payload uses a `$HPC_*` name the site does not define (a guessed role such as `$HPC_MYTOOLS`); nothing was transmitted | use a name from the printed list, or the full path (`bin/hpc-trust list` shows each trusted repo's). A missing `$HPC_PYTHON_BIN` means the site file sets no `python_bin`: tell the user |
| Exit 79 | a job named on an `sbatch -J` line is already queued or running, or `squeue` failed, or the payload names one job twice (or in a loop); nothing ran | do not resubmit; read the job list on stderr with the user. If it is the job being replaced, cancel it (with approval) in its own payload first |
| Exit 78 | a script root the payload uses is not a clean git checkout, or the script being run is not tracked there; nothing ran | stderr names the untracked or modified code file. It is the user's to commit or gitignore; never move it aside to get the script to run |
| `hpc-push`: "whether git tracks ... is unknown" (exit 77) | git is missing on the cluster's PATH, or failed in the destination's directory | a push never overwrites a file git might track. Run `git status` in that directory through `hpc-run`; a broken checkout is the user's to repair |
| Exit 124 + "remote watchdog killed the payload" on stderr | the command hung and was killed **on the cluster** | the payload's process group was killed; shorten the operation, or raise `--timeout` (the watchdog scales with it) and run the Bash call in the background if it goes past ~540s. A child that left the group (`setsid`, a daemon) can survive — check if the payload backgrounded anything |
| Exit 124, no watchdog notice | local transport stall; remote may still be running | do not assume it died; check before retrying |

An agent cannot reopen an expired master — that needs interactive 2FA. Do not
explain why the remote is unreachable without having run `bin/hpc-check` first;
a documented constraint is a claim, not evidence.

## A 124 usually means the loop was too serial, not too slow

The common cause of a watchdog kill is a payload looping over many files on a
shared parallel filesystem. Each open is a metadata round-trip, and they add up
far faster than a local test suggests — a loop that takes a second on a laptop
can take minutes there. Rough budget: **6–8 files per payload** for one cheap
read each, fewer when the loop makes multiple passes per file.

Split the loop before touching `--timeout`: by year, by case, by subdirectory.
Confirm the output row count matches the input file count — a short table is
itself a check. Raising the timeout only lengthens the serial loop on a login
node, which is the thing the watchdog exists to stop. Better still, let the
cluster aggregate: one `ncra`/`ncwa` over a glob beats N round-trips, though
writing an output file needs profile `prepare`, approval, and an output path
inside a write root.

## Empty output is not a result

**Read the exit code before reading the output.** A `grep` with no hits, an
`ls` on an empty directory, and a killed payload can all leave you looking at
nothing, and the wrong reading of a kill is a plausible scientific finding —
"the field is empty," "no jobs are queued," "that string doesn't appear."

The exit code settles it, and the table above says how: 0 is a genuine empty
result, 124 with the watchdog notice was killed remotely, 124 without it means
the transport stalled. Do not infer which one happened from the shape of the
output — payload output streams as it is produced, so a kill mid-loop leaves
the earlier lines on screen and looks exactly like a short result. Never report
an empty body as data without having checked the code that came with it.

## Two payload-text traps

**`<` or `>` inside a quoted pattern reads as a redirect.** The guard's scan is
lexical, so `diff a b | grep -E "^[<>]"` is refused at write tier (exit 77,
`redirects output to ']"'`). Nothing is sent, so it errs on the safe side. Do not
answer it with `--allow-write`: the payload writes nothing, and the token
would approve a write that is not there. Spell the pattern without the
characters instead, e.g. keep diff bodies with `grep -vE '^(---|[0-9])'`.

**One failing pipeline ends the payload.** A payload stops at the first
pipeline whose status is nonzero: `diff` of two files that differ (exit 1),
`grep` or `grep -c` with no match (exit 1). Every later section is silently
skipped, and the payload exits 1. Seen three times in one session, each time
looking like a short result. When a payload has several sections, end each
check that may legitimately "fail" with `|| true`, or give it its own call.
Then read the exit code as above.
