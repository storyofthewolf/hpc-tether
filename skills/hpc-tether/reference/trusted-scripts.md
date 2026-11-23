# Trusted scripts: what a script root promises, and what it can't

A script root is a repository you wrote and maintain, marked
`trusted = true` in its `[repos.<name>]` entry in the site file. (Older site
files list them as `script_roots`; that still loads, with a note from
`hpc-check`.) From profile `operate` up, an agent runs any `.py` under one with no
confirmation token, and the guard does not look inside it. That is the point:
the guard reads hand-written shell, and it cannot read your Python either way.

"Trusted" means **you reviewed this code when it was committed.** The toolkit
holds up its side of that in code. Your scripts have to hold up the rest.

## What the toolkit enforces

- **No code is written into a root except through git**, at any profile. An
  agent can't push, copy, move, link, redirect or unpack a code file into a
  script root (`hpc-run`, `hpc-push`). Data files (`.csv`, `.toml`, `.json`,
  `.nc`, ...) can be written there.
- **A trusted script runs only from a clean checkout.** Before a payload that
  runs one, each root it uses must be a git checkout with no modified and no
  untracked code file, and the script being run must itself be a tracked
  file, not a symlink (a committed symlink can point at an untracked file).
  Otherwise nothing in the payload runs (exit 78). A git submodule under a
  root must be declared as a root of its own: the check reads the parent's
  index, which tracks only the submodule's gitlink, not the files in it.
  Gitignored files don't count: ignoring a path says it holds generated
  artifacts.
- **No environment tampering.** A payload that sets `PYTHONPATH`, `PATH`,
  `LD_PRELOAD`, `PYTHONHOME`, `BASH_ENV` or another loader variable runs no
  trusted script. Those variables decide what code a script imports or calls.
  The one exception is the site's own `export PATH="$HPC_PYTHON_BIN:$PATH"`.
- **Trust is yours to grant.** The agent never marks a repo trusted.

## How a trusted script must be spelled

The guard recognizes a trusted script only by a path it can name for certain:

- absolute: `python /home/you/code/analysis/plot.py`
- a site role: `python $HPC_REPO/my-script.py check CASE`
- `~/`, when the site declares a `home` path role:
  `python ~/code/analysis/plot.py`
- relative, in the pipeline directly after `cd <absolute, role or ~> &&` on
  the same line: `cd ~/code/analysis && python summarize.py`, or
  `cd ~/code/analysis && python summarize.py | tail`

`~user`, `$HOME` or any other variable, a glob, a `..`, a `cd` joined by `;`
or on its own line, or another `&&`, `;`, `||` or `&` between the `cd` and
the script leaves it
unrecognized, and so treated as inline code. So does `~` in a payload that
assigns `HOME`. `python -c`, `python -m`, `python - <<EOF`, a nested `bash -c`
and a `.py` outside every root are always inline code, including one written
to scratch. A payload may not reassign a site role (`HPC_REPO=...` is
refused). A trusted repo's `confirm` patterns, `fnmatch` over
`<path relative to its root> <args>`, put a match back behind
`--allow-write`. They apply to that repo only, so a pattern written for one
repo's flags can't catch another's. (An old top-level `script_confirm` list
applies to every root.) `hpc-run --no-trust` classifies as if no roots were
declared, for diagnosis.

## What your scripts must do

The guard can't see these. A script that breaks one hands an agent a way to
run unreviewed code with no token:

1. **Never execute from a path the agent chooses or can write.** A flag or a
   relative default that names a directory whose files get run (`build.py
   make --scripts-dir DIR`, or `'build_scripts'` resolved against the working
   directory) runs whatever the agent put there. Anchor such paths to the
   repository: `os.path.join(os.path.dirname(os.path.abspath(__file__)), ...)`.
2. **Never put data into shell or `eval` unquoted.** A matrix value
   interpolated into a generated script (`f"./xmlchange RUN_TYPE={run_type}"`)
   makes the matrix code. Use `shlex.quote`, or check values against a strict
   pattern or an allowed set. No `subprocess(..., shell=True)` on data values.
3. **Never import from, or add to `sys.path`, a location set by arguments or
   the working directory.**
4. **Generate code only into gitignored paths inside the repository**, and
   only from your own templates. The clean-checkout check trusts ignored
   paths for exactly this reason.

## Gating consequential scripts (optional)

Most trusted repos need no gate. An analysis or plotting script reads model
output and writes figures or tables into scratch: let it run. Add a gate
only to a script whose actions are expensive or hard to undo, such as
submitting jobs, rebuilding or deleting cases, or editing a run in place.
When you do, design it so the gate is something hpc-tether can see:

1. **Name every action in the command line.** With no action flag, the
   script previews (reports what it would do) and changes nothing. One
   flag acts. A `confirm` pattern matches command text, so an action
   switched on by a config file, a matrix value or an environment variable
   (`submit: true` in a YAML file) passes every pattern unseen.
2. **Don't ask at run time.** No `input()` for a decision. Under `hpc-run`
   the script's stdin is `/dev/null`: a prompt either stops it or teaches
   the agent to pipe `yes` into it, and neither one protects anything. The
   flag is the confirmation. If a prompt stays, end-of-file must mean no.
3. **Behave the same for every caller.** Don't branch on whether stdin is a
   terminal, on environment variables, or on who is calling. You approve a
   command, and that approval means something only if the command does the
   same thing whoever sends it.
4. **Use one flag per kind of risk, with the same names in every repo.**
   `--execute` changes things. A separate flag (such as `--send-it`) covers
   what spends allocation or can't be undone, so it can be gated on its
   own. Shared names keep each repo's `confirm` list to the same
   one or two patterns: `confirm = ["* --execute*", "* --send-it*"]`.
5. **Make flags exact.** Python's argparse accepts `--exec` for `--execute`
   unless the parser sets `allow_abbrev=False`, and a pattern would miss
   it. Set that on every parser and subparser, and give action flags no
   short aliases (`-x`).
6. **Make the preview faithful.** It is read-only, cheap, and lists exactly
   what the action would touch, because it is what your approval rests on.
   When a selection can change between the preview and the action (a prefix
   or glob), an `--expect N` that makes the action refuse unless N items
   match ties the two together.
7. **Fail loudly.** A refused or partial action exits nonzero with a
   one-line reason. A preview with nothing to do exits 0.

The agent's side of this: before sending a confirm match, it runs the same
command without the action flag and shows you the preview. Then it sends the
real command, which you approve. Set `preview = false` in a repo's entry to
skip that step for the repo (`hpc-trust list` shows `preview: off`), or say so
in the session.

## A scope option for sandbox work (optional)

A gated script can also say, in its own command line, that it writes only
under one directory. Give it an option for that (`--within DIR`): every
action refuses a target outside `DIR`, and anything the script itself
writes (generated scripts, logs) goes under `DIR` too. Then name the option
in the repo's entry:

```toml
[repos.my-tools]
trusted        = true
confirm        = ["* --execute*", "* --send-it*"]
scope_option   = "--within"
always_confirm = ["* --purge*"]    # deleting still asks, scope or not
```

A `confirm` match carrying `--within` with a directory inside `[sandbox]
root` then needs no token, so it doesn't ask. hpc-tether can't check that
the script honours the option; it trusts that the same way it trusts the
script at all. Keep the option an ordinary feature of the script, with no
knowledge of hpc-tether: it should mean the same thing from your own
terminal. If the script's idea of the directory and the site file's
`[sandbox] root` drift apart, the option names a directory outside the
sandbox and the call asks again, so a mismatch fails toward asking.

## What stays open

- **Git is the review, and the toolkit can't check that you reviewed.** If an
  agent can commit and push to the repository's remote from your laptop, then
  `git pull` on the cluster brings its code in as trusted. Showing the incoming
  commits in a prompt was considered and rejected: a prompt you
  approve every time is not a review. Keep agent-written changes to these
  repositories behind your own review (a branch and pull request you merge,
  or reading the diff before you push).
- **A script that breaks the rules above.** Until it's fixed, a `confirm`
  pattern in its repo's entry puts the risky form behind a prompt, for
  example `confirm = ["*build.py *make*"]`.
- **A script whose flags can be abbreviated.** A `confirm` pattern for one
  of its flags misses the short spellings until the parser sets
  `allow_abbrev=False` (point 5 under "Gating consequential scripts").

`bin/hpc-trust list` shows the roots and whether they are active at the
site's profile; `... | bin/hpc-trust check -` shows how a payload classifies.
