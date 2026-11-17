# One-time setup: ControlMaster over your center's baseline

This is a one-time `~/.ssh/config` edit done **by the user**. An agent must not
modify anything under `~/.ssh/`.

Many HPC clusters require interactive authentication (2FA token, password,
often through a gateway host) on *every* connection, which normally makes them
unreachable from an agent that has no TTY. A persistent ControlMaster socket
removes that barrier: the user authenticates once, the master stays alive, and
every later `ssh`/`rsync`/`scp` rides that already-authenticated socket with no
prompt. Claude Code runs as the local user with the real `$HOME`, so it reads
the same config and the same sockets — it is simply another client on the
master.

## Your center's baseline

Most centers publish an official connection recipe, often a `ProxyCommand`
through a gateway host. It is authoritative for the gateway pattern and for
host naming; follow it first.

**ControlMaster is a delta on top of that baseline.** The three `Control*`
lines below are an addition, not a correction: nothing here should replace
or contradict your center's instructions. The example assumes a gateway
`gateway.example.org` in front of the cluster `cluster.example.org`; use
your center's names and its `ProxyCommand` line.

## The config

```sshconfig
Host cluster.example.org
    User <user>
    LogLevel Quiet
    ProxyCommand ssh -l <user> gateway.example.org -W %h:%p
    ControlMaster auto
    ControlPath ~/.ssh/sockets/%r@%h-%p
    ControlPersist 4h

Host gateway.example.org          # multiplex the gateway hop too
    User <user>
    ControlMaster auto
    ControlPath ~/.ssh/sockets/%r@%h-%p
    ControlPersist 4h
```

**The gateway hop must be multiplexed too.** Without the second block, every
connection to the destination re-authenticates through the gateway, which
defeats the point — the `ProxyCommand` opens a fresh gateway session each time.

Omit `Protocol 2` if you see it in older recipes; it has been a no-op since
OpenSSH 7.6.

## The socket directory

```bash
mkdir -p ~/.ssh/sockets
chmod 700 ~/.ssh/sockets
```

**The `chmod 700` is required, not hygiene.** SSH refuses to create a control
socket in a world- or group-accessible directory and fails in a way that does
not obviously point at permissions.

## Opening and checking the master

```bash
ssh -fN cluster.example.org     # user runs this; prompts for 2FA
ssh -O check cluster.example.org
```

`-f` backgrounds after authenticating, `-N` runs no remote command. The socket
then persists for `ControlPersist` (4h above) past the last client.

From then on, `bin/hpc-check` is the check to run, and everything else goes
through `bin/hpc-run`.

## The harness-level fence

The skill's "never hand-roll `ssh`" rule is prose the model follows. The
fence that holds regardless is two PreToolUse hooks, which the plugin
registers itself (`hooks/hooks.json`); Claude Code runs them before every
Bash command, in every session and subagent:

- `block_raw_ssh.py` blocks a command whose verb is a transport program
  (`ssh`, `scp`, `sftp`, `rsync`, ...) and whose target is an HPC host, and
  names the toolkit verb to use. HPC hosts are the site files' `host` values,
  expanded through `~/.ssh/config` (aliases, HostName, proxy hosts). Other
  hosts (workstations, git servers) pass. With no readable site file, or a
  target in a shell variable, it blocks.
- `approve_hpc_writes.py` opens your permission prompt for any toolkit
  command carrying a confirmation token, and refuses one in
  `bypassPermissions` mode.

Do not add `ssh`/`scp`/`rsync` deny rules to `~/.claude/settings.json` as
well. They duplicate the hook, and a blanket rule such as `Bash(ssh *)`
overrides it: it is checked first and blocks ssh to every host, not only
clusters. The hook reads your site files, so adding a cluster needs no
settings change.

The toolkit itself is unaffected: its `ssh` and `scp` calls are Python
subprocess calls, not Bash tool command lines. A script that shells out to
`ssh` (`python3 fetch.py`) is also not seen by the hook; it needs profile
`improvise`, and `hpc-fetch` exists so that directory pulls need no such
script. `hooks/test_*.py` pins what is blocked, asked and allowed.

## Recording the site

Once the master works, describe the cluster in
`~/.config/hpc-tether/sites/<name>.toml`. Start from the template
`sites/example.toml` in this skill. The toolkit reads only the config
directory (override: `HPC_TETHER_SITES`), never the templates, so an update
to the toolkit cannot overwrite your site file. Put **only** host alias,
scheduler, user, and path roles there. User, port, identity, and `ProxyCommand` stay in `~/.ssh/config`, which
remains the single authority; duplicating them creates two places to be wrong.

## Git on the cluster

The usual way code reaches the cluster is: commit locally, `git push` to
GitHub, then `git pull` in the cluster checkout (`reference/git-sync.md`). The
pull runs in a payload, and a payload has no terminal. So the cluster needs a
GitHub credential that works **without anyone typing**. Every payload exports
`GIT_TERMINAL_PROMPT=0`, plus `GIT_SSH_COMMAND="ssh -o BatchMode=yes"` unless you
set your own. A missing credential therefore fails at once with git's error
(`could not read Username`, `Permission denied (publickey)`) instead of
hanging until the watchdog.

Pick one, on the cluster, in your own terminal:

- **An SSH key on the cluster** (most common). Run
  `ssh-keygen -t ed25519 -f ~/.ssh/id_github` with no passphrase, or one held
  by an agent you start yourself. Add the `.pub` to GitHub (Settings → SSH
  keys), and in the cluster's `~/.ssh/config`:

      Host github.com
          IdentityFile ~/.ssh/id_github
          IdentitiesOnly yes

  Clone with the `git@github.com:owner/repo.git` form.
- **A deploy key per repository**, read-only, if you'd rather the cluster
  could not push. Same steps, but the key goes under the repository's
  Settings → Deploy keys. Use one key per repository, with a `Host` alias for
  each.
- **HTTPS with a token**, through git's credential store
  (`git config --global credential.helper store`, then one interactive pull
  to save it). The token sits on the cluster in plain text, so prefer a
  fine-grained token limited to the repositories you need.

Agent forwarding (`ForwardAgent`) through the ControlMaster is not
recommended: a forwarded agent lets anything on the login node use your
laptop's keys while the master is open.

**Test it** the way a payload will run it, no terminal:

    GIT_TERMINAL_PROMPT=0 git -C <checkout> ls-remote origin HEAD < /dev/null

It prints one line with a commit hash. Then confirm the toolkit sees the
same: `echo 'git -C <checkout> ls-remote origin HEAD' | bin/hpc-run -`.

**A public repository needs no credential to pull over HTTPS.** Checkouts
with `https://github.com/...` remotes pull through payloads with nothing set up. A credential matters only for
private repositories, or for pushing from the cluster (`mode = "two-way"` in
`git-sync.md`).

**Using ssh remotes:** the first connection to GitHub asks you to confirm its
host key. Answer once in your own terminal (`ssh -T git@github.com`):
`BatchMode` refuses an unknown host key instead of asking.

## What this does not change

- **Starting work is the user's choice of profile.** Submitting jobs and
  running trusted scripts need profile `operate`, which only the user sets in
  the site file. Below it the agent inspects and prepares, and the user
  launches. The default, `observe`, sends only the fixed tools' toolkit-written scripts and no free-form shell.
- **Committed tooling must not depend on live SSH.** The master expires, and
  collaborators do not have one. Interactive reachability is a working
  convenience, not a dependency to bake into a script. If a committed script
  genuinely needs the remote, it should check and print a friendly
  "run `ssh -fN <host>` first" rather than assuming — which is exactly what
  `bin/hpc-check` does.
