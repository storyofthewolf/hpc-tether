# Pull → edit locally → push

Significant edits to remote files never happen over ssh. The file comes down,
gets edited with real local tools, and goes back through a checked, reversible
write. `bin/hpc-pull` and `bin/hpc-push` are that protocol as tooling; this
file is the judgment around them.

## The protocol

1. **`hpc-pull role:path/file` (or an absolute path).** Pull refuses to
   overwrite an existing local file without `--overwrite` — the local copy may
   hold edits that exist nowhere else — and refuses files over 2 GB without
   `--force-size`. On success it records a manifest entry
   (`~/.cache/hpc-tether/manifest.json`): remote path, size, mtime, and the
   content sha256.
2. **Edit locally** with Read/Edit/Write, run local checks, show the user the
   diff when the change is theirs to approve.
3. **`hpc-push file --yes`** — `--yes` makes the approval hook ask the user,
   and their answer is the approval for this write. Push re-checksums the remote first and **refuses if it changed since
   the pull**; the drifted copy may be the only copy of someone else's edit.
   The new bytes then land beside the destination, and one remote call
   re-verifies the checksum, copies the current file to
   `<scratch>/hpc-tether-backups/<name>.<stamp>.<hash8>`, and renames the
   upload into place. So every push can be undone, and the destination is
   never partially written. Without `--yes`, push runs every check (write
   roots, script roots, git tracking, drift) read-only and either refuses or
   says what it would do, so run it that way first when a refusal is likely,
   before asking the user to approve.

   **The re-verify is not redundant with the first check.** It is what makes
   the backup meaningful: a backup taken before a network transfer describes a
   state that may already be stale by the time the file is replaced. Checking
   and renaming in the same call shrinks the window from "the duration of the
   upload" to two adjacent commands. If the destination changed in between,
   the push aborts and the remote is left untouched; the uploaded temp stays
   beside it with its path printed, because the toolkit never deletes on the
   cluster — report both checksums and the temp path, and let the user
   reconcile.

## Judgment on top

- **Drift refusal is a stop, not a hurdle.** Do not re-pull and force through
  in one motion. Show the user both versions' provenance (pulled checksum vs
  current), let them decide how to reconcile.
- **`--force` (push without a manifest entry) needs its own confirmation.**
  It exists for pushing to a destination that was never pulled; the backup
  still happens, but drift cannot be assessed. Say so when proposing it.
- **Tracked-repo files do not go through push at all.** Anything inside a git
  checkout syncs via local → GitHub → HPC `git pull` (`reference/git-sync.md`).
  pull/push is for config files, run scripts, namelists, and data — things
  outside version control.
- **A directory comes down through `hpc-fetch`.** `hpc-fetch SRC [DST]
  --dry-run` lists and sizes the tree with one bounded `find` and stops;
  show the user that summary, then run it again without `--dry-run`. It is
  rsync in the read direction with no delete flag, inside the session
  transfer budget (exit 77 past it; `--budget-add SIZE` with the user's
  agreement), capped at 2 GiB and 2000
  files — measured after `--exclude`, so excluding the one oversized file
  (bare name matches at any depth, leading `/` anchors at the root, trailing
  `/` means a directory; `+ ` / `- ` / `!` rule spellings are refused) is how to fetch the rest of a tree, never a loop of
  `hpc-pull` — refusing the skill tree and temp dirs as destinations, and
  refusing a non-empty destination without `--merge`. It records a
  provenance entry in the manifest (no per-file checksums). Nothing goes
  back up as a tree: `hpc-push` stays one file with a drift check.
- **Data flows down, not sideways.** If output data is small enough (a few
  GB), pull it and analyze locally. When it is not, condense on the cluster
  first (insitu reduction) and pull the condensed product. Pull into the repo
  or working directory the session started in, never an agent scratch dir.
- **Backups in scratch are not archives.** Scratch is typically purged; a
  backup there covers "undo this push", not long-term retention.
