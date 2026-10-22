# Commands

Every ow command, its flags, its exit codes and the lines it prints. Commands run inside a
workspace unless stated otherwise, and print each git command as they run it. Every output
below comes from a real run.

## Exit codes

- **0** — the command did its work, or nothing needed doing.
- **1** — a repo was refused, or a step failed.
- **2** — a usage error, a declined prompt, or a prompt with no terminal and no `-y`.

Notes and refusals print on stdout as `# <repo>: <text>`; errors print as `error: <text>` on
stderr:

```
# community: diverged from origin/18.0; run `ow rebase`
# community: modified files; commit, stash, or pass --autostash
```

Commands that change or delete print their plan, then ask:

```
Proceed? [y/N]
```

`-y/--yes` skips the question, `-n/--dry-run` prints the plan and stops before running it. A
command with nothing to run does not prompt. Without a terminal and without `-y`, a prompt is an
error (exit 2). A frontend confirms in two runs: `-n` to show the plan, then `-y --no-fetch` to
execute against the same cached refs.

## Shared options

- **`-w, --workspace WS`** — the workspace to act on, by name or path (default: the one holding
  the current directory). On every command except `ls`, `shell-init` and `prune`.
- **`--only REPO`** — repeatable; on `fetch`, `switch`, `pull`, `rebase` and `reset`. An unknown
  name is exit 2: `error: unknown repo 'x' in --only; this workspace has: community, enterprise`.
- **`-n, --dry-run`** — on `switch`, `pull`, `rebase`, `reset`, `repo rm`, `rm`, `prune`.
- **`-y, --yes`** — on `rebase`, `reset`, `repo rm`, `rm`, `prune`.
- **`--no-fetch`** — on `pull`, `rebase`, `reset`: work on the cached refs.
- **`ow --version`** prints `ow <version>`; a bare `ow` prints the help and exits 0.

Commands that move branches refresh the generated files afterwards, and their exit code is the
worse of the batch's and the refresh's.

---

## Setup

### `ow init [DIR] [REPO…] [--base REF] [--branch BRANCH]`

Create, adopt or repair the workspace at `DIR` (default: the current directory).

- **`DIR`** — created when missing; an existing directory holding `.ow/config.toml` is adopted.
- **`REPO…`** — repos to add. Each needs a registered bare, or a name ow knows
  (`community`, `enterprise`, `design-themes`).
- **`--base REF`** — base for the repos added: `18.0` is stored as `origin/18.0`.
- **`--branch BRANCH`** — create and switch to this branch in every repo.

Adding a repo fetches its base with an explicit refspec, then adds a worktree on `--branch`, or
detached at the base. Adoption repairs what ow 2 left behind (`remote.origin.tagOpt`, ref logs,
`origin/HEAD`; it adds no fetch refspec), relinks a moved worktree, re-adds a repo whose worktree is gone, and rewrites
`.ow/config.toml`, warning about every key it drops. Unknown settings and rejected bases warn once
per config file, ending with `run \`ow init\``. Re-running is idempotent; a repo already present
is reported instead of switched:

```
$ ow init ws/voip community enterprise --base 18.0 --branch 18.0-voip
git -C ~/.local/share/ow/repos/community.git fetch origin +refs/heads/18.0:refs/remotes/origin/18.0
git -C ~/.local/share/ow/repos/community.git worktree add --no-track -b 18.0-voip ~/ws/voip/community origin/18.0
git -C community config branch.18.0-voip.ow-base origin/18.0
…
added community (base origin/18.0)
added enterprise (base origin/18.0)

$ ow init ws/voip community --branch other
# community: present; run `ow switch other`
```

**Exit**: 0 on success; 1 when a step failed or the refresh was blocked; 2 for an unknown repo or
an invalid base, before anything is written.

### `ow repo add NAME [URL] [--base REF] [-w WS]`

Register a repo, and add its worktree to the workspace.

- **`NAME`** — repo name; the worktree lands at `<workspace>/<name>`.
- **`URL`** — clone URL. May be omitted for a name ow knows (`community`, `enterprise`,
  `design-themes`) or a bare already registered.
- **`--base REF`** — default base for this repo, stored fully qualified.

On first registration the bare is created at `~/.local/share/ow/repos/<name>.git`, with a
blobless fetch of the default branch and no fetch refspec, so other branches arrive only when a workspace needs them. Outside a workspace only the bare is registered. Inside one
the worktree is added too, detached at the base. A URL that differs from a registered one is
ignored, with a note naming the registered one.

```
$ ow repo add payments ~/remotes/payments.git
git init --bare ~/.local/share/ow/repos/payments.git
git -C ~/.local/share/ow/repos/payments.git config remote.origin.url ~/remotes/payments.git
git -C ~/.local/share/ow/repos/payments.git config remote.origin.tagOpt --no-tags
git -C ~/.local/share/ow/repos/payments.git config core.logAllRefUpdates true
git -C ~/.local/share/ow/repos/payments.git ls-remote --symref origin HEAD
git -C ~/.local/share/ow/repos/payments.git fetch --filter=blob:none origin +refs/heads/master:refs/remotes/origin/master
git -C ~/.local/share/ow/repos/payments.git remote set-head origin master

$ ow repo add payments --base 18.0
# payments: default base origin/18.0 (shared by community, enterprise)
git -C ~/.local/share/ow/repos/payments.git fetch origin +refs/heads/18.0:refs/remotes/origin/18.0
git -C ~/.local/share/ow/repos/payments.git worktree add --detach ~/ws/voip/payments origin/18.0
```

**Exit**: 0 on success; 1 when the clone fails; 2 for an unknown name without a URL.

### `ow repo ls [-w WS]`

One line per registered bare: `<name>: <url>`, with `(in this workspace)` on the repos the
workspace uses and `(no origin)` on a bare without a remote.

```
$ ow repo ls
community: ~/remotes/community.git (in this workspace)
enterprise: ~/remotes/enterprise.git (in this workspace)
payments: ~/remotes/payments.git
```

**Exit**: 0 always.

### `ow repo rm NAME [-n] [-y] [-w WS]`

Remove a repo's worktree from the workspace. The bare stays registered.

The plan removes the worktree, then deletes the branch when a remote-tracking ref contains it; an
unpushed branch is kept, with the command that deletes it. A dirty worktree, or a detached HEAD
holding commits no remote has, refuses before the prompt. A worktree already gone is reported and
the `[repos]` entry dropped.

```
$ ow repo rm payments -n
git -C ~/.local/share/ow/repos/payments.git worktree remove ~/ws/voip/payments

$ ow repo rm enterprise -n
# enterprise: kept unpushed branch local-only; delete it with `git -C ~/.local/share/ow/repos/enterprise.git branch -D local-only`
git -C ~/.local/share/ow/repos/enterprise.git worktree remove ~/ws/voip/enterprise

$ ow repo rm enterprise
# enterprise: untracked scratch.txt
# enterprise: commit or stash these changes first
```

**Exit**: 0 on success; 1 refused; 2 declined or no terminal.

---

## Settings

### `ow config [get|set|unset …] [-w WS] [--global]`

Read and write settings. Bare `ow config` lists every declared setting with its effective value,
its origin and its help; `--global` lists the global layer and the defaults only.

- **`ow config get KEY`** — print the effective value; a string prints raw, an unset key prints
  nothing (exit 0).
- **`ow config set KEY VALUE…`** — one item per value; multi-word strings join with spaces.
- **`ow config unset KEY`** — unknown keys are accepted: unsetting is how you clear the
  unknown-key warning.
- **`--global`** — read or write `$XDG_CONFIG_HOME/ow/config.toml` instead of the workspace's.

Every write rewrites the whole file, prints the keys it drops, then refreshes the generated files.
A config write leaves a sidecar lock next to the file it wrote.

```
$ ow config
odoo.http_port = 8069 (default) # HTTP port for Odoo
odoo.db_name = "voip" (default) # Database name (defaults to workspace name)
odoo.smtp_port = 25 (default) # SMTP port
…

$ ow config set odoo.http_port 8080
wrote odoorc

$ ow config get odoo.http_port
8080

$ ow config get --global odoo.smtp_port
1025

$ ow config set odoo.http_prot 80
error: unknown setting 'odoo.http_prot'; did you mean 'odoo.http_port'?
```

**Exit**: 0 on success; 1 when a write or the refresh fails; 2 for an unknown key (`set` only) or
a bad value. See [Configuration](configuration.md) for the key table and the layer rules.

---

## Status and files

### `ow status [-w WS]`

One line per repo: name, state (branch, or `detached at <sha>`), base, upstream, base counts,
dirty file count, and the operation in progress. Never touches the network. On a terminal the
columns are aligned under a header; piped, the fields are tab-separated with no header.

```
$ ow status
community	18.0-voip	origin/18.0	(none)	(even)
enterprise	18.0-voip	origin/18.0	(none)	(even)
```

`(none)` is an upstream that is unset or equal to the base, `(even)` a 0/0 count, `(default)` a
base falling back to the repo's default, `no base` a repo ow has no base for, `detached at <sha>`
a detached HEAD, `dirty N` uncommitted files, and `rebase in progress` (or merge, cherry-pick,
…) a repo holding an unfinished operation.


**Exit**: 0 always. Outside a workspace: 2.

### `ow fetch [-w WS] [--only REPO]…`

Print every fetch line, run them in parallel, then print the status table. Each line names its
refspec explicitly, so a bare without a `remote.<r>.fetch` config still fetches, and the base and a
distinct upstream are separate steps. A force-pushed ref and an upstream branch that no longer
exists are notes, not failures.

```
$ ow fetch
git -C ~/.local/share/ow/repos/community.git fetch origin +refs/heads/18.0:refs/remotes/origin/18.0
git -C ~/.local/share/ow/repos/community.git fetch origin +refs/heads/saas-18.1:refs/remotes/origin/saas-18.1
git -C ~/.local/share/ow/repos/enterprise.git fetch origin +refs/heads/18.0:refs/remotes/origin/18.0
# community: upstream origin/saas-18.1 is gone; `git branch --unset-upstream`
community	18.0-voip	origin/18.0	origin/saas-18.1 (ahead 1, behind 1)	ahead 0, behind 1
enterprise	18.0-voip	origin/18.0	(none)	(even)
```

A force-pushed base prints the same kind of note: `# community: origin/18.0 was force-pushed`.
A failing fetch prints git's output and the other repos go on; `pull`, `rebase` and `reset` refuse
the repo instead, with `# <repo>: fetch failed; pass --no-fetch to work on cached refs`.


**Exit**: 0 on success; 1 when a fetch failed.

### `ow refresh [-w WS]`

Write every generated file that is missing or outdated. Prints `wrote <path>` per file. A file
edited by hand is left alone with a warning naming how to get ow's version back; a file behind a
symlinked directory is skipped. An error diagnostic from a plugin means nothing is written.

```
$ ow refresh
wrote pyrightconfig.json
warning: odoorc: not overwritten (yours); delete it and run `ow refresh` to get ow's version back
```

**Exit**: 0 on success; 1 when a plugin reported an error and nothing was written.

### `ow files [-w WS] [--diff]`

List the generated files, one `<path>\t<state>` line each:

- **`up to date`** — the file matches ow's output.
- **`outdated`** — the file matches the lock but ow's output changed; `ow refresh` rewrites it.
- **`yours`** — edited by hand since ow wrote it; never overwritten.
- **`skipped`** — not written, with the reason (a symlinked parent directory).
- **`absent`** — missing; `ow refresh` writes it.

Lock entries no plugin produces any more print as `stale (no longer produced)`; their files stay.
`--diff` prints a unified diff per differing file, read as current → ow's proposal, so `+` lines
are what ow would write. An absent file is all additions; a binary one prints `<path>: binary`.

```
$ ow files
odoorc	yours
pyrightconfig.json	absent
.vscode/settings.json	skipped	.vscode is a symlink
…

$ ow files --diff
--- pyrightconfig.json
+++ ow's pyrightconfig.json
@@ -0,0 +1,4 @@
+{
+  "venvPath": ".",
+  "venv": ".venv"
+}
```

**Exit**: 0; 1 when a plugin error blocks the refresh.

---

## Branch operations

### `ow switch [BRANCH] [-c BRANCH] [--base REF] [--detach] [-w WS] [--only REPO]… [-n]`

Put every repo of the workspace on one branch, create one, or detach.

- **`BRANCH`** — switch to an existing branch. A branch only a remote publishes is fetched and
  tracked; a repo without the branch is left alone with `no <branch>`.
- **`-c, --create BRANCH`** — create the branch from `--base`, or from the repo's default base,
  and record `branch.<b>.ow-base`. Re-running switches to the branch, re-writes a missing
  `ow-base`, and says `--base ignored; <b> already exists` when `--base` was passed.
- **`--detach`** — detach at each repo's default base.
- **`-n, --dry-run`** — print the steps without running them.

Exactly one of `BRANCH`, `-c` and `--detach` is required; `--base` needs `-c`. Pre-flight is
all-or-nothing: an operation in progress, a broken worktree, a branch checked out in another
worktree, a detached HEAD holding commits no branch has, a branch two remotes publish, or a
failing `ls-remote` refuses every repo (the others print `nothing switched`). When a step fails
mid-run, the split is reported.

```
$ ow switch master
git -C community switch -c master --no-track origin/master
git -C community config branch.master.remote origin
git -C community config branch.master.merge refs/heads/master
git -C community config branch.master.ow-base origin/18.0
git -C enterprise switch -c master --no-track origin/master
git -C enterprise config branch.master.remote origin
git -C enterprise config branch.master.merge refs/heads/master
git -C enterprise config branch.master.ow-base origin/18.0

$ ow switch -c 18.0-voip2 --base 18.0
git -C community switch --no-track -c 18.0-voip2 origin/18.0
git -C community config branch.18.0-voip2.ow-base origin/18.0
…

$ ow switch master
# community: commits on no branch; save them with `git -C community switch -c <b>`
# enterprise: nothing switched

$ ow switch nope
# community: nothing switched
# enterprise: ls-remote origin failed: fatal: '/nonexistent/repo.git' does not appear to be a git repository

$ ow switch nope
error: no repo has nope; create it with `ow switch -c nope`
```

Upstreams are recorded in the bare's config (`branch.<b>.remote`, `branch.<b>.merge`) with no fetch refspec behind them. A manual `git fetch` or `git pull` only touches the current branch. The trade-off: after a manual `git push` or `git pull`, `refs/remotes/<r>/<b>` stays old until the next `ow fetch`, so `git status` and editors show no ahead/behind until then.

**Exit**: 0 on success; 1 refused or failed; 2 for an invalid combination of arguments.

### `ow pull [-w WS] [--only REPO]… [--no-fetch] [-n]`

Fetch, then fast-forward each repo to the ref it follows: its upstream when set and different
from the base, else its base. Only a strict fast-forward runs; a repo that is ahead and behind is
refused and the others go on. No prompt: this never drops a commit.

```
$ ow pull -n
git -C ~/.local/share/ow/repos/community.git fetch origin +refs/heads/18.0:refs/remotes/origin/18.0
git -C ~/.local/share/ow/repos/enterprise.git fetch origin +refs/heads/18.0:refs/remotes/origin/18.0
git -C enterprise merge --ff-only origin/18.0

$ ow pull
# enterprise: diverged from origin/18.0; run `ow rebase`
```

**Exit**: 0 on success; 1 when a repo was refused or failed.

### `ow rebase [-w WS] [--only REPO]… [--base REF] [--autostash] [--no-fetch] [-n] [-y]`

Fetch, then rebase each branch onto its base, replaying the branch's own commits on top.

- **`--base REF`** — rebase onto this ref with `--onto`, then record it as the new `ow-base`.
- **`--autostash`** — stash uncommitted changes before the rebase, restore them after.
- **`-n` / `-y`** — print the plan / skip the prompt.

The guard refuses an operation already in progress, a modified worktree without `--autostash`,
and a failed fetch. A plan that only moves the branch to a commit it already contains is a
fast-forward: it runs without asking. On a conflict the run stops with the labelled tail:

```
$ ow rebase --autostash -y
git -C community stash push -m 'ow rebase --autostash'
git -C community rebase origin/18.0
git -C community stash pop
CONFLICT (add/add): Merge conflict in conflict.txt
# resolve the conflict, then:
git -C community rebase --continue
# or give up:
git -C community rebase --abort
# then:
git -C community stash pop

$ ow rebase
# community: modified files; commit, stash, or pass --autostash

$ ow rebase
# community: rebase in progress; finish it or run `git -C community rebase --abort`
```

**Exit**: 0 on success; 1 refused or failed; 2 declined or no terminal.

### `ow reset [-w WS] [--only REPO]… [--hard] [--no-fetch] [-n] [-y]`

Fetch, then put each repo back on the ref it follows (the same rule as `ow pull`), dropping
commits. Without `--hard` a behind-only HEAD uses `--keep`, so local changes survive; dropping
commits uses a plain `git reset <ref>`, which leaves the working tree as it is. `--hard` discards
the working tree too.

The plan names what is at stake: the commits that exist on no remote, the modified files, and the
untracked files the ref also tracks (`--hard` overwrites those). It prompts whenever there is
something to run.

```
$ ow reset -n
# enterprise: drops 1 commit(s) on no remote
git -C enterprise reset origin/18.0

$ ow reset --no-fetch -n
git -C enterprise reset --keep origin/18.0

$ ow reset --hard -n
# enterprise: modified: VERSION
…
# enterprise: untracked, overwritten: VERSION
git -C enterprise reset --hard origin/18.0
```

**Exit**: 0 on success; 1 refused or failed; 2 declined or no terminal.

---

## Machine-wide

### `ow ls`

One line per indexed workspace: `<name>\t<path>\t<repos>`, each repo as `<name>:<branch>`. An
index entry whose directory is gone prints `missing`.

```
$ ow ls
voip	~/ws/voip	community:18.0-voip2, enterprise:behind-only
buried	~/ws/buried	missing
```

**Exit**: 0 always.

### `ow cd [WS]`

Print the path of a workspace (default: the one holding the current directory), one line. The
shell wrapper installed by `ow shell-init` turns it into a real `cd`.

**Exit**: 0 on success; 2 when the name is unknown, ambiguous (two indexed paths), or dead
(`run \`ow prune\``).

### `ow shell-init SHELL`

Print the shell integration for `bash`, `zsh` or `fish`: an `ow()` function that turns
`ow cd …` into a real `cd` and leaves every other call to the binary, then a completion script.
The zsh completion is registered through a `compdef` guard, so it works whether or not `compinit`
has run.

```sh
eval "$(ow shell-init bash)"   # bash, zsh
ow shell-init fish | source    # fish
```

**Exit**: 0; 2 for any other shell name.

### `ow rm WS [-n] [-y]`

Remove a workspace: its worktrees, its pushed branches, its directory and its index entry.

Refused, before the prompt, when a worktree is dirty, when a detached HEAD holds commits no
branch has, or when a directory holds a checkout ow does not manage (a second worktree, a plain
clone). The plan first names the top-level entries `rm -rf` takes besides the worktrees, with
their file counts, then each removal, the branch deletions, and the index drop.

```
$ ow rm ws/gone -y
# .ow/ (3 files), .vscode/ (2 files), .zed/ (2 files), mise/ (1 file), notes/ (2 files), odools.toml, odoorc, pyrightconfig.json, requirements-dev.txt, scratch.txt
# community: kept unpushed branch kept; delete it with `git -C ~/.local/share/ow/repos/community.git branch -D kept`
git -C ~/.local/share/ow/repos/community.git worktree remove ~/ws/gone/community
rm -rf ~/ws/gone
# drop ~/ws/gone from the index

$ ow rm ws/gone
# community: untracked dirty.txt; commit, stash or discard, then re-run
```

**Exit**: 0 on success; 1 refused; 2 declined or no terminal.

### `ow prune [-n] [-y]`

Clean up machine-wide, in one batch with one prompt:

- worktrees whose directory is gone, each announced with a hint to run `ow init` there first;
- branches no worktree has checked out: deleted when a remote-tracking ref contains them, kept
  with the deleting command otherwise;
- index entries whose workspace no longer exists.

```
$ ow prune -y
# ~/ws/dead/community: prunable (moved a workspace? run `ow init` there first)
# community: kept unpushed branch temp-orphan; delete it with `git -C ~/.local/share/ow/repos/community.git branch -D temp-orphan`
git -C ~/.local/share/ow/repos/community.git worktree prune
git -C ~/.local/share/ow/repos/community.git branch -D 18.0-voip
# drop ~/ws/dead from the index
```

**Exit**: 0 on success; 1 refused or failed; 2 declined or no terminal.

---

## See also

- [Configuration](configuration.md) — settings, layers, and every key.
- [Generated files](files.md) — what ow writes, ownership, and the refresh model.
- [Migrating to 3.0](migrating-to-3.0.md) — adopting a 2.x workspace, removed commands.