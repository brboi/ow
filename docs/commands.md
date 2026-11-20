# Commands

## Exit Codes and Confirmation Rules

Every command exits with:

- **0**: everything ran successfully, or nothing was needed.
- **1**: at least one repo was refused or failed.
- **2**: a usage error, a declined prompt, or a prompt triggered with no TTY and no `-y`.

Commands that rewrite or remove (`repo rm`, `rebase`, `reset`, `rm`, `prune`) print the exact plan first, then prompt:

```
Proceed? [y/N]
```

Use `-y/--yes` to skip the prompt. On non-TTY (e.g. in CI), no `-y` and a prompt is an error (exit 2). Use `-n/--dry-run` to print and exit without prompting.

The **confirm protocol for frontends**: run once with `-n` to print the plan, then run again with `-y --no-fetch` to execute against the same cached refs.

## Shared Options

**`-w, --workspace <name|path>`**  
Act on a named or path-addressed workspace. Default: the workspace holding the current directory. A name matching two paths is an error.

**`--only <repo>` (repeatable)**  
On commands that act on multiple repos (`fetch`, `switch`, `pull`, `rebase`, `reset`), limit to this repo. The cwd never narrows the target.

---

## Workspace Setup

### `ow init [DIR] [REPO…] [--base <ref>] [--branch <branch>]`

Create, adopt or repair a workspace.

- **`[DIR]`** (default `.`): workspace directory. If it doesn't exist, create it and the workspace inside.
- **`[REPO…]`** (zero or more): repos to add to the workspace. Uses configured URLs if no URL given.
- **`--base <ref>`**: default base for all added repos (e.g. `origin/18.0` or shorthand `18.0`). Default: inferred from existing repos or `origin/HEAD`.
- **`--branch <branch>`**: create and switch to this branch in every repo with a base.

On success, prints which repos were switched, and any adoption notices. Re-run is idempotent.

**Exit**: 0 on success, 1 if adoption refused (e.g. conflicting existing branches).

### `ow repo add <name> [<url>] [--base <ref>] [-w <ws>]`

Register or clone a repo, and add its worktree to the workspace.

- **`<name>`**: repo name (e.g. `community`, `enterprise`). Must match `[a-z0-9_-]+`.
- **`<url>`** (optional): clone URL. If omitted, use the registered URL for this name.
- **`--base <ref>`**: default base for this repo. Re-run with `--base` only to change the default.
- **`-w, --workspace`**: apply to this workspace (default: current).

On first registration of a name, clones to `$XDG_DATA_HOME/ow/repos/<name>.git`. Subsequent `ow repo add <name>` within a workspace adds the worktree if not already present. Prints each git command as it runs.

**Exit**: 0 on success, 1 if the URL is inaccessible or the base branch doesn't exist.

### `ow repo rm <name> [-n] [-y] [-w <ws>]`

Remove a repo's worktree from the workspace. (The bare repo stays registered machine-wide.)

- **`<name>`**: repo to remove.
- **`-n, --dry-run`**: print without running.
- **`-y, --yes`**: skip confirmation.

Prints the plan (worktree remove command and any branch cleanup). Prompts unless `-y`. On success, the repo is still available to add back with `ow repo add <name>`.

**Exit**: 0 on success, 1 if the repo is not in this workspace, 2 if confirmation declined or no TTY.

### `ow repo ls [-w <ws>]`

List every known repo and its remotes. Marks repos used in this workspace with `*`.

Example output:

```
community
  origin  https://github.com/odoo/odoo.git
enterprise
  origin  https://github.com/odoo/enterprise.git  *
myaddons
  origin  git@github.com:me/myaddons.git  *
```

**Exit**: 0 always.

---

## Workspace Status and Refresh

### `ow status [-w <ws>] [--only <repo>]…`

Print a table of each repo's state: branch or detached HEAD, base (falling back to default), upstream, ahead/behind counts, and dirty file count. Also shows any in-progress operations (rebase, merge, etc.).

Never touches the network. Includes `info` diagnostics (e.g. missing base, misconfigured branches).

Example output:

```
Repo      Branch    Base          Upstream      Ahead Behind Dirty Op
community 18.0      origin/18.0   origin/18.0        0      0     0
odoo-my   feature   origin/18.0   origin/18.0        3      0     2
myaddons  (detached) origin/main  —                  0      0     0
```

**Exit**: 0 always.

### `ow fetch [-w <ws>] [--only <repo>]…`

Run `git fetch` for each remote, showing fetches as they complete. Then print the `status` table.

Use this to refresh cached refs without changing any worktree. The next `pull`/`rebase`/`reset` will use the fetched refs.

**Exit**: 0 on success, 1 if any fetch fails.

### `ow refresh [-w <ws>]`

Recompute and write all generated files (replacing the old `ow render` command). Checks ownership of every file against the lock; if a file has been modified by hand, prints a warning and skips it. On success, prints which files were written or updated.

Run this after editing `.ow/config.toml` or if an IDE config or `.env` goes stale.

**Exit**: 0 on success, 1 if a file is owned by another workspace or the config is invalid.

### `ow files [-w <ws>] [--diff]`

List all generated files and their state.

- **`--diff`**: show the unified diff of files that differ from disk instead of a state summary.

States:

- **`up to date`**: file on disk matches ow's output exactly.
- **`outdated`**: file on disk exists but differs; it will be overwritten on `ow refresh`.
- **`yours`**: file was edited by hand (not owned by ow); won't be touched.
- **`absent`**: file should exist but doesn't; will be written on `ow refresh`.
- **`skipped`**: the plugin didn't contribute this file (e.g. VSCode config when using Zed).

**Exit**: 0 always.

---

## Branch Operations

### `ow switch [<branch>] [-c <branch> [--base <ref>]] [--detach] [-w <ws>] [--only <repo>]… [-n]`

Switch the workspace to a branch. Exactly one of `<branch>`, `-c`, or `--detach` is required.

- **`<branch>`**: switch every repo to this branch. Branch must exist.
- **`-c, --create <branch>`**: create this branch instead (with `--base`, record it as the branch's base; default: repo's default base).
- **`--detach`**: detach at each repo's default base (or `HEAD` if no base).
- **`-n, --dry-run`**: print commands without running.

Prints each checkout command as it runs. On success, the workspace is on the new branch.

**Exit**: 0 on success, 1 if the branch doesn't exist or a checkout fails, 2 if the usage is invalid (no argument, or two of `-c`/`--detach`/positional).

### `ow pull [-w <ws>] [--only <repo>]… [--no-fetch] [-n]`

Fetch (unless `--no-fetch`), then fast-forward each repo to the ref it follows. The ref is the upstream (`@{u}`) if set and different from the base, otherwise the base, otherwise the default base.

If any fast-forward is impossible (the branch is ahead), the command fails for that repo and continues. Prints each `git pull` command and the result.

**Exit**: 0 on success, 1 if any repo fails.

### `ow rebase [-w <ws>] [--only <repo>]… [--base <ref>] [--autostash] [--no-fetch] [-n] [-y]`

Fetch (unless `--no-fetch`), then rebase each branch onto its base (or `--base` if given). Records the new base.

- **`--base <ref>`**: rebase onto this ref and record it as the new base (on success).
- **`--autostash`**: stash uncommitted changes before rebasing, restore after.
- **`-n`**: print the plan (runs fetch first to show exact SHAs).
- **`-y`**: skip the confirmation prompt.

Uses the guarded rebase recipe: checks for conflicts, uses `--empty=drop` for amendment commits, and saves the reflog state so `ow rebase` can recover from partial failures. On conflict, prints:

```
Rebasing community (commit X..Y):
  commit X: pick abc123 feature
  commit Y: pick def456 fixup
Conflict in community/model.py. Run to continue:
  ow rebase --continue [--only community]
```

**Exit**: 0 on success, 1 if any rebase fails or is refused, 2 if declined or no TTY.

### `ow reset [-w <ws>] [--only <repo>]… [--hard] [--no-fetch] [-n] [-y]`

Fetch (unless `--no-fetch`), then put each repo back on the ref it follows (same rule as `pull`).

- **`--hard`**: discard working-tree changes too (like `git reset --hard`), not just commits.
- **`-n`, `-y`**: same as `rebase`.

Prints the reset command and result for each repo.

**Exit**: 0 on success, 1 if any repo fails, 2 if declined or no TTY.

---

## Configuration

### `ow config [get|set|unset <key> [<value>…]] [-w <ws>] [--global]`

Read and write settings.

- **`config get <key>`**: print the effective value (workspace first, then global).
- **`config set <key> <value>…`**: set in the workspace config. Multi-word values are joined with spaces.
- **`config unset <key>`**: remove from the workspace config.
- **`--global`**: read or write the global config (`$XDG_CONFIG_HOME/ow/config.toml`) instead of the workspace config.

Setting keys use dot notation: `odoo.http_port`, `mise.trust`, `vscode.theme`. Unknown keys are warned about but accepted (for forward compatibility). Settings are organized by plugin; see [Configuration](configuration.md) for the full key table.

Example:

```sh
ow config set odoo.http_port 8080
ow config get odoo.http_port
ow config set --global odoo.smtp_port 1025
ow config unset odoo.http_port
```

**Exit**: 0 on success, 1 if the key is invalid or the file can't be written.

---

## Workspace Discovery and Deletion

### `ow ls`

List every known workspace: name (if registered), path, and each repo's branch or detached base.

Dead index entries (paths that no longer exist) are marked `missing`.

Example output:

```
voip          /home/me/ws/voip
  community   18.0
  enterprise  18.0
feature       /home/me/ws/feature  missing
```

**Exit**: 0 always.

### `ow cd [<workspace>]`

Print a workspace's path.

- **`<workspace>`** (optional): workspace name or path. Default: the workspace holding the current directory.

Output is a single line with no trailing newline, suitable for shell command substitution:

```sh
$(ow cd myfeature)
```

Used by the `ow cd` wrapper installed by `ow shell-init`.

**Exit**: 0 on success, 1 if the workspace doesn't exist, 2 if the name matches two paths.

### `ow shell-init <shell>`

Print a shell snippet that installs the `ow cd` wrapper (allowing `ow cd` to change directory) and tab completion.

- **`<shell>`**: `bash`, `zsh`, or `fish`.

Add to your shell config:

```bash
eval "$(ow shell-init bash)"
```

After sourcing, `ow cd` changes directory, and `ow <TAB>` offers completions.

**Exit**: 0 always.

### `ow rm <workspace> [-n] [-y]`

Remove a workspace: delete its worktrees, remove the directory, and remove it from the workspace index.

- **`<workspace>`**: workspace name or path.
- **`-n, --dry-run`**: print the plan without running.
- **`-y, --yes`**: skip confirmation.

Refuses if any branch is not pushed (to prevent data loss). Prints which branches are pushed and which are not. If all are pushed, and `-y` is given (or confirmed), removes worktrees and the directory.

**Exit**: 0 on success, 1 if branches are not pushed, 2 if declined or no TTY.

### `ow prune [-n] [-y]`

Clean up machine-wide:

- Stale worktree references (worktrees that were manually deleted).
- Orphan branches that are pushed but not in any workspace.
- Dead index entries (workspaces that no longer exist on disk).

No arguments. Prints the summary. Prompts unless `-y`. Prints each git command as it runs.

**Exit**: 0 on success, 1 if cleanup fails, 2 if declined or no TTY.

---

## Plan and Refusal Display

When a command plans changes (e.g. `rebase -n`), the output lists each repo and the git commands it would run:

```
community:
  git fetch origin 18.0
  git rebase --onto origin/18.0 abc123
enterprise:
  git fetch origin 18.0
  git rebase --onto origin/18.0 def456
```

Refusals (when a repo can't proceed) print a line:

```
myaddons: refused (base not found: 'origin/19.0' does not exist)
```

Notes (non-blocking messages) print inline:

```
feature: note (upstream = base, not merged)
```

---

## Post-Batch Refresh

After `ow init`, `ow repo add`, or `ow switch`, if any repos were updated (`mutated = True`), the command runs `ow refresh` and its exit code becomes the maximum of the batch's and the refresh's. This ensures IDE configs and generated files stay up to date.
