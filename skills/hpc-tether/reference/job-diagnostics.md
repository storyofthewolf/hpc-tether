# Diagnosing a remote SLURM job

For answering "is this job actually working?" Every probe here is read-only.
**Never** `scancel` or resubmit off the back of these without confirming with
the user first.

Transport is not discussed here — every command below runs through `bin/hpc-run`
or one of the `bin/hpc-probe` subcommands, and the `timeout 30` on every `srun`
probe is built into `hpc-probe` rather than being something to remember.

## The order that works

Cheapest and most decisive first. Stop as soon as the question is answered.

### 1. Does the job exist, and in what state?

```bash
bin/hpc-jobs                          # current queue
bin/hpc-jobs --job <jobid>            # merged squeue + sacct
bin/hpc-jobs --job <jobid> --brief    # one line, for checking again later
```

`sacct` retains MaxRSS and ExitCode after `squeue` forgets the job. A job that
failed in seconds with a nonzero ExitCode died at startup — go straight to the
log head (§3).

Rules for reading job state, and for every later check:

- **State comes from `sacct`**, which keeps finished jobs; `squeue` forgets
  them. `bin/hpc-jobs --job` merges both. Hand-rolled:
  `sacct -j <id> -X -n -P -o State,ExitCode,Elapsed`. `PENDING` and `RUNNING`
  are active; anything else (COMPLETED, FAILED, CANCELLED, TIMEOUT,
  OUT_OF_MEMORY, NODE_FAIL, PREEMPTED, ...) is terminal. `squeue` is only for
  extras on a live job (pending reason, start estimate); "Invalid job id"
  means the job left the queue, so read `sacct`.
- **Blank or unparseable output is a failed check, not "no change".** Say so
  and find out why (`bin/hpc-check`; did a command error?). Two in a row:
  stop and report. A watcher once logged blank lines for an hour while two
  jobs sat FAILED: `squeue -j` exited nonzero once the jobs left the queue,
  the payload's `set -e` ended it, and the `sacct` line never ran.
- **Keep each probe independent** in a hand-rolled payload: `cmd || echo
  "cmd rc=$?"`, with a labelling `echo` per command, so one failure cannot
  swallow the rest.
- **A terminal state is the headline** of a report: state, exit code and
  elapsed time, first.

**Trap: `TotalCPU` is not a progress signal.** It reads `00:00:00` for
processes not launched under `srun`, even when they are working hard. A batch
script running `python` directly in a loop will show zero CPU time forever.
Reading that as "not computing" is wrong. Use §4 instead.

### 2. What processes are actually running?

Direct `ssh` to a compute node is usually refused. Go through SLURM:

```bash
bin/hpc-probe ps --job <jobid>
```

This is the highest-value single probe. It shows the parent script, every
worker, its elapsed time, memory, and full command line — which reveals which
case/input each worker is on and therefore how far the job has advanced.

**Read the parent process line.** If a driver script iterates over inputs, its
command line names the input it is currently on. That is the ground truth for
"has it moved on to the next stage", far more reliable than the log.

**It is one node's view, not the job's.** An unqualified `srun` lands on a
single node, so on a multi-node job this shows that node's ranks and says
nothing about the others — a stall elsewhere is invisible here. The listing is
also filtered by user, not by job, so on a node running two of your jobs it can
include processes belonging to the other one. Match against the job's own
command line before concluding anything about "every worker".

### 3. What does the log actually say?

Write a script and run it through `bin/hpc-run`:

```bash
tail -40 "$HPC_RUNDIR/<logfile>"   # progress, per-item completion, end summary
head -25 "$HPC_RUNDIR/<logfile>"   # startup failures live here
```

**For a fast failure, read the head, not the tail.** SLURM epilogue writes a
resource-usage footer at the end of every job, so `tail` on a job that died at
startup shows only the footer. The actual error is in the first lines.

**A per-item summary is not a batch summary.** A driver that runs items
concurrently prints a completion block per item. Seeing one does not mean the
batch finished — check the parent process (§2) before concluding a stage is
done.

### 4. Is a process progressing or genuinely blocked?

```bash
bin/hpc-probe io --job <jobid> --pid <pid> [--interval 30]
```

This samples `/proc/<pid>/io` twice, computes the deltas, and prints a
`PROGRESSING` or `STALLED` verdict directly (exit 1 on `STALLED`). What it is
measuring:

- `rchar` — cumulative bytes read (monotonic). Sampled twice, ~30 s apart.
  **Rising = progressing.** That direction is conclusive: bytes moved.

  **Frozen is not.** It is the strongest single signal available here, but
  healthy work shows a flat `rchar` whenever it is not reading — a long
  compute-bound stretch, a memory-mapped file (`mmap` traffic bypasses these
  counters), a phase that only writes, a legitimate MPI barrier waiting on a
  peer, or a rank whose I/O another rank performs. Before calling a flat
  reading a hang, confirm it against `%CPU` from §2 (busy CPU with flat
  `rchar` is compute, not a stall), `wchar` (writing is progress too), and a
  second interval further apart. A `STALLED` verdict on one interval is a
  reason to look closer, not a diagnosis.
- `wchar` — cumulative bytes written. Near-zero with large `rchar` means the
  process is in an ingest phase and has not started emitting output yet.
- `syscr` — read syscall count. `rchar / syscr` gives mean read size; small
  reads (~8 KB) against a parallel filesystem that wants MB-sized reads
  indicate strided or unchunked access.
- `read_bytes` is **0 on Lustre/GPFS** because those are network filesystems
  with no local block device. Do not read that as "no I/O" — use `rchar`.

`STAT Sl` with `WCHAN futex_wait_queue` and low `%CPU` means threads are
parked waiting. That is normal for threaded work coordinating or waiting on
I/O — it is not evidence of a hang on its own. Pair it with `rchar`.

### 5. Where is the job actually reading from?

```bash
bin/hpc-probe fd --job <jobid> --pid <pid>
```

Resolves open file descriptors to real paths. Use this when a documented input
path and the real one may have drifted — it answers "where is this data
*actually* coming from" without trusting any config file.

### 6. Where is the time going?

**Read the script's own timing output before measuring anything.** Many
analysis scripts print a per-stage breakdown at the end. Check that first — it
is free and authoritative, and it routinely contradicts assumptions built from
process-level probes.

Only if there is no such breakdown, profile from the outside.

## If an srun probe times out

`srun --overlap` must obtain a slot from SLURM, and on a node already saturated
by the job's own work it can block indefinitely. `hpc-probe` bounds this at 30 s
and exits 75. An `srun` probe that returned quickly earlier in a run may hang
later as the node fills. On a timeout, fall back to probes that do not need a
slot (`bin/hpc-jobs`, `bin/hpc-read LOG --tail N`, `bin/hpc-ls` for the log's
size and time) rather than retrying.

## Format traps

Checking file format before theorizing about I/O:

```bash
ncdump -k <file>      # netCDF format
ncdump -hs <file> | grep -E '_ChunkSizes|_Storage'
```

**Empty output from that grep is itself the answer.** `_ChunkSizes` and
`_Storage` exist only on netCDF-4/HDF5 files. `ncdump -k` returning
`classic` or `64-bit offset` means netCDF-3, which has no chunking at all —
so there is no chunk cache to tune and no `chunks=` alignment to fix. Strided
small reads are inherent to that format, not a bug in the reading code.

## Failure signatures

| Symptom | Likely cause | Probe |
|---|---|---|
| Failed in seconds, ExitCode 1:0 | Environment/startup, not compute | `head` the log (§3) |
| `command not found` for an interpreter | Batch job got no interactive shell; alias/module never loaded | grep the submit script for `module load` |
| Running, log silent, no output files | Ingest phase | `rchar` rising (§4) |
| Running, `rchar` frozen, low CPU | Genuinely blocked | Escalate; ask before acting |
| Stage appears not to advance | Earlier stage still draining | Parent process command line (§2) |
| `TotalCPU 00:00:00` while working | Processes not under `srun` | Ignore it; use §4 |

## The environment trap

The most common fast-failure: a batch job gets no interactive shell, so
`~/.cshrc` / `~/.bashrc` never runs and any alias defined there does not exist.
An alias like `alias loadpy "module load python/..."` is unavailable, and a
`#!/bin/bash` script never sources `.cshrc` regardless.

The fix is a literal module load in the script — **and Lmod must be
initialized first**:

```bash
source /usr/share/lmod/lmod/init/bash
module load <module>
```

Without the `source`, `module` is undefined and `module load` silently does
nothing, producing a failure identical to having no load line at all. Verify
`type -t module` returns `function` when testing.

Place environment setup near the top of the script, after `set -u` (Lmod's
init references unset variables; under `set -u` before it, wrap in
`set +u` / `set -u`). Note that `hpc-run`'s prelude sets `set -Eeuo pipefail`,
so a script run through it needs that `set +u` / `set -u` wrapping around the
Lmod init.

## Preflight: the cheapest win

A job that dies in six seconds may have waited a day in the queue first. Run
the submit script's environment setup and any dependency check **on a login
node before `sbatch`**. Write it as a script and run it through `bin/hpc-run`:

```bash
# preflight.sh
set +u
source /usr/share/lmod/lmod/init/bash
module load <module>
set -u
type -t module
command -v python
python -c 'import <deps>'
test -d "$HPC_CASE" || { echo "missing: $HPC_CASE" >&2; exit 1; }
```

```bash
bin/hpc-run preflight.sh --allow-exec   # `source` and `python -c` are interpreter tier
```

This needs profile `improvise`, and `--allow-exec` makes the approval hook ask
the user, like any other token. Ten seconds of read-only work converts a day-latency failure into an immediate
one. Also verify paths the script depends on actually exist — a wrong path in
a submit script survives commits precisely because nobody checks it cheaply.

## Before concluding anything

Two mistakes made in the session this material came from, both stated
confidently before the evidence supported them:

- Read `TotalCPU 00:00:00` as "not computing". It was working; the number is
  meaningless for non-`srun` processes.
- Diagnosed chunk misalignment on files that had no chunks, and pursued an I/O
  bottleneck that the script's own timing table showed was 1.9% of runtime.

A documented constraint is a claim, not evidence. When something looks
impossible or broken, probe it before explaining the mechanism. Confident wrong
explanations are worse than "let me check" — they get written down and believed
later.

**Never `scancel` or resubmit on the strength of a diagnosis.** "Looks hung" is
frequently wrong — an earlier stage still draining looks identical to a hang
from the log. Report findings, propose the command, let the user run it.

The realistic failure here is not malice, it is over-helpfulness: a job looks
stalled, you resubmit, and now two large jobs write to the same output
directory — racing on files, double-spending the allocation, producing corrupt
results that still look plausible. `bin/hpc-jobs --name <name>` exits nonzero
when an instance is already queued or running; run it before proposing any
resubmit, and state what you found when you propose it.
