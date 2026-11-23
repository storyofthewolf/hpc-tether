# Why the rules are what they are

SKILL.md states the rules. This file keeps the reasons and the incidents
behind them, so a rule can be questioned on its merits instead of followed
or dropped blindly. Read it when a rule seems to be in the way.

One thing first: the toolkit steers an agent and catches common mistakes. It
is not a security boundary. The agent runs on the user's machine, as the
user, next to an open SSH connection, and a scan of shell text cannot stop a
payload written to get past it. The backstops are the user's approval
prompts, the account's own permissions, and backups. "Known limits", at the
end, lists what the guard does not catch.

## The toolkit stays on the local machine

The guard, the audit log, the confirmation tokens and the approval hook all
live on the laptop. The cluster receives a payload on stdin and nothing else.
A copy of the toolkit on the cluster would be a second way in, with none of
those checks around it.

## Payloads go on stdin

A quoted `ssh host 'cmd'` passes through a local shell first, often tcsh,
which re-parses quotes and `$`. `hpc-run` refuses a command string, so
nothing local ever parses what goes to the cluster. Script files in `/tmp` or
a scratchpad are refused because "write a throwaway file, pass its path" is
how 31 orphaned `hpc_*.sh` files once accumulated. A script worth keeping
belongs in `lib/` under version control.

## The watchdog, and sizing `--timeout`

Every payload runs under `timeout` on the cluster, sized just below the local
`--timeout`, so a hung command dies on the login node rather than lingering as
an orphan. GNU `timeout` signals the process group; a child that leaves the
group (`setsid`, a daemon, a `disown`ed job) can survive.

Inside the watchdog the script reaches its `bash` on file descriptor 3, and
stdin is `/dev/null`. While the script was on stdin, a command that reads
stdin (`srun`, a nested `ssh`, `cat`, a script asking "Proceed? [y/N]")
consumed the lines after it: they never ran, and the payload still exited 0.

A foreground Bash call in Claude Code is capped at 10 minutes. A `--timeout`
chosen to fit under that cap is what killed 10–30 minute analysis runs
(`trend.py` over a long series, a `my-script.py` scan). Past about 540 s on a
foreground call, the Bash tool kills the local side first, and the remote side
is left to ssh's hangup instead of the watchdog. Hence: background the call,
and give the work the time it needs.

## Fixed tools first

A fixed tool sends a script the toolkit wrote, with the agent's arguments
passed as data (`set -- ...`) and placed after `--`, or, for `find`, which
takes no `--`, as an absolute path. It needs no guard pass, its output is
bounded, and it is quicker to call than a payload is to write. The audit log
(2026-09 to 2026-11) showed agents writing payloads anyway where a tool was
close: 37 single-file reads against 7 `hpc-read` calls, 103 queue queries
(61 of them the same `sacct` line repeated as a job watch), and 74 listings
with no tool to go to. `hpc-jobs --brief`, multi-file `hpc-read --grep` and
`hpc-ls` fill those gaps.

## Strict mode, and pipelines cut short

Every payload runs under `set -Eeuo pipefail`, so a failure stops it at
once rather than letting later lines run on a bad state, and an ERR trap
names the line: a silent stop read like a transport fault.
`pipefail` alone also stopped a payload when `head` exited early and the
command feeding it died of SIGPIPE (exit 141), which nothing had done wrong:
25 payloads in the audit log. The trap now lets a pipeline through when its
only nonzero statuses are 141, and still stops on any other status anywhere
in it. It does so by turning `-e` off and ending the payload itself from
then on; the cost is that a later `set +e` no longer turns strict mode off.
A cut pipeline is still false in an `if`.

## The duplicate-job check

Two jobs writing one output directory race on files, double-spend the
allocation, and produce corrupt results that still look plausible. The check
runs on the cluster in the same connection as the submit, just before the
payload's first line. That narrows the gap between checking and submitting;
it does not close it, since another session can submit in between. It runs
once per payload, so a payload may name a job only once (exit 79 otherwise).
It fails closed, because an empty answer from a failed `squeue` once read as
"clear".

## The audit log

On the cluster everything arrives as the account holder. Nothing there
distinguishes a command an agent issued from one typed by hand, except this
log. It is also the eval set: what agents actually send is how the toolkit is
judged and tuned. At first only a payload's first line was kept, and
for half the payloads that line was setup (`export PATH=...`), so the log
could not say what they did. Now every `hpc-run` payload's text is kept as
the agent wrote it, refusals included, with the profile level it needed. A
payload can carry data, so the file stays on this machine at mode 0600. Past
65,536 characters a body is cut and the line says so; code the guard can't read
(improvise) is kept whole or not sent. The fixed tools send the toolkit's own
scripts, so their lines record the arguments instead.

## Observe is fixed tools only

At `observe` `hpc-run` refuses every payload (exit 77, audited like any
profile refusal). The fixed tools still run: their remote scripts are written
by the toolkit and the agent's input reaches them only as quoted arguments,
so nothing the agent wrote as shell is sent, and nothing needs parsing. This
replaced "the guard recognizes a read" as the observe rule. The
guard's read list had to be right for every spelling of every command, and
two reviews found it was not (`sed w`, `sort -o`, `xxd -r`). Free-form shell
starts at `prepare`, where the user has already chosen to approve what the
agent writes.

## Profiles live in the site file

A command-line flag is something the agent can pass itself. A level the
agent could raise would mean nothing, so the level is set only in a file
the user edits, and a refusal tells the agent to ask rather than edit it.
The same reasoning keeps the rest of `[policy]` (write roots, protected
paths, `nc_max_read_mb`) and which repos are trusted out of reach of any
flag. Two hooks back this: one refuses
an edit of the site files, and a Bash command that sets `HPC_TETHER_SITES`
(which would swap in site files the agent wrote), `HPC_TETHER_AUDIT` or
`HPC_TETHER_MANIFEST`. What they can't stop is a program the agent writes
that loads its own configuration; that is the sandbox's to prevent.

## What the guard is, and is not

The scan is lexical: it reads the payload as text and refuses the spellings
it recognizes. That makes it a good detector of habit, and of payload text
steered by something read along the way: the two ways a deletion realistically
reaches a cluster from here. It does not stop a payload written to get past
it (`eval` through variable indirection, a base64 blob decoded at run time,
anything inside a file it cannot open), and once a payload runs an
interpreter the guard has no view into what it does. Describe it as a
mistake-detector, never as a technical impossibility. The deletion check also
runs in the transport, so it applies to every verb, not only `hpc-run`.

Two reviews each found a dozen more spellings past
it, nearly all one bug: the guard parses a command's options differently from
the real tool. Each round of fixes added rules and each review found more, so
the guard is frozen. Only gaps an agent might plausibly write, and
false refusals of harmless reads, get fixed; the rest are listed under "Known
limits". The audit log of real use, not another review, is the test set.

## How the guard is built

Three parts, so the list of gotchas can grow without touching the parsing:

- `hpctether/shell.py` parses a payload into commands: logical lines,
  segments split at shell separators outside quotes, substitution bodies,
  tokens, wrappers and keywords stepped over, and the directory a
  `cd DIR &&` puts the next command in. It decides nothing.
- `hpctether/rules.py` is the gotcha list: small rules tried in order, the
  first to claim a command deciding it, each with the incident or review
  that put it there. `hpctether/commands.py` holds the tables they look up.
- `hpctether/guard.py` joins them: payload-wide checks (site roles, loader
  variables), then the rules on every command, then the report.

A new gotcha is a rule in `RULES` or a line in a table. The split was a pure
move: every payload in the audit log and the tests classified
identically before and after.

## Unknown commands fail closed

At first an unrecognized command passed as a read. Each dangerous
spelling then needed its own guard branch, found after the fact, and
`ncks -O`, `cdo` and `nccopy` wrote files with no token. Refusing what the
guard doesn't know ends that, and makes a new command a data line in
`hpctether/commands.py` rather than code.

Options fail closed the same way. A command listed as a read
was a read whatever flags it carried, and `sed -n 'w FILE'`, `xxd -r` and
`sort -oFILE` wrote outside every root at `observe`. Now
each reading command lists the options it may carry, `sed` is a read only
for a program the guard can parse, and anything else is unknown. The cost is
a refusal that names the option; the audit replay changed for 2 of 898
payloads.

## Path text, links and wrappers

Confinement compares path text; it cannot stat the cluster. Three things
follow. A link is a second name for its source, so `ln` needs its source in
a write root too: a link made to an outside directory would carry later
writes there. A link that already exists is not seen, and is followed.
A wrapper that changes where a command runs (`env -C`) or supplies its
arguments at run time (`xargs`, `parallel`) leaves the text describing a
different command than the one that runs. `env -C` is unknown, and behind
`xargs` or `parallel` only a short list of commands with no writing option
(`grep`, `wc`, `cat`, `head`, `tail`, `ls`, `stat`, `md5sum`, `basename`,
`dirname`, `echo`) is known; `echo -o/out f | xargs sort` would otherwise read
as a bare `sort`. And a site
role (`$HPC_SCRATCH`) is resolved from the site file, so the payload may not
set one; the prelude also marks the roles `readonly`.

## Interpreters are their own tier

`python -c "import os; os.remove(...)"` is exactly the `rm` the guard refuses,
in a language it cannot classify. Any command that runs text (an interpreter,
`eval`, `bash -c`, a file run by path, `make`, a network fetcher) escapes every
other check, so these are classified by capability, not by spelling.

## Traversal bounds

On a shared parallel filesystem the cost of `find` or `du` is the tree walked,
and unbounded walks on a login node trigger resource warnings. `du -s` and
`du --max-depth` bound what is printed, not what is walked: they pass the
check without being cheap.

## `hpc-probe` may use `srun`

It runs `srun --jobid=ID --overlap` itself, with fixed, read-only payloads
under a 30 s remote timeout. `--overlap` must still obtain a slot, so on a
saturated node a probe can block and time out. That is the probe failing,
not the job.

## Renames are write-tier inside the write roots

Refusing every `mv` once stranded a `/remote-control` session until the user
reached a physical terminal. A wrong move is undone by moving
back; a wrong `rm` is not. So `mv` inside the user's own roots is an ordinary
write, and deletion stays forbidden everywhere. A shallow root (`/`, or one
level deep) would disarm confinement for the whole filesystem, so the loader
refuses one.

## Tokens declare; the prompt approves

A token is a flag the agent passes itself. It still does two jobs: it
declares intent, so a payload that writes when the agent thought it only read
is refused, and it leaves an audit record. Consent comes from the approval
hook's permission prompt, which the agent cannot answer, and which allow
rules and "don't ask again" don't skip. The hook matches text, it does not
parse: any command that names an hpc-tether tool and a token asks, in any
wrapping. The parser it replaced missed `bash -c '...'` and `env -i ...`
. A command that only mentions a token costs a prompt. Scoped grants to subagents were
removed: a written grant was the agent's own reading of the
user's words, not the user's approval.

## Git writes that can be undone

A git write that git itself can undo (`fetch`, `pull`, `clone`, `add`,
`commit`, `merge`, a `switch` that discards nothing, a `stash` that drops
nothing, a new branch or tag) needs no token. It is still write tier for the
profile, still confined to the write roots or its `[repos]` mode, and still
in the audit log, but the user is not asked. Git writes that can't be undone
were refused already (force push, `reset --hard`, `clean`). Those that
publish, change what git runs, or can discard uncommitted work or the reflog
keep the token: `push`, `config`, `remote`, `checkout`, `restore`, `reset`,
`rebase`, `gc`, `stash drop`, `branch -D`. The reason is prompt load: a
prompt the user approves without reading is no approval, and `pull
--ff-only` was a routine one.

## Trusted scripts

The guard is strict where it can read (hand-written shell) and permissive with
the user's own committed Python, which it cannot read either way. See
`trusted-scripts.md` for what keeps a trusted script trusted.

The tracked-file check runs `git ls-files --error-unmatch` in the root on the
cluster. It refuses a script that is a symlink, since a committed link can
point at an untracked file in scratch. It reads the root's own index, and a
git submodule appears there only as a gitlink, so a script inside one is not
found. Declare each submodule as a script root of its own.

## Pull, edit locally, push

Local edits get real tools and a diff, and a push keeps a backup and checks
the remote didn't change meanwhile. An in-place `sed -i` has none of that.
The author's audit log held no in-place edit in seven weeks, so refusing them
costs nothing.

## The transfer budget

The per-call cap (2 GiB) bounds one transfer. A loop of small pulls could add
up unseen, so a session also has a budget, raised one step at a time with the
user's agreement.

## Never `ssh -O check` by hand

`-O check` resolves its argument through `~/.ssh/config`. A name with no
matching `Host` stanza yields `No ControlPath specified for "-O" command`,
which means "this alias is unknown", not "the master is down". Reporting a
live cluster as unreachable once stopped real work for no reason. `hpc-check`
resolves the host through the site file.

## Known limits

What the guard and its hooks deliberately do not catch. Each is a spelling or
route an agent is unlikely to produce by habit; closing them one at a time did
not converge (see "What the guard is, and is not"). None is a promise.

- Abbreviated long options: `cp --target-d=DIR` for `--target-directory`.
- Clustered options: `env -iC/dir`.
- Option forms of `tar`, `xz`, `xxd`, `zip`, `split`, `install` and `ncks`
  that write a file or run a program.
- `cp -s` and `cp -l`: links to files outside the write roots.
- `install -d` with several operands.
- Options hidden in shell variables (`$opts`).
- A role or override variable set through `${NAME:=...}` or a `for` loop.
- The approval hook missing a token behind `bash<<EOF` with no space.
- Brace expansion (`--allow-{write,exec}`) and `${x:---allow-write}`, which
  the approval hook does not expand.
- A multiline `$(...)` in the raw-ssh hook.
- A symlink that already exists inside a write root: paths are compared as
  text and the link is followed.
- A program the agent writes reaching the SSH socket, or loading its own
  configuration.
- The agent's own local shell reaching the ControlMaster socket directly. This
  is the one gap at every profile; only Claude Code's sandboxing closes it.
