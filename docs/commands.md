# Commands

## Overview

| Command | Flags | Description |
|---------|-------|-------------|
| `ow init` | `[NAME]`, `-c/--configuration`, `-t/--template`, `-r/--repo` | Create a workspace here, or in `./NAME` |
| `ow apply` | `[workspace]`, `-w/--workspace`, `--check` | Re-render templates and materialize worktrees |
| `ow status` | `[workspace]`, `-w/--workspace`, `-f/--fetch` | Show branch status with behind/ahead counts |
| `ow fetch` | `[workspace]`, `-w/--workspace`, `--only` | Refresh the refs a workspace follows, without touching any worktree |
| `ow rebase` | `[workspace]`, `-w/--workspace`, `--only`, `--autostash`, `--dry-run`, `-y/--yes`, `--no-fetch` | Fetch and rebase repos in a workspace |
| `ow pull` | `[workspace]`, `-w/--workspace`, `--only`, `--dry-run` | Fetch and fast-forward repos in a workspace |
| `ow reset` | `[workspace]`, `-w/--workspace`, `--only`, `--hard`, `-f/--fetch`, `--dry-run`, `-y/--yes` | Put repos back on the refs they follow |
| `ow switch` | `[target]`, `-w/--workspace`, `-c/--create`, `--detach`, `--only`, `-a/--all`, `--dry-run`, `--include-detached-specs` | Switch every repo in a workspace to a branch; repos configured detached are pins and left alone by default |
| `ow mv` | `<source>`, `<dest>`, `-y/--yes` | Move a workspace to a new path, repairing its worktrees |
| `ow archive` | `<name>`, `-y/--yes` | Park a workspace without losing its worktrees or branches |
| `ow unarchive` | `<name>`, `[dest]`, `-y/--yes` | Restore an archived workspace |
| `ow rm` | `<name>`, `-w/--workspace` (alias for `<name>`), `-y/--yes` | Remove a workspace: worktrees, local branches, directory, and index entry |
| `ow prune` | `--dry-run`, `-y/--yes`, `--also-backups` | Clean up stale worktree references, orphaned branches, dead index entries |
| `ow ls` | `--archived` | List every known workspace, its path, and its repos |
| `ow cd` | `[workspace]`, `-w/--workspace` | Print a workspace path — with `ow shell-init`, changes directory |
| `ow shell-init` | `<shell>` | Print the shell snippet that makes `ow cd` change directory |
| `ow open` | `[workspace]`, `-w/--workspace` | Open a workspace in the configured editor |
| `ow templates` | `[workspace]`, `-w/--workspace`, `--diff` | List a workspace's template files and their state, or diff the outdated ones |
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

Every one of these commands also accepts `-w/--workspace` naming the same workspace as the
positional form. Passing both with different values is an error; passing both with the same
value is accepted. `ow rm` is the exception: `-w` is a plain alias for its mandatory `<name>`
argument rather than another way to spell the same resolution, and `ow rm` never resolves a
workspace implicitly — it is the destructive command, so it insists on being told.

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
Useful after changing templates or the global config without recreating the workspace. `--check`
reports drift and outdated templates without modifying anything, and exits non-zero if either is
found — for scripts and pre-commit hooks.

Each template file is copied into `<ws>/.ow/templates/<bundle>/<relpath>` before it is rendered;
`ow apply` prints `materialised <name>` the first time a file lands there, and `updated <name>`
when it overwrites a copy you never touched whose source has since moved. A copy you edited
yourself is never touched — silently, if `ow`'s source hasn't moved either, or flagged with a
pointer to `ow templates --diff` if it has.
Files left over from a template bundle you've since removed from your config are listed as
orphans — remove them manually if stale.
Like `ow init` and `ow rebase`, `ow apply` exits non-zero when any repo failed, even though
everything else — templates, vars, the repos that worked — is applied.

## Interactive Dashboard

Running `ow` without a subcommand in a terminal launches the interactive dashboard — a
two-pane TUI for managing workspaces without memorising flags. In a non-TTY environment
(scripts, pipes) it prints help and exits 2, so `ow` without arguments is safe to type
anywhere.

The left pane lists every known workspace (active, then archived, separated); the right
pane shows the highlighted workspace's config and, once status has been gathered, a
per-repo table with behind/ahead counts, drift warnings, and links. A log pane at the
bottom captures every operation's output.

### Key bindings

| Key | Action |
|-----|--------|
| `j` / `k` | Move up/down in the workspace list |
| `tab` / `shift+tab` | Cycle focus between list, detail, and log |
| `enter` | Focus the detail pane |
| `s` | Status (local — no fetch) |
| `f` | Fetch + status |
| `a` | Apply |
| `R` | Rebase |
| `P` | Pull |
| `S` | Switch |
| `r` | Reset |
| `p` | Prune |
| `n` | New workspace |
| `e` | Edit workspace config |
| `E` | Edit global config |
| `o` | Open in editor |
| `m` | Move workspace |
| `A` | Archive / unarchive |
| `x` | Remove workspace |
| `ctrl+r` | Reload workspace list |
| `ctrl+l` | Clear log |
| `ctrl+c` | Cancel running operation, or quit if idle |
| `?` | Help screen |
| `q` | Quit |

Operations that touch worktrees run on a worker thread; the log pane shows their output
in real time and a progress row appears for multi-step operations. `ctrl+c` cancels the
running operation (killing child git processes) without exiting the dashboard; pressed
again when idle, it quits.

### Theme

Press `Ctrl+P` to open Textual's command palette, then type "theme" to search and select a theme. The choice is persisted to `theme` in the global config and survives across sessions. You can also click the header icon at the top-left to open the palette. See [Configuration](configuration.md) for the config field.
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

## `ow fetch`

Refreshes the refs a workspace follows — `git fetch`, into the bare repos, one repo at a time —
and reports what arrived. The network half of `ow pull` and `ow rebase`, on its own: no worktree
moves, no branch changes, nothing but the bare repos' remote-tracking refs.

```sh
ow fetch                                    # every repo of the current workspace
ow fetch parrot --only community            # one repo of a named workspace
```

What each ref is after the fetch is compared to what it was before, which is the one thing
`git fetch`'s own output does not say: `+34` means the ref advanced by 34 commits, `up to date`
means it did not move, `force-pushed +N -M` means the remote rewrote it under you — which is
what `ow rebase` will have to replay around and what `ow reset -f` exists to adopt.

Each repo follows its base ref and, when its branch is pushed somewhere, its upstream — two
refs, two rows, because a fetch moves them independently. A repo whose worktree is missing or
whose remote is unreachable is reported and skipped; the run exits non-zero if any fetch failed.

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


`--no-fetch` rebases against the refs already in the bare repos rather than fetching first —
for a workspace whose remotes are unreachable, or for replaying the same rebase the second time.
`--dry-run` fetches refs to show you what would happen, but runs no command that
touches your worktrees.

## `ow pull`

Brings every repo of a workspace up to date without moving any of them off the
base branch they are configured on. `ow rebase` is the command that does move
them.

```sh
ow pull                                    # every repo of the current workspace
ow pull parrot --only community            # one repo of a named workspace
ow pull --dry-run                          # fetch, then print the plan — no worktree touched
```

Each repo follows its upstream when it has one — its own branch as pushed
elsewhere — and its base ref otherwise.

- already there: left alone
- detached: re-detaches onto the fetched ref
- behind: `git merge --ff-only`, which git refuses if the move would clobber a
  local modification
- diverged **from its upstream**: `git rebase <upstream>`, exactly what
  `git pull --rebase` does — same branch, same base, nobody else's commits
- diverged **from its base ref**, with no upstream: reported and left alone.
  Carrying local work over to a moved base needs force-push detection and a
  replay floor, and is `ow rebase`'s job

A repo is also left alone, and the run exits non-zero, when a git operation is
already in progress, when a replay would be needed but the worktree is dirty
(the message names the files), when the worktree is missing, or when the fetch
failed — moving onto the stale cached ref would look like success.

On conflict during a replay, resolve, `git rebase --continue`, then re-run
`ow pull --only <alias>`. Nothing is ever pushed.

## `ow reset`

Puts every repo back on the ref it follows — `git reset`, one repo at a time.
Use it when a workspace has wandered, or when an upstream was force-pushed and
you want the remote's version of the branch, whatever yours has become.

```sh
ow reset                                   # every repo of the current workspace
ow reset parrot --only enterprise          # one repo of a named workspace
ow reset --hard                            # discard the working tree too
ow reset --hard -f                         # ... against freshly fetched refs
ow reset --dry-run                         # print the plan, touch nothing
```

**The ref each repo is reset to** is the one `git reset @{u}` would have used:
the branch's upstream — its own copy on a remote — or, for a branch nobody has
pushed and for a detached repo, the base ref it was cut from. An attached branch
is never reset onto its base: that would not put the repo back, it would throw
the whole branch away.

The two forms differ exactly as git's do:

- `ow reset` moves HEAD and leaves the working tree alone. The commits are gone
  from the branch, but their content is still on disk as unstaged changes —
  **nothing on disk is lost**.
- `ow reset --hard` discards the working tree as well. Untracked files are left
  alone, exactly as `git reset --hard` leaves them; `git clean` is its own
  command.

No fetch by default: it resets to the refs already in the bare repos, the way
`git reset origin/master` does. `-f/--fetch` refreshes them first, which is what
an upstream that was force-pushed needs — otherwise you land on the copy the
last fetch happened to cache.

The summary counts what each repo loses before anything runs, and flags the
commits no remote carries — the only ones a reset makes unrecoverable outside
the reflog. The confirmation defaults to no; `-y/--yes` skips it.

A repo is skipped, and the run exits non-zero, when a git operation is already
in progress, when the worktree is missing, when its refs will not resolve
locally, or when it is not on the branch the config names. That last one
matters: resetting whatever else happens to be checked out would throw away
work ow was never told about, and realigning is `ow apply`'s job.

## `ow switch`

Switches every repo of a workspace to a branch — `git switch`, one repo at a time. Unlike
`ow init`'s `base..feature` specs, the argument here is always a branch: `ow switch` moves a
workspace that already exists, it does not define one.

```sh
ow switch 18.0                              # every repo of the current workspace
ow switch -c feat-x origin/master           # create feat-x from origin/master and switch to it
ow switch --detach origin/master            # detached HEAD at origin/master
ow switch 18.0 --include-detached-specs     # ... including the repos pinned to a bare ref
cd community && ow switch 18.0              # only community — you are standing in it
cd community && ow switch 18.0 --all        # ... the whole workspace anyway
```

A repo configured detached — a bare ref in `.ow/config.toml`, no `..branch` — is a **pin**: the
config names the exact ref it should sit on, and a run that moves the workspace's branches has no
business rewriting it. Pins are left alone by default, reported as such in the summary, and join
the run only when you insist: `--include-detached-specs` for all of them, or `--only` naming one
(with a branch target they switch like any other repo and become attached specs; with `--detach`
they are re-pinned at the new ref).

A branch that exists on exactly one remote is created locally and switched to as a tracking
branch — the DWIM `git switch --guess` performs. `ow` does that guessing itself: its bare repos
are cloned `--single-branch` and extra branches are fetched outside the remote's configured
refspec, which makes git's own `--guess` refuse to find them.

The same guess settles the two forms git does not guess for at all. `git switch --detach 18.0`
on a branch that is only remote does not detach — git turns it into a branch creation and then
refuses its own combination with `'--detach' cannot be used with -b/-B/--orphan` — and
`git switch -c fix 18.0` simply fails with `invalid reference: 18.0`. Both get the
remote-tracking ref by name, so `--detach 18.0` detaches at `origin/18.0` and pins the repo to
that ref, not to the short name (which would always mean `origin`'s).

Run from inside one of the repos, with no workspace and no `--only` named, the run narrows to
that repo and says so: `cd community && ow switch 18.0` is the question `git` would have answered
there, and answering it for the whole workspace instead moves repos nobody mentioned. `-a/--all`
asks for the whole workspace anyway, and naming a workspace (`-w`, a path, or `$OW_WORKSPACE`)
never narrows. A pin narrowed to this way is still left alone: standing in a directory is not
insisting on it, `--only` and `--include-detached-specs` are.

No fetch happens by default. When the target isn't already known locally, `ow` asks each remote
the repository actually has — not the ones `[remotes.<alias>]` happens to declare, since a
workspace outlives its config entries — for that one branch, and asks them all at once. A name
no remote carries therefore costs a single round trip and no fetch at all, and a repo already on
the target costs nothing: it is reported as `already there`, nothing is run for it, and its spec
is left exactly as it was.

The run opens with the same summary the other commands print, before anything moves:
```
[voip] switch to 18.0
  community   master..master  new branch tracking origin/18.0
  enterprise  18.0..18.0      already there
  voip        master..voip    local branch
  upgrade     master          left alone — detached spec
```

Pre-flight is all-or-nothing, unlike `ow rebase`, `ow pull`, and `ow reset`, which skip a bad
repo and continue: every selected repo must have its worktree present, no git operation already
in progress, a resolvable target, and — with `-c` — no branch of that name yet, or nothing is
switched at all and the command exits 2. The table marks every offending repo `refused`, and the
way out is printed once rather than once per repo:

```
  enterprise  18.0..18.0  refused — no branch named 'master-voip-2' here or on any remote

Nothing was switched: a switch moves the whole workspace or none of it.
  create it with `ow switch -c master-voip-2`
```

A workspace half on 17.0 and half on 18.0 is exactly what `ow` exists to prevent, and there is
no `ow unswitch` to walk it back.

A dirty worktree is not a reason to refuse: `git switch` carries uncommitted changes across when
it can, and `ow` does not second-guess it.

Once a repo has actually moved, `.ow/config.toml` is rewritten from what git left on disk, not
from what was asked for: an attached branch with an upstream gets `<upstream>..<branch>`, an
attached branch without one keeps its start point (`-c`) or the repo's previous base ref, and a
detached repo gets the bare ref you asked for. Each repo's `Done.` line carries the spec that
was written for it. Templates are deliberately not re-rendered — the run ends by telling you to
run `ow apply` if you need them refreshed.

`--dry-run` prints the same summary, then the exact `git switch` invocation per repo, and writes
nothing.

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
`--also-backups` lists every `.ow/config.toml` backup `ow rm` has saved over time, and deletes
them with the same prompt — the last safety net before they go.

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

## `ow mv`

Moves a workspace to a new path, repairing what `mv` alone would break: the bare repos still
point at the old worktree paths, the discovery index still names the old directory, and the
rendered templates hold absolute paths that only `ow apply` can regenerate. All three are
fixed, in that order — the worktrees have to work again before the addon scan that re-renders
`odoorc` can see anything.

```sh
ow mv parrot ~/odoo/parrot          # rename
ow mv ./parrot ..                    # move into the parent (mv(1) semantics)
```

`mv(1)` semantics: an existing directory means "move into it", anything else is the new path
itself. Renaming changes `db_name` and `dbfilter` in `odoorc` to the new name — the existing
Odoo database is not renamed. A `.venv` holds absolute paths and must be rebuilt at the new
location (`mise install`); the move warns when one is present.

## `ow archive` / `ow unarchive`

Parks a workspace without losing it, then brings it back. Archiving is relocation to a
canonical place (`$XDG_DATA_HOME/ow/archives/<name>`) plus dropping the index entry; the
worktrees stay registered — repaired at the archive path — and the local branches stay, which
is the whole point: an archived workspace comes back exactly as it left.

```sh
ow archive parrot                    # park it
ow ls --archived                     # see what's parked
ow unarchive parrot                  # restore it to ./parrot
ow unarchive parrot ~/odoo/parrot   # ... or somewhere else
```

Unarchiving is the same move in reverse, plus a re-render so the absolute paths in `odoorc`
name wherever it landed. `ow ls --archived` lists the archive — it is not in the index by
design, so it is read straight off the filesystem.

## `ow cd` / `ow shell-init`

A process cannot change its parent shell's directory, so `ow cd` prints a path and a shell
function — installed by `ow shell-init` — does the actual `cd`. Add the integration to your
shell rc:

```sh
eval "$(ow shell-init bash)"    # or zsh, or: ow shell-init fish | source
```

Then `ow cd parrot` changes directory; without the snippet, the same command prints the path.

## `ow open`

Opens a workspace in the configured editor — `editor` in the global config, defaulting to `code`.
No `xdg-open`, `$EDITOR`, or `$VISUAL` cascade: a cascade makes the command's behaviour depend on
ambient environment, which is the opposite of what a workspace manager should do.

```sh
ow open parrot                       # opens ~/odoo/parrot in `code`
ow open                               # the current workspace (OW_WORKSPACE or walk-up)
```

## `ow ls`

Lists every workspace `ow` currently knows about, in name order — name, path (home-relative), and its repos
with their branch specs — read from the discovery index and each workspace's own
`.ow/config.toml`. No git, no network: this is local files only. `--archived` lists the archive
instead, which is not in the index by design — see `ow archive`. A workspace config that fails
to parse shows as an error in place of its repos rather than aborting the listing.

## `ow templates`

Lists every template file materialised into one workspace, with its state:

- `up to date` — your copy matches what `ow` copied it from
- `modified` — you edited it, and `ow`'s source hasn't moved since
- `outdated` — you edited it, and `ow`'s source has moved since; `ow apply` leaves it alone
  either way, so this is the one state you have to reconcile by hand
- `unlocked` — the file has no lock entry, because you (or something else) added it directly;
  `ow` never touches it and never reports it as anything else

Owning a template now means editing its copy directly, under
`<ws>/.ow/templates/<bundle>/<relpath>`. `--diff` prints a unified diff, from your copy
(`(yours)`) to `ow`'s current source (`(ow)`), for every outdated file. See
[Template System](templates.md).

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
