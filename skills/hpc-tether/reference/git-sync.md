# Git sync: local, GitHub, cluster checkout

Most people move code to the cluster like this: edit and commit on the laptop,
`git push` to GitHub, then `git pull` in the cluster checkout. This toolkit is
built around that cycle. This file covers the setup (the site's `[repos]`
table), what the guard allows in each mode, how to read `hpc-sync-status`,
and the judgment on top of it.

## `[repos]`: which repositories are mirrored

The user lists each repository on the cluster in their site file
(`~/.config/hpc-tether/sites/<site>.toml`), one entry each. An entry with a
`local` side is a mirror: it is what this file is about.

```toml
[repos.mymodel]
remote = "/home/me/mymodel"
local  = "~/code/mymodel"
branch = "main"

[repos.analysis]
remote  = "/home/me/analysis"
local   = "~/code/analysis"
mode    = "two-way"
trusted = true
```

| Key | Meaning |
|---|---|
| `remote` | the checkout on the cluster: absolute, at least two levels deep |
| `local` | the repository on this machine. Without it the entry is not a mirror: no sync rules, and `hpc-sync-status` skips it |
| `mode` | `pull-only` (default) or `two-way`; see below |
| `branch` | optional. Pins the checkout to one branch: switching away is refused. Leave it out to test other branches on the cluster. |
| `trusted`, `confirm`, `role` | not about sync: see `reference/trusted-scripts.md` and `sites/example.toml` |

The older one-line form, `name = { local = "...", remote = "..." }` under
`[repos]`, is the same table and still loads.

**Only the user writes this table.** Each entry lets the agent run git in a
cluster checkout, and a hook blocks the agent's writes to the site directory.
To help, the agent may find the checkouts read-only (`hpc-read`,
`hpc-ls`), compare `git config --get remote.origin.url` on both sides, and print
the entry for the user to paste.

Listing a checkout does **not** make it a write root: `cp`, `mv` and redirects
into it are refused as before. It only lets git do the sync operations of its
mode there.

## What each mode allows on the cluster

| | `pull-only` | `two-way` |
|---|---|---|
| `fetch` (no forcing refspec) | yes | yes |
| `pull --ff-only` | yes | yes |
| `pull` with a merge | no | yes |
| `switch <branch>`, `switch --detach <ref>` | yes, unless pinned elsewhere | yes, unless pinned elsewhere |
| `switch -c <new>` | no: create it locally and push | yes, unless pinned |
| `add`, `commit`, `push`, `merge` | no: do it locally and push | yes |
| `checkout` | no: use `switch` (`checkout` also overwrites files) | no |
| `rebase`, `pull --rebase`, `commit --amend`, `switch -C`/`-f`/`--discard-changes`, refspecs with `:` or `+` | no | no |
| force push, `reset --hard`, `clean` | never, at any setting | never |

Reads (`status`, `log`, `diff`, `branch`, `config --get`, `stash list`, ...)
are free in both modes. Every allowed write is write tier: profile `prepare`
or above. A write git can undo (`fetch`, `pull`, `clone`, `add`, `commit`,
`merge`, `switch` without a discarding flag, `stash` other than `drop`/`clear`,
`branch NAME`, `tag NAME`) needs **no token** and no prompt: send it without
`--allow-write`. Every other git write keeps the token and the user's
approval: `push`, `config`, `remote`, and anything that can discard work or
the reflog (`checkout`, `restore`, `reset`, `rebase`, `gc`, `stash drop`,
`branch -D`).

**The origin check.** Before a payload that writes with git in a listed
checkout runs its first line, it compares that checkout's `origin` with the
local repository's. The SSH, `git@host:` and HTTPS spellings of one repository
match. A different repository, a missing origin, or a path that isn't a
checkout stops the payload with exit 80. That catches an entry that pairs two
different repositories. Fixing the entry is the user's job.

**Checkouts not listed** keep the older rule: a git write is allowed only
inside a write root or script root, and the mode rules above don't apply.
List a repository to get them.

## A normal cycle

```text
local:    git commit ...; git push origin main          (the user's or local tools)
check:    bin/hpc-sync-status mymodel                   (read-only)
          -> "cluster is 2 behind origin/main: a fast-forward pull brings it up"
run:      echo 'git -C /home/me/mymodel pull --ff-only' | bin/hpc-run -   (no token: git can undo it)
verify:   bin/hpc-sync-status mymodel                   -> "in sync"
```

To test a branch on the cluster: push it from local, then
`git -C <checkout> switch <branch>` and `pull --ff-only`. `hpc-sync-status`
compares the cluster checkout with origin's copy of *its own* branch, so the
cluster on `feature-x` while local is on `main` is not reported as a
difference.

## Reading `hpc-sync-status`

`hpc-sync-status NAME` reports one entry in detail; `--all` gives one line per
entry in a single ssh round trip; with neither, it compares the current
directory against the `repo` path role, as before. `--json` for parsing. It
never writes, and it does not fetch: `origin/<branch>` is as fresh as the last
local `git fetch`. If that matters, ask the user before fetching.

| Report | Means | Next |
|---|---|---|
| in sync | local, origin and cluster agree | nothing |
| cluster is N behind origin/B | GitHub has commits the cluster doesn't | propose `pull --ff-only` |
| local has N commit(s) not pushed | local is ahead of GitHub | the push is the user's |
| cluster has N commit(s) not on origin/B | someone committed on the cluster | stop; show the user |
| cluster is at a commit the local clone doesn't have | committed on the cluster, or the local fetch is stale | ask before `git fetch` locally, then recheck |
| cluster has N uncommitted file(s) | edited on the cluster | stop; show the user |
| origins differ | the entry pairs two repositories | the user fixes the entry |
| pinned to B but on C | someone switched a pinned checkout | stop; show the user |

## The rules underneath

**Code flows local → GitHub → cluster.** In a `pull-only` checkout, the only
writes are fetch, fast-forward and switch, and the guard enforces that. A
`two-way` checkout is the user's choice for a repository they still commit to
on the cluster, and history rewrites are refused there too.

**Push and pull are separately confirm-worthy.** Propose the exact command for
each. A push changes history other clones read from; a pull changes what code
runs on shared compute next. Never chain them into one action.

**Never force.** No `--force`, no `--force-with-lease`, no `reset --hard`, on
either side.

**Surface divergence; do not reconcile it.** If the cluster checkout has
commits GitHub doesn't, uncommitted changes, or a refused fast-forward,
**stop and show it**. Do not merge, overwrite, stash or pick a winner.
Divergence usually means someone edited on the cluster, and that edit may be
the only copy.

**Credentials.** A pull runs in a payload with no terminal; git never prompts
there and fails at once if it needs a credential. Setup is in
`reference/setup.md`, "Git on the cluster".

## Scratch copies are not part of the sync

Temporary files copied to the cluster for verification (dry-run copies,
throwaway probe scripts) live **outside** the tracked checkout, in a scratch
directory, never inside the git working tree. Each one needs explicit
confirmation to create. The toolkit never deletes on the cluster, so the agent
doesn't remove them either. Keep them under one clearly named scratch
directory, and at the end of the session list that directory for the user to
remove by hand.

## Interactive reachability is not a dependency

A live ControlMaster is a working convenience for the session, never a premise
of committed code. Don't bake live SSH into tooling that gets checked in
(scripts, Makefiles, test harnesses, CI). Masters expire and collaborators
don't have one, so anything that assumes a reachable cluster works only on one
machine on one good day.
