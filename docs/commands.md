# Commands

## Overview

| Command | Flags | Description |
|---------|-------|-------------|
| `ow init` | `[NAME]`, `-c/--configuration`, `-t/--template`, `-r/--repo` | Create a workspace here, or in `./NAME` |
| `ow apply` | `[workspace]` | Re-render templates and materialize worktrees |
| `ow status` | `[workspace]`, `-f/--fetch` | Show branch status with behind/ahead counts |
| `ow rebase` | `[workspace]`, `--only`, `--autostash`, `--dry-run`, `-y/--yes` | Fetch and rebase repos in a workspace |
| `ow prune` | `--dry-run`, `-y/--yes` | Clean up stale worktree references, orphaned branches, and dead index entries |
| `ow rm` | `<name>`, `-y/--yes` | Remove a workspace: worktrees, local branches, directory, and index entry |
| `ow ls` | — | List every known workspace, its path, and its repos |
| `ow templates` | `--take`, `--diff` | List template files and their state, take one, or diff the stale ones |

A command that takes a `[workspace]` resolves it in exactly one of four forms, never falling
back from one to the next:

- a **path** (starts with `~`, is `.`/`..`, or contains a separator, e.g. `./canary`) — must
  contain `.ow/config.toml` or the command fails
- a bare **name** (e.g. `ow status canary`) — looked up in the discovery index; zero matches or
  more than one is an error naming the fix (`ow ls`, or pass a path)
- no argument, **`OW_WORKSPACE` set** — must be an absolute path (not a name, not `~`, not
  relative); `mise` exports it as such automatically inside a generated workspace
- no argument, **`OW_WORKSPACE` unset** — walk up from the current directory looking for
  `.ow/config.toml`

`ow init` doesn't go through this: it resolves its *target* directory itself (the current
directory, or `./NAME`), since the workspace doesn't exist yet.

## `ow init`

Creates a workspace: in the current directory by default, or in `./NAME` if given — mirrors
`git init`. Interactive by default (templates → repos → branch specs, pre-filled from any flags
given); when stdin isn't a terminal, flags (or `-c/--configuration`, to duplicate an existing
workspace's config) must supply everything, or the command refuses to guess.

```sh
ow init my_work -r community:master..my-feature -r enterprise:master..my-feature -t common -t vscode
```

`-r` takes a single `ALIAS:SPEC` argument and `-t` a single template name; repeat either flag
to pass more than one. A `-r` value without a `:` is rejected rather than ignored. `NAME`, when
given, must be alphanumeric plus `-`/`_`.

After confirmation, `ow` sets up each repo's bare clone and required refs, creates (or
reconciles) its worktree, applies templates, writes `.ow/config.toml`, trusts `mise.toml` if the
templates produced one, and remembers the workspace in the discovery index. A repo that fails to
set up is reported; the workspace is still created as long as at least one repo succeeded, and
the command exits non-zero — the workspace exists, but it is not the one you asked for.

## `ow apply`

Re-renders templates and materializes worktrees for a workspace: creates any missing worktree,
reconciles attached/detached state for existing ones, and renders the services compose file.
Useful after changing templates or the global config without recreating the workspace.

If any template file you took has since changed upstream, `ow apply` lists it and points at
`ow templates --diff`.
Files left over from a template bundle you've since removed from your config are listed as
orphans — remove them manually if stale.
Like `ow init` and `ow rebase`, `ow apply` exits non-zero when any repo failed, even though
everything else — templates, vars, the repos that worked — is applied.

## `ow status`

Shows local branch status with behind/ahead counts — no network by default, like `git status`:

```
[canary]
    branches
        community:  dev/master-canary ↓0 ↑0 (origin/master ↓34 ↑0)
        enterprise: dev/master-canary ↓1 ↑1 (origin/master ↓12 ↑0)
    links
        runbot: master-canary
        community:  https://github.com/odoo-dev/odoo/tree/master-canary
        enterprise: https://github.com/odoo-dev/enterprise/tree/master-canary
```

Pass `-f`/`--fetch` to fetch latest refs before showing status. Behind/ahead counts are then
relative to fresh remote-tracking refs; without it, they reflect the last fetch.

## `ow rebase`

Fetches the latest refs and rebases each repo of a workspace onto its base branch.
Shows a summary and asks for confirmation before touching anything.

```sh
ow rebase                                  # every repo of the current workspace
ow rebase parrot --only community          # one repo of a named workspace
ow rebase --dry-run                        # fetch, then print the plan — no worktree touched
```

Running it twice in a row with nothing changed in between does nothing the second
time — no commit is rewritten.

For a repo whose branch is also published on a remote (`master..my-feature` with
`dev/my-feature` pushed), `ow` first integrates that remote copy only when it
carries commits yours does not, then rebases everything onto the base branch. A
force-pushed remote copy is detected by comparing the ref before and after the
fetch, and handled with a single `git rebase --onto`.

A repo is skipped, and the run exits non-zero, when a git operation is already in
progress (the message gives the exact `--continue` / `--abort` command), when the
worktree has uncommitted changes, or when the worktree is missing. `--autostash` stashes and restores uncommitted changes instead.
`--only` restricts the whole run to the selected repos, including drift warnings and ref fetching.

On conflict, resolve, `git rebase --continue`, then re-run `ow rebase --only <alias>`. Nothing is
ever pushed: the `git push --force-with-lease` stays yours.

`--dry-run` fetches refs to show you what would happen, but runs no command that
touches your worktrees.

## `ow prune`

Cleans up stale worktree references and orphaned local branches from every bare repo, and drops
dead entries from the workspace discovery index. Run after manually removing a workspace
directory:

```sh
rm -rf ~/wherever/my-workspace
ow prune
```

Deleting a branch is the one step that can lose work, so it is confirmed first,
defaulting to no; `-y/--yes` skips the prompt and `--dry-run` stops after the
survey. A branch holding commits no remote has is never deleted — it is listed,
with the command to delete it by hand.


## `ow rm`

Removes a workspace and everything `ow` created for it: worktrees unregistered from their bare
repos, local branches deleted, the workspace directory removed, and the index entry dropped.
Bare repos are shared and stay.

```sh
ow rm canary              # asks for confirmation after showing what will go
ow rm canary -y           # skip the prompt
```

Before touching anything, `ow rm` shows a summary of each repo: its branch spec, whether the
local branch is safe to delete (pushed to a remote), and warns about unpushed commits and
uncommitted changes in the working tree. Confirmation defaults to no — `-y/--yes` skips it.

A workspace whose bare repo is missing still has its directory and index entry cleaned up.

## `ow ls`

Lists every workspace `ow` currently knows about, in name order — name, path (home-relative), and its repos
with their branch specs — read from the discovery index and each workspace's own
`.ow/config.toml`. No git, no network: this is local files only. A workspace config that fails
to parse shows as an error in place of its repos rather than aborting the listing.

## `ow templates`

Lists every template file `ow` can use, with its state:

- `packaged` — shipped inside `ow`, unmodified
- `taken` — you have a local override
- `taken, outdated` — the packaged file changed since you took it

`--take BUNDLE/PATH` copies one packaged file into your local overrides, plus a pristine
baseline used later to detect drift. `--diff` prints a unified diff (baseline vs. current
packaged) for every outdated file. See [Template System](templates.md).

## Tab Completion

One-time setup for your current shell:
```sh
ow --install-completion
```

Then restart your shell. To inspect the generated script instead of installing it:
```sh
ow --show-completion
```

Completion covers template names (`ow init -t <TAB>`), repo aliases (`ow init -r <TAB>`,
which only offers aliases you haven't already passed) and workspace names
(`ow status <TAB>`, `ow rm <TAB>`, from the same discovery index `ow ls` reads — so a workspace `ow` has
never resolved is not offered).
