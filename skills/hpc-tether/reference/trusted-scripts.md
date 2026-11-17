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
- relative, directly after `cd <absolute, role or ~> &&` on the same line:
  `cd ~/code/analysis && python summarize.py`

`~user`, `$HOME` or any other variable, a glob, a `..`, a `cd` joined by `;`
or on its own line, or anything between the `cd` and the script leaves it
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
- **A script whose flags can be abbreviated.** Python's argparse accepts
  `--exec` for `--execute` unless the parser sets `allow_abbrev=False`, and
  a `confirm` pattern matches text. Give a repo `allow_abbrev=False` before
  writing a `confirm` pattern for one of its flags.

`bin/hpc-trust list` shows the roots and whether they are active at the
site's profile; `... | bin/hpc-trust check -` shows how a payload classifies.
