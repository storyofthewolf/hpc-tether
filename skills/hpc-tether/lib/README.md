# lib/ — promoted remote scripts

A one-shot script goes to `hpc-run` on stdin and is gone. A script you find
yourself regenerating belongs here instead: versioned, reviewed once, run as
`bin/hpc-run lib/<name>.sh`.

Rules for scripts in this directory:

- Bash, run remotely via `bash -s` — the same payload rules as any `hpc-run`
  input. Reference site paths through the `HPC_*` role variables, never a
  hardcoded cluster path.
- The guard and the site's profile still apply at run time: writes need
  profile `prepare` and `--allow-write`, interpreters and unknown commands
  (`python`, a nested `bash -c`, `srun`, `make`, `curl`) need `improvise` and
  `--allow-exec`, and the floor (deletion, writes outside the write roots,
  in-place edits) is refused no matter what is written here. Being versioned in this directory
  earns a script no exemption — the scan runs on its contents at every
  invocation.
- Keep each script one purpose, named for that purpose.

`hpc-run` refuses script files from `/tmp` and scratchpads precisely so that
recurring scripts end up here, in version control, instead of accumulating as
untracked temp files.
